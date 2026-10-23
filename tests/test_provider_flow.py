"""Provider collection through real subprocess/HTTP transports into cached panels."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import support  # noqa: F401 — isolate cache and config first

from porchlight import alerts, collect, core, registry
from porchlight import sources  # noqa: F401 — register the collector's built-in sources
from porchlight.panels import dev as panels
from porchlight.ui import RCtx


class ProviderFlow(unittest.TestCase):
    def setUp(self):
        support.clear_cache()

    def tearDown(self):
        support.clear_cache()

    def test_cloud_cli_processes_collect_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            answers = {
                "gcloud": {"auth list": [{"account": "operator@example.com", "status": "ACTIVE"}], "config list": {},
                           "projects describe": {"projectId": "example-dev", "projectNumber": "123456"}},
                "az": {"account show": {"account": "operator@example.com", "subscription": "Example dev",
                                        "id": "11111111-2222-3333-4444-555555555555", "tenant": "example"},
                       "account list-locations": ["eastus"]},
            }
            for name, replies in answers.items():
                script = root / name
                script.write_text("#!%s\nimport json, sys\nreplies = %r\n"
                                  "print(json.dumps(replies[' '.join(sys.argv[1:3])]))\n" % (sys.executable, replies))
                script.chmod(0o700)
            h = support.host('[sources.gcp_auth]\nproject = "example-dev"\n[sources.azure_auth]\n')
            original_which = core.which

            def which(name, paths=()):
                return str(root / name) if name in answers else original_which(name, paths)

            with mock.patch.object(core, "which", which):
                for name in ("gcp_auth", "azure_auth"):
                    record = collect.run_one(registry.SOURCES[name], h, {})
                    self.assertTrue(record["ok"])
                    self.assertEqual(record["data"]["state"], "ok")
                panel = panels.p_access(RCtx(h, registry.Cache(), {"vitals": {}}, [], 130))
            text = support.strip("\n".join(panel.lines))
            self.assertIn("gcp", text)
            self.assertIn("azure", text)
            self.assertIn("example-dev", text)
            self.assertIn("Example dev", text)
            self.assertNotIn("not verified", text)

    def test_forge_http_collection_cache_panel_and_alert(self):
        requests = []
        pull = {"number": 7, "state": "open", "user": {"login": "operator"}, "title": "Update service",
                "mergeable": False, "updated_at": "2026-10-22T10:00:00Z"}
        bb_pull = {"id": 8, "state": "OPEN", "author": {"uuid": "operator-id"}, "title": "Update worker",
                   "updated_on": "2026-10-22T10:00:00Z", "participants": [], "reviewers": []}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                requests.append((path, self.headers.get("Authorization")))
                if path == "/2.0/user":
                    body = {"uuid": "operator-id"}
                elif path == "/api/v1/user":
                    body = {"login": "operator"}
                elif path == "/api/v1/settings/api":
                    body = {"max_response_items": 50}
                elif path.endswith("/reviews"):
                    body = [{"user": {"login": "reviewer"}, "state": "REQUEST_CHANGES", "stale": True,
                             "submitted_at": "2026-10-22T10:00:00Z"}]
                elif path.endswith("/pulls"):
                    body = [pull]
                elif path.endswith("/pullrequests"):
                    body = {"values": [bb_pull]}
                else:
                    self.send_error(404)
                    return
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if path.endswith("/reviews"):
                    self.send_header("X-Total-Count", str(len(body)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        allowed = {"gitea.example.test", "forgejo.example.test", "api.bitbucket.org"}

        def http(url, **kwargs):
            parsed = urllib.parse.urlsplit(url)
            self.assertIn(parsed.hostname, allowed)
            self.assertEqual(parsed.scheme, "https")
            # Replace only the socket destination; serialization and response parsing
            # still use Porchlight's real HTTP helper and a local fixture server.
            local = "http://127.0.0.1:%d%s?%s" % (server.server_port, parsed.path, parsed.query)
            return core.http(local, **kwargs)

        h = support.host('''[sources.prs]
github = false
gitlab = false
gitea = true
gitea_url = "https://gitea.example.test"
gitea_repos = ["team/service"]
forgejo = true
forgejo_url = "https://forgejo.example.test"
forgejo_repos = ["team/tools"]
bitbucket = true
bitbucket_repos = ["team/worker"]
''')
        env = {"GITEA_TOKEN": "fixture-gitea", "FORGEJO_TOKEN": "fixture-forgejo", "BITBUCKET_TOKEN": "fixture-bitbucket"}
        try:
            with mock.patch.object(registry.Ctx, "http", staticmethod(http)):
                record = collect.run_one(registry.SOURCES["prs"], h, env)
            self.assertTrue(record["ok"], record)
            self.assertEqual(record["data"]["forges"], ["bb", "fj", "gt"])
            self.assertEqual(record["data"]["open"], 3)
            cache = registry.Cache()
            panel = panels.p_prs(RCtx(h, cache, {"vitals": {}}, [], 130))
            self.assertIn("3 open", support.strip("\n".join(panel.lines)))
            found = alerts.gather(h, cache, {"vitals": {"mem": {"used": 1, "total": 10}, "swap": {"total": 0},
                                                        "load": [0], "cores": 1}})
            self.assertTrue(any("changes requested" in a["msg"] for a in found))
            self.assertFalse(any("conflict" in a["msg"] for a in found))
            self.assertEqual({auth for _, auth in requests}, {"token fixture-gitea", "token fixture-forgejo", "Bearer fixture-bitbucket"})
            persisted = Path(registry.src_path("prs")).read_text()
            for token in env.values():
                self.assertNotIn(token, persisted)
            self.assertEqual(os.stat(registry.src_path("prs")).st_mode & 0o777, 0o600)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

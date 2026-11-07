"""The knowledge source (sources/knowledge.py). Contract: docs/contracts/knowledge-v1.md.

Offline: every HTTP call is a fake routed by URL substring (FakeHTTP); the Drover
daemon reflections.list goes through a temp state dir + a fake op dispatcher, the
same shape test_drover.py uses for sources/drover.py itself.
"""
import support  # noqa: F401 — first: scratch cache and config

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from porchlight import core, registry, theme as T
from porchlight.panels import knowledge as pknowledge  # noqa: F401 — registers the panel
from porchlight.sources import knowledge
from porchlight.ui import BUILDERS, RCtx
from support import FakeHTTP, ctx, host, put, resp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "porchlight", "data", "demo", "knowledge.json")


def demo():
    with open(DEMO, encoding="utf-8") as f:
        raw = json.load(f)
    from porchlight import demo as pdemo
    return pdemo._relative(raw["knowledge"], time.time())

CANARY = "sk-ant-" + "a1b2c3d4e5" * 3  # a token shape, built at runtime: never scanner bait


def base_cfg(**over):
    cfg = {"brain_name": "brain", "brain_health_url": "", "brain_mcp_url": "", "brain_key_env": "",
           "brain_list_tool": "brain_list", "brain_recent": 5, "brain_web_url": "", "reflections": True,
           "wikis": [], "links": []}
    cfg.update(over)
    return cfg


def mcp_ok(items, limit=5, has_more=False):
    """A tools/call reply shaped like a real MCP server's (structuredContent.items)."""
    return resp(200, {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"items": items, "page": 1,
                                                                                   "limit": limit, "hasMore": has_more}}})


def mcp_text_only(items):
    """A server that answers with only the text content block (defensive fallback path)."""
    return resp(200, {"jsonrpc": "2.0", "id": 1,
                       "result": {"content": [{"type": "text", "text": json.dumps({"items": items})}]}})


def mcp_tool_error(msg):
    return resp(200, {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": msg}], "isError": True}})


def mcp_rpc_error(msg):
    return resp(200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": msg}})


def brain_entry(slug="e1", title="voice gateway: AEC decision", kind="decision", created="2026-11-01T00:00:00Z", url=""):
    return {"slug": slug, "title": title, "kind": kind, "createdAt": created, "url": url}


class Brain(unittest.TestCase):
    def test_health_and_recent_entries(self):
        h = host("")
        fake = FakeHTTP({
            "brain.example.com/healthz": resp(200, {"ok": True, "stale": False, "entries": 1840}),
            "brain.example.com/mcp": mcp_ok([brain_entry(), brain_entry("e2", "second one")]),
        })
        c = ctx(h, env={"BRAIN_KEY": "tok"}, http=fake)
        cfg = base_cfg(brain_health_url="https://brain.example.com/healthz", brain_mcp_url="https://brain.example.com/mcp",
                        brain_key_env="BRAIN_KEY")
        out = knowledge._brain(c, cfg, {})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["entries"], 1840)
        self.assertFalse(out["stale"])
        self.assertEqual(len(out["recent"]), 2)
        self.assertEqual(out["recent"][0]["id"], "e1")
        self.assertEqual(out["recent"][0]["title"], "voice gateway: AEC decision")
        self.assertEqual(out["recent"][0]["kind"], "decision")
        self.assertGreater(out["recent"][0]["created"], 0)
        # Only one outgoing header carried the key, and only to the MCP call.
        mcp_calls = [c for c in fake.calls if c[1].endswith("/mcp")]
        self.assertEqual(len(mcp_calls), 1)
        self.assertEqual(mcp_calls[0][2].get("Authorization"), "Bearer tok")
        health_calls = [c for c in fake.calls if c[1].endswith("/healthz")]
        self.assertNotIn("Authorization", health_calls[0][2])  # health is a public GET

    def test_the_text_only_fallback_is_parsed_too(self):
        h = host("")
        fake = FakeHTTP({"brain.example.com/mcp": mcp_text_only([brain_entry("e3", "third")])})
        c = ctx(h, env={"BRAIN_KEY": "tok"}, http=fake)
        cfg = base_cfg(brain_mcp_url="https://brain.example.com/mcp", brain_key_env="BRAIN_KEY")
        out = knowledge._brain(c, cfg, {})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["recent"][0]["title"], "third")

    def test_a_tool_error_and_an_rpc_error_are_both_short_messages_not_raised(self):
        h = host("")
        for route, body in (("brain.example.com/mcp", mcp_tool_error("Tool brain_list not found")),
                            ("brain.example.com/mcp2", mcp_rpc_error("Internal error"))):
            fake = FakeHTTP({route: body})
            c = ctx(h, env={"BRAIN_KEY": "tok"}, http=fake)
            cfg = base_cfg(brain_mcp_url="https://" + route, brain_key_env="BRAIN_KEY")
            out = knowledge._brain(c, cfg, {})
            self.assertFalse(out["ok"])
            self.assertTrue(out["error"])

    def test_no_key_env_set_is_a_short_error_never_a_network_call(self):
        h = host("")
        fake = FakeHTTP({})
        c = ctx(h, env={}, http=fake)
        cfg = base_cfg(brain_mcp_url="https://brain.example.com/mcp", brain_key_env="BRAIN_KEY")
        out = knowledge._brain(c, cfg, {})
        self.assertFalse(out["ok"])
        self.assertIn("BRAIN_KEY", out["error"])
        self.assertEqual(fake.calls, [])

    def test_a_failed_part_keeps_its_previous_data(self):
        h = host("")
        fake = FakeHTTP({"brain.example.com/healthz": resp(503, {"ok": False})})
        c = ctx(h, env={}, http=fake)
        cfg = base_cfg(brain_health_url="https://brain.example.com/healthz")
        prev = {"entries": 900, "stale": False, "recent": [{"id": "old", "title": "an old one", "kind": "memory",
                                                              "created": 1, "url": ""}]}
        out = knowledge._brain(c, cfg, prev)
        self.assertFalse(out["ok"])
        self.assertEqual(out["entries"], 900)            # not reset to 0
        self.assertEqual(out["recent"], prev["recent"])  # not reset to []

    def test_titles_are_masked_and_bounded(self):
        h = host("")
        long_title = "see " + CANARY + " and https://bob:hunter2@example.com/x " + "x" * 300
        fake = FakeHTTP({"brain.example.com/mcp": mcp_ok([brain_entry("e1", long_title)])})
        c = ctx(h, env={"BRAIN_KEY": "tok"}, http=fake)
        cfg = base_cfg(brain_mcp_url="https://brain.example.com/mcp", brain_key_env="BRAIN_KEY")
        out = knowledge._brain(c, cfg, {})
        title = out["recent"][0]["title"]
        self.assertLessEqual(len(title), 160)
        self.assertNotIn(CANARY, title)
        self.assertIn("sk-ant-", title)                  # the shape's prefix survives
        self.assertNotIn("hunter2", title)

    def test_a_long_slug_id_is_never_masked_only_the_title_is(self):
        """Regression: a real brain's slug (its id) is a long, readable, hyphenated
        string — exactly the shape secrets.mask_shapes's 40+-char rule redacts in
        freeform text. The id is a pointer the "copy id" action depends on, not
        something that could ever be a pasted secret: it must survive whole."""
        slug = "mac-mini-securityd-wedge-root-cause-locked-login-026ca3"
        self.assertGreater(len(slug), 40)
        fake = FakeHTTP({"brain.example.com/mcp": mcp_ok([brain_entry(slug, "a short title")])})
        c = ctx(host(""), env={"BRAIN_KEY": "tok"}, http=fake)
        cfg = base_cfg(brain_mcp_url="https://brain.example.com/mcp", brain_key_env="BRAIN_KEY")
        out = knowledge._brain(c, cfg, {})
        self.assertEqual(out["recent"][0]["id"], slug)

    def test_a_token_prefixed_id_is_still_masked(self):
        """The id is spared the long-run rule only: a known token prefix never survives."""
        tok = "ghp_" + "Z9" * 18
        fake = FakeHTTP({"brain.example.com/mcp": mcp_ok([brain_entry(tok, "a short title")])})
        c = ctx(host(""), env={"BRAIN_KEY": "tok"}, http=fake)
        cfg = base_cfg(brain_mcp_url="https://brain.example.com/mcp", brain_key_env="BRAIN_KEY")
        out = knowledge._brain(c, cfg, {})
        self.assertNotIn(tok, out["recent"][0]["id"])
        self.assertTrue(out["recent"][0]["id"].startswith("ghp_"))

    def test_not_configured_means_absent_from_the_record(self):
        h = host("")
        # reflections=False here: with no [sources.drover] state_dir this machine's own
        # Drover daemon, if any, must never be the one a unit test happens to reach.
        c = ctx(h, cfg=base_cfg(reflections=False), env={}, http=FakeHTTP({}))
        out = knowledge.knowledge(c)
        self.assertNotIn("brain", out)
        self.assertNotIn("reflections", out)
        self.assertNotIn("wikis", out)
        self.assertNotIn("links", out)
        self.assertEqual(out["v"], 1)


class Reflections(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.state = tempfile.mkdtemp(prefix="knowledge-drover-state-")
        self.addCleanup(shutil.rmtree, self.state, True)
        with open(os.path.join(self.state, "daemon.json"), "w") as f:
            json.dump({"url": "http://127.0.0.1:7337/"}, f)
        with open(os.path.join(self.state, "token"), "w") as f:
            f.write("main-fixture-9f2c1a\n")

    def toml(self, extra=""):
        return '[sources.drover]\nstate_dir = "%s"\n%s' % (self.state, extra)

    def daemon(self, items, **kw):
        d = Daemon(items, **kw)
        p = mock.patch.object(registry.Ctx, "http", staticmethod(d))
        p.start()
        self.addCleanup(p.stop)
        return d

    def test_counts_by_kind_and_a_few_recent_titles(self):
        self.daemon([refl("r1", "action", "atlas", "do the thing"), refl("r2", "action", "web-1", "another"),
                     refl("r3", "question", "atlas", "should we?")])
        h = host(self.toml())
        out = knowledge._reflections(registry.Ctx(h, {}, None, {}), {})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["open"], 3)
        self.assertEqual(out["by_kind"], {"action": 2, "question": 1})
        self.assertEqual(len(out["recent"]), 3)
        self.assertEqual(out["recent"][0]["id"], "r1")
        self.assertEqual(out["recent"][0]["host"], "atlas")
        self.assertGreater(out["recent"][0]["created"], 0)

    def test_a_long_reflection_id_is_never_masked(self):
        long_id = "r-" + "a1b2c3" * 8  # 50 chars: the same shape mask_shapes would otherwise redact
        self.daemon([refl(long_id, "action", "atlas", "do the thing")])
        h = host(self.toml())
        out = knowledge._reflections(registry.Ctx(h, {}, None, {}), {})
        self.assertEqual(out["recent"][0]["id"], long_id[:24])  # bounded, not redacted

    def test_without_a_main_token_it_is_an_error_not_a_403(self):
        os.remove(os.path.join(self.state, "token"))
        self.daemon([])
        h = host(self.toml())
        out = knowledge._reflections(registry.Ctx(h, {}, None, {}), {})
        self.assertFalse(out["ok"])
        self.assertIn("main token", out["error"])

    def test_no_daemon_is_a_short_error(self):
        h = host('[sources.drover]\nstate_dir = "%s"\n' % os.path.join(self.state, "nowhere"))
        out = knowledge._reflections(registry.Ctx(h, {}, None, {}), {})
        self.assertFalse(out["ok"])
        self.assertEqual(out["open"], 0)

    def test_a_failure_keeps_the_previous_counts(self):
        h = host(self.toml())  # no daemon.json removed, but op() will 404 on an unknown op name below
        self.daemon([], down=True)
        prev = {"open": 4, "by_kind": {"action": 4}, "recent": [{"id": "old", "title": "x", "kind": "action",
                                                                  "host": "atlas", "created": 1}]}
        out = knowledge._reflections(registry.Ctx(h, {}, None, {}), prev)
        self.assertFalse(out["ok"])
        self.assertEqual(out["open"], 4)
        self.assertEqual(out["recent"], prev["recent"])


def refl(rid, kind, host_, title):
    return {"id": rid, "kind": kind, "host": host_, "title": title, "created_at": "2026-11-01T00:00:00Z"}


class Daemon:
    """Mirrors test_drover.py's fake: op -> result, the main token only."""

    def __init__(self, items, down=False):
        self.items, self.down, self.reqs = items, down, []

    def __call__(self, url, method="GET", headers=None, json_body=None, timeout=6.0, **kw):
        op = url.rsplit("/", 1)[-1]
        auth = (headers or {}).get("Authorization", "")
        tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        self.reqs.append({"op": op, "token": tok, "body": json_body})
        if self.down:
            return core.Resp(502, {"ok": False, "error": "bad gateway"}, 1)
        if op != "reflections.list":
            return core.Resp(404, {"ok": False, "error": "unknown op %s" % op}, 1)
        return core.Resp(200, {"ok": True, "result": {"items": self.items}}, 3)


class Wikis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="knowledge-wiki-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, rel, mtime=None):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("# hi\n")
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def test_no_path_keeps_only_the_link(self):
        out = knowledge._wiki({"name": "docs", "url": "https://example.com/wiki"}, {})
        self.assertEqual(out, {"name": "docs", "url": "https://example.com/wiki", "ok": True, "error": "", "pages": 0,
                                "recent": []})

    def test_a_missing_path_is_an_error_for_that_wiki_only_and_keeps_previous_counts(self):
        prev = {"docs": {"pages": 3, "recent": [{"title": "Old", "path": "Old.md", "mtime": 1}]}}
        out = knowledge._wiki({"name": "docs", "url": "", "path": os.path.join(self.tmp, "nowhere")}, prev)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "no such directory")
        self.assertEqual(out["pages"], 3)
        self.assertEqual(out["recent"], prev["docs"]["recent"])

    def test_md_files_counted_newest_first_git_skipped(self):
        now = time.time()
        self.write("Runbook.md", mtime=now - 10)
        self.write("On-call.md", mtime=now - 1000)
        self.write("sub/Deep.md", mtime=now - 5)
        self.write("README.txt", mtime=now)       # not markdown
        self.write(".git/config", mtime=now)        # skipped entirely
        out = knowledge._wiki({"name": "docs", "url": "https://example.com/wiki", "path": self.tmp}, {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["pages"], 3)
        self.assertEqual([r["title"] for r in out["recent"]], ["Deep", "Runbook", "On-call"])
        self.assertEqual(out["recent"][0]["path"], "sub/Deep.md")


class Links(unittest.TestCase):
    def test_bad_urls_are_dropped(self):
        self.assertIsNone(knowledge._link({"label": "x", "url": "javascript:alert(1)"}))
        self.assertIsNone(knowledge._link({"label": "x", "url": ""}))
        self.assertIsNone(knowledge._link("not a dict"))
        self.assertEqual(knowledge._link({"label": "Status", "url": "https://example.com/status"}),
                          {"label": "Status", "url": "https://example.com/status"})


class Source(unittest.TestCase):
    def test_absent_parts_are_absent_from_the_record_and_alerts_fire_on_a_bad_brain(self):
        h = host("")
        fake = FakeHTTP({"brain.example.com/healthz": resp(200, {"ok": False, "reason": "degraded"})})
        src = registry.SOURCES["knowledge"]
        cfg = src.config(h)
        cfg = dict(cfg, brain_health_url="https://brain.example.com/healthz", reflections=False)
        c = registry.Ctx(h, {}, None, cfg)
        c.http = fake
        out = knowledge.knowledge(c)
        self.assertEqual(out["v"], 1)
        self.assertIn("brain", out)
        self.assertNotIn("reflections", out)
        self.assertNotIn("wikis", out)
        self.assertNotIn("links", out)
        self.assertEqual(out["alerts"], [{"sev": "warn", "msg": "brain: degraded"}])

    def test_links_are_passed_through_when_configured(self):
        h = host("")
        src = registry.SOURCES["knowledge"]
        cfg = dict(src.config(h), reflections=False, links=[{"label": "Status", "url": "https://example.com/status"},
                                                              {"label": "bad", "url": "not a url"}])
        c = registry.Ctx(h, {}, None, cfg)
        c.http = FakeHTTP({})
        out = knowledge.knowledge(c)
        self.assertEqual(out["links"], [{"label": "Status", "url": "https://example.com/status"}])


class Panel(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.addCleanup(T.set_mode, "truecolor")
        self.addCleanup(T.set_ascii, False)

    def build(self, data=None, cw=80, last_seen=None):
        put("knowledge", demo() if data is None else data)
        return BUILDERS["knowledge"](RCtx(host(""), registry.Cache(), {"vitals": {}}, [], cw, last_seen))

    def test_renders_the_demo_record_with_a_new_entry_note_and_items(self):
        p = self.build(last_seen=time.time() - 3600)
        rows = [T.strip(ln) for ln in p.lines]
        self.assertTrue(any(r.startswith("brain") and "1,840 entries" in r for r in rows), rows)
        self.assertTrue(any("new: voice gateway: AEC decision" in r for r in rows), rows)
        self.assertTrue(any(r.startswith("reflect") and "9 open" in r for r in rows), rows)
        self.assertTrue(any(r.startswith("wikis") and "docs 42 pages" in r for r in rows), rows)
        self.assertTrue(any(r.startswith("links") and "Dashboard" in r and "Status" in r for r in rows), rows)
        self.assertEqual(p.note, "1 new since last visit")
        self.assertGreater(len(p.items), 0)
        ids = [it.id for it in p.items]
        self.assertEqual(len(ids), len(set(ids)))  # stable, unique ids

    def test_fits_every_width_in_ascii_mode_without_cutting_a_word(self):
        d = demo()
        vocab = set()
        import re as _re

        def words(obj):
            if isinstance(obj, dict):
                for v in obj.values():
                    words(v)
            elif isinstance(obj, list):
                for v in obj:
                    words(v)
            elif isinstance(obj, str):
                vocab.update(_re.split(r"[\s,]+", obj))
        words(d)
        T.set_mode("none")
        T.set_ascii(True)
        for cw in (40, 80, 140):
            p = self.build(d, cw=cw, last_seen=time.time() - 3600)
            for ln in p.lines:
                self.assertLessEqual(T.width(ln), cw - 1, "%d: %r" % (cw, T.strip(ln)))
            for ln in p.render(cw, T.ACCENT):
                self.assertLessEqual(T.width(ln), cw)
                self.assertTrue(all(ord(ch) < 128 for ch in T.asciify(ln)), ln)
                plain = T.strip(ln)
                for m in _re.finditer(r"(\S*)(\.\.\.)", plain):
                    self.assertIn(m.group(1), vocab, "a word cut at %d: %r" % (cw, plain))

    def test_an_empty_record_shows_nothing_configured(self):
        p = self.build({"v": 1})
        self.assertEqual([T.strip(ln) for ln in p.lines], ["○ nothing configured"])
        self.assertEqual(p.items, [])

    def test_no_record_at_all_is_no_panel(self):
        support.clear_cache()
        self.assertIsNone(BUILDERS["knowledge"](RCtx(host(""), registry.Cache(), {"vitals": {}}, [], 80)))

    def test_an_error_record_is_shown_dimmed_and_never_crashes(self):
        p = self.build({"v": 1, "brain": {"name": "brain", "ok": False, "error": "HTTP 503", "ms": 0, "entries": 0,
                                           "stale": False, "recent": [], "web_url": ""}})
        self.assertEqual(p.level, "crit")
        text = "\n".join(T.strip(ln) for ln in p.lines)
        self.assertIn("HTTP 503", text)


if __name__ == "__main__":
    unittest.main()

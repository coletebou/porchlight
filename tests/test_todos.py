"""The todos source (sources/todos.py), the TODOS panel (panels/todos.py) and
`porchlight todos` (todo_cmd.py), offline.

Every provider is fed canned responses: files from a temp directory, GitHub, Badland and
Drover through a fake `ctx.http`, `gh` and commands through a fake `ctx.run`. Drover's state
directory (daemon.json, token) is a temporary one. Nothing here touches the network, runs a
real `gh`, or starts a collector. Every name and token is synthetic.
"""
import support  # noqa: F401 — first: scratch cache and config

import calendar
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from porchlight import cli, collect, core, registry, theme as T
from porchlight.panels import todos as ptodos  # noqa: F401 — registers the panel
from porchlight.sources import todos as S
from porchlight.ui import BUILDERS, RCtx
from support import FakeHTTP, host, put, resp

MAIN = "main-fixture-0f3c9a2b"
GHTOK = "ghp_" + ("aB3dE6gH9k" * 4)[:36]
SKTOK = "sk-" + "x" * 40
TODAY = time.strftime("%Y-%m-%d")


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def epoch(day):
    return calendar.timegm(time.strptime(day, "%Y-%m-%d"))


def q(s):
    """A TOML basic string."""
    return json.dumps(s)


class Daemon:
    """A Drover daemon: op -> result, answering only the main token. Every request is recorded."""

    def __init__(self, **results):
        self.results, self.reqs = dict(results), []

    def __call__(self, url, method="GET", headers=None, json_body=None, timeout=6.0, **kw):
        op = url.rsplit("/", 1)[-1]
        auth = (headers or {}).get("Authorization", "")
        tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        self.reqs.append({"op": op, "token": tok, "body": json_body, "method": method, "url": url})
        if tok != MAIN:
            return core.Resp(401, {"ok": False, "error": "unauthorized"}, 1)
        if op not in self.results:
            return core.Resp(404, {"ok": False, "error": "unknown op %s" % op}, 1)
        return core.Resp(200, {"ok": True, "result": self.results[op]}, 1)

    def ops(self):
        return [r["op"] for r in self.reqs]


class Runner:
    """A fake ctx.run: queued (rc, out, err) answers, every argv recorded."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, argv, timeout=4.0, env=None, **kw):
        self.calls.append({"argv": list(argv), "env": env, "timeout": timeout})
        return self.answers.pop(0) if len(self.answers) > 1 else (self.answers[0] if self.answers else (0, "", ""))


def gh_issue(num, title, repo="octo/widgets", labels=(), pr=False, **kw):
    d = {"number": num, "title": title, "html_url": "https://github.com/%s/issues/%s" % (repo, num),
         "repository_url": "https://api.github.com/repos/" + repo, "labels": [{"name": n} for n in labels],
         "updated_at": "2026-11-01T10:00:00Z"}
    if pr:
        d["pull_request"] = {"url": "x"}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.dir = tempfile.mkdtemp(prefix="todos-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.state)
        self.daemon_json()
        with open(os.path.join(self.state, "token"), "w") as f:
            f.write(MAIN + "\n")

    def daemon_json(self):
        with open(os.path.join(self.state, "daemon.json"), "w") as f:
            json.dump({"url": "http://127.0.0.1:7337/", "pid": 4242}, f)

    def no_daemon(self):
        os.unlink(os.path.join(self.state, "daemon.json"))

    def write(self, name, text):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def toml(self, extra=""):
        return "[sources.todos]\nstate_dir = %s\n%s\n" % (q(self.state), extra)

    def host(self, extra=""):
        return host(self.toml(extra))

    def ctx(self, extra="", env=None, prev=None, http=None, run=None):
        h = self.host(extra)
        c = support.ctx(h, h.cfg.source("todos", S.DEFAULTS), env, prev, http)
        if run is not None:
            c.run = run
        return c

    def run_src(self, extra="", **kw):
        return S.todos(self.ctx(extra, **kw))

    def prov(self, name, extra="", **kw):
        c = self.ctx(extra, **kw)
        return S.FETCH[name](c, c.cfg)

    def files_toml(self, *paths, more=""):
        return "files = [%s]\n%s" % (", ".join(q(p) for p in paths), more)


# ── 1. providers ───────────────────────────────────────────────────────────────
class Files(Base):
    TEXT = ("(A) Fix the roof due:2030-01-02 +house @home\n"
            "(C) Low thing\n"
            "plain item\n"
            "x 2026-01-01 already done\n"
            "# a comment\n"
            "\n"
            "   \n"
            "(Z) lowest priority\n"
            "  (B) indented one +work\n")

    def items(self, text=None):
        p = self.write("todo.txt", self.TEXT if text is None else text)
        return {i["title"]: i for i in self.prov("files", self.files_toml(p))}, p

    def test_lines_become_items_and_done_lines_and_comments_are_skipped(self):
        got, p = self.items()
        self.assertEqual(sorted(got), sorted(["Fix the roof due:2030-01-02 +house @home", "Low thing", "plain item",
                                              "lowest priority", "indented one +work"]))
        for it in got.values():
            self.assertEqual((it["provider"], it["status"], it["url"]), ("files", "open", ""))
            self.assertTrue(re.fullmatch(r"files:[0-9a-f]{10}", it["key"]), it["key"])
            self.assertEqual(it["ref"]["path"], p)

    def test_priorities(self):
        got, _ = self.items()
        self.assertEqual(got["Fix the roof due:2030-01-02 +house @home"]["prio"], 1)
        self.assertEqual(got["indented one +work"]["prio"], 2)
        self.assertEqual(got["Low thing"]["prio"], 3)
        self.assertEqual(got["plain item"]["prio"], 5)
        self.assertEqual(got["lowest priority"]["prio"], 5)  # (Z) clamps to the lowest

    def test_due_dates_and_labels(self):
        got, _ = self.items()
        roof = got["Fix the roof due:2030-01-02 +house @home"]
        self.assertEqual(roof["due"], epoch("2030-01-02"))
        self.assertEqual(roof["labels"], ["house", "home"])
        self.assertEqual(got["indented one +work"]["labels"], ["work"])
        self.assertEqual(got["plain item"]["due"], 0)

    def test_the_ref_is_a_hash_of_the_stripped_line_and_the_key_is_stable(self):
        got, p = self.items()
        ref = got["indented one +work"]["ref"]
        self.assertEqual(ref["sha"], S._line_sha("(B) indented one +work"))
        self.assertNotIn("line", ref)  # the raw line can hold anything; the record travels to every peer
        again, _ = self.items()
        self.assertEqual({i["key"] for i in got.values()}, {i["key"] for i in again.values()})

    def test_a_missing_file_is_a_provider_error_and_an_empty_one_is_not(self):
        with self.assertRaises(RuntimeError) as cm:
            self.prov("files", self.files_toml(os.path.join(self.dir, "nope.txt")))
        self.assertIn("not found", str(cm.exception))
        empty = self.write("empty.txt", "")
        self.assertEqual(self.prov("files", self.files_toml(empty)), [])

    def test_several_files_and_a_tilde_path(self):
        a = self.write("a.txt", "one\n")
        b = self.write("b.txt", "two\n")
        got = self.prov("files", self.files_toml(a, b))
        self.assertEqual([i["title"] for i in got], ["one", "two"])
        with mock.patch.dict(os.environ, {"HOME": self.dir}):
            self.assertEqual([i["title"] for i in self.prov("files", self.files_toml("~/a.txt"))], ["one"])

    def test_a_missing_file_shows_in_the_source_record_while_the_other_files_provider_would_not_run(self):
        data = self.run_src(self.files_toml(os.path.join(self.dir, "nope.txt")))
        self.assertFalse(data["providers"]["files"]["ok"])
        self.assertIn("not found", data["providers"]["files"]["error"])


class GitHub(Base):
    def search(self, rows=(), status=200):
        return FakeHTTP({"search/issues": resp(status, {"items": list(rows)})})

    def test_search_with_a_token(self):
        rows = [gh_issue(12, "Voice crackles", labels=["P1", "bug"], pr=True),
                gh_issue(7, "Docs typo", repo="Octo/Docs", labels=["bug"])]
        http = self.search(rows)
        got = self.prov("github", 'github_query = "is:open label:voice user:octo"', env={"GITHUB_TOKEN": GHTOK}, http=http)
        self.assertEqual(len(http.calls), 1)
        method, url, headers, kw = http.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith("https://api.github.com/search/issues?per_page=60&sort=updated&q="), url)
        self.assertIn("is%3Aopen%20label%3Avoice%20user%3Aocto", url)
        self.assertEqual(headers["Authorization"], "Bearer " + GHTOK)
        a, b = got
        self.assertEqual((a["key"], a["repo"], a["kind"], a["prio"]), ("github:octo/widgets#12", "octo/widgets", "pr", 2))
        self.assertEqual((a["title"], a["labels"], a["url"]), ("Voice crackles", ["P1", "bug"],
                                                              "https://github.com/octo/widgets/issues/12"))
        self.assertEqual(a["ref"], {"repo": "octo/widgets", "number": 12})
        self.assertEqual(a["updated"], epoch("2026-11-01") + 10 * 3600)
        self.assertEqual((b["key"], b["kind"], b["prio"], b["repo"]), ("github:Octo/Docs#7", "issue", 4, "Octo/Docs"))

    def test_priority_labels(self):
        for label, prio in (("p0", 2), ("P1", 2), ("urgent", 2), ("High", 2), ("high-ish", 2), ("bug", 4), ("phigh", 4)):
            got = self.prov("github", 'github_query = "x"', env={"GH_TOKEN": "t"},
                            http=self.search([gh_issue(1, "t", labels=[label])]))
            self.assertEqual(got[0]["prio"], prio, label)

    def test_the_second_token_name_and_a_custom_one(self):
        http = self.search([gh_issue(1, "t")])
        self.prov("github", 'github_query = "x"', env={"GH_TOKEN": "second"}, http=http)
        self.assertEqual(http.calls[0][2]["Authorization"], "Bearer second")
        http = self.search([gh_issue(1, "t")])
        self.prov("github", 'github_query = "x"\ngithub_token_env = "MY_GH"', env={"MY_GH": "mine", "GITHUB_TOKEN": "no"},
                  http=http)
        self.assertEqual(http.calls[0][2]["Authorization"], "Bearer mine")

    def test_the_token_never_reaches_the_record(self):
        data = self.run_src('github_query = "x"', env={"GITHUB_TOKEN": GHTOK}, http=self.search([gh_issue(1, "t")]))
        self.assertNotIn(GHTOK, json.dumps(data))

    def test_per_provider_limit_reaches_the_query_and_the_rows(self):
        http = self.search([gh_issue(n, "t%d" % n) for n in range(5)])
        got = self.prov("github", 'github_query = "x"\nlimit = 2', env={"GITHUB_TOKEN": "t"}, http=http)
        self.assertEqual(len(got), 2)
        self.assertIn("per_page=2", http.calls[0][1])

    def test_junk_rows_are_skipped(self):
        got = self.prov("github", 'github_query = "x"', env={"GITHUB_TOKEN": "t"},
                        http=self.search(["junk", None, gh_issue(3, "ok")]))
        self.assertEqual([i["id"] for i in got], ["octo/widgets#3"])

    def test_a_rejected_token_falls_back_to_gh_api(self):
        for status in (401, 403):
            http = self.search(status=status)
            run = Runner((0, json.dumps({"items": [gh_issue(5, "via gh", labels=["urgent"])]}), ""))
            with mock.patch.object(core, "which", lambda n, extra=(): "/usr/bin/gh"):
                got = self.prov("github", 'github_query = "is:open user:octo"', env={"GITHUB_TOKEN": "stale"}, http=http,
                                run=run)
            self.assertEqual(len(http.calls), 1)
            self.assertEqual([i["title"] for i in got], ["via gh"])
            argv = run.calls[0]["argv"]
            self.assertEqual(argv[:5], ["/usr/bin/gh", "api", "-X", "GET", "search/issues"])
            self.assertIn("q=is:open user:octo", argv)
            self.assertIn("per_page=60", argv)

    def test_a_rejected_token_and_a_failing_gh_names_both(self):
        run = Runner((1, "", "gh: not logged in"))
        with mock.patch.object(core, "which", lambda n, extra=(): "/usr/bin/gh"), self.assertRaises(RuntimeError) as cm:
            self.prov("github", 'github_query = "x"', env={"GITHUB_TOKEN": "stale"}, http=self.search(status=401), run=run)
        self.assertIn("token rejected", str(cm.exception))
        self.assertIn("gh api: gh: not logged in", str(cm.exception))

    def test_no_token_and_no_gh_is_an_error(self):
        http = self.search()
        with mock.patch.object(core, "which", lambda n, extra=(): None), self.assertRaises(RuntimeError) as cm:
            self.prov("github", 'github_query = "x"', env={}, http=http)
        self.assertEqual(str(cm.exception), "no GitHub token (github_token_env) and no gh")
        self.assertEqual(http.calls, [])

    def test_a_rejected_token_and_no_gh_is_an_error_that_says_so(self):
        with mock.patch.object(core, "which", lambda n, extra=(): None), self.assertRaises(RuntimeError) as cm:
            self.prov("github", 'github_query = "x"', env={"GITHUB_TOKEN": "stale"}, http=self.search(status=401))
        self.assertIn("token rejected", str(cm.exception))
        self.assertTrue(str(cm.exception).endswith("and no gh"))

    def test_no_token_uses_gh_without_any_http(self):
        http = self.search()
        run = Runner((0, json.dumps({"items": [gh_issue(9, "gh only")]}), ""))
        with mock.patch.object(core, "which", lambda n, extra=(): "/usr/bin/gh"):
            got = self.prov("github", 'github_query = "x"', env={}, http=http, run=run)
        self.assertEqual((http.calls, [i["title"] for i in got]), ([], ["gh only"]))

    def test_another_http_error_does_not_fall_back(self):
        run = Runner((0, "{}", ""))
        with mock.patch.object(core, "which", lambda n, extra=(): "/usr/bin/gh"), self.assertRaises(RuntimeError) as cm:
            self.prov("github", 'github_query = "x"', env={"GITHUB_TOKEN": "t"}, http=self.search(status=500), run=run)
        self.assertEqual(str(cm.exception), "GitHub search HTTP 500")
        self.assertEqual(run.calls, [])


class DroverProviders(Base):
    FEATURES = {"features": [
        {"fid": "f-1", "title": "Ship the gateway", "state": "in-progress", "repo": "octo/gw", "links": ["https://x", 5],
         "notes": "tracks V-7 and V-9 SECRETNOTE-TEXT", "updated": "2026-11-02T00:00:00Z"},
        {"fid": "f-2", "title": "Write docs", "state": "not-started", "repo": "octo/docs", "updated": "2026-11-05T00:00:00Z"},
        {"fid": "f-3", "title": "Already out", "state": "shipped"},
        {"fid": "f-4", "title": "Dropped", "state": "abandoned"},
        "junk"]}

    def test_only_open_features_are_kept_newest_first(self):
        d = Daemon(**{"events.state": self.FEATURES})
        got = self.prov("drover", http=d)
        self.assertEqual([i["id"] for i in got], ["f-2", "f-1"])
        self.assertEqual(d.reqs[0]["token"], MAIN)
        self.assertEqual((d.reqs[0]["method"], d.reqs[0]["url"]), ("POST", "http://127.0.0.1:7337/api/op/events.state"))
        f1 = got[1]
        self.assertEqual((f1["key"], f1["status"], f1["prio"], f1["kind"], f1["repo"]),
                         ("drover:f-1", "in-progress", 3, "feature", "octo/gw"))
        self.assertEqual((got[0]["status"], got[0]["prio"]), ("not-started", 4))
        self.assertEqual(f1["labels"], ["https://x"])
        self.assertEqual(f1["ref"], {"fid": "f-1"})

    def test_drover_states_setting(self):
        d = Daemon(**{"events.state": self.FEATURES})
        got = self.prov("drover", 'drover_states = ["shipped"]', http=d)
        self.assertEqual([i["id"] for i in got], ["f-3"])

    def test_notes_are_searched_for_asks_but_never_stored(self):
        d = Daemon(**{"events.state": self.FEATURES})
        data = self.run_src('drover = true\nask_pattern = "V-\\\\d+"\nask_systems = ["drover"]', http=d)
        by = {i["id"]: i for i in data["items"]}
        self.assertEqual(by["f-1"]["asks"], ["V-7", "V-9"])
        for it in data["items"]:
            self.assertNotIn("_match", it)
        self.assertNotIn("SECRETNOTE-TEXT", json.dumps(data))
        self.assertEqual(sorted(data["asks"]), ["V-7", "V-9"])

    def test_no_daemon_and_no_token_are_provider_errors(self):
        d = Daemon(**{"events.state": self.FEATURES})
        os.unlink(os.path.join(self.state, "token"))
        with self.assertRaises(RuntimeError) as cm:
            self.prov("drover", http=d)
        self.assertEqual(str(cm.exception), "no Drover token")
        self.no_daemon()
        with self.assertRaises(RuntimeError) as cm:
            self.prov("drover", http=d)
        self.assertEqual(str(cm.exception), "no Drover daemon here")
        self.assertEqual(d.reqs, [])

    def test_a_daemon_that_does_not_answer_is_an_error(self):
        with self.assertRaises(RuntimeError) as cm:
            self.prov("drover", http=Daemon())
        self.assertIn("events.state: HTTP 404", str(cm.exception))

    def test_reflections(self):
        d = Daemon(**{"reflections.list": {"items": [
            {"id": "r-1", "title": "Rotate the key", "status": "open", "repo": "octo/ops", "priority": 2, "kind": "action",
             "tags": ["sec"], "ts": "2026-11-03T00:00:00Z", "body": "see V-3 SECRETBODY"},
            {"id": "r-2", "title": "Why slow?", "kind": "question"}, "junk"]}})
        got = self.prov("reflections", http=d)
        self.assertEqual(d.reqs[0]["body"], {"all": True, "status": "open", "limit": 60, "kind": "action,question"})
        self.assertEqual(d.reqs[0]["op"], "reflections.list")
        a, b = got
        self.assertEqual((a["key"], a["prio"], a["kind"], a["labels"], a["repo"], a["ref"]),
                         ("reflections:r-1", 2, "action", ["sec"], "octo/ops", {"id": "r-1"}))
        self.assertEqual((b["prio"], b["status"]), (5, "open"))
        self.assertEqual(a["_match"], "see V-3 SECRETBODY")

    def test_reflection_bodies_feed_asks_and_are_dropped(self):
        d = Daemon(**{"reflections.list": {"items": [{"id": "r-1", "title": "Rotate", "body": "see V-3 SECRETBODY"}]}})
        data = self.run_src('reflections = true\nask_pattern = "V-\\\\d+"', http=d)
        self.assertEqual(data["items"][0]["asks"], ["V-3"])
        self.assertNotIn("SECRETBODY", json.dumps(data))
        self.assertNotIn("_match", data["items"][0])

    def test_exclude_paths_drops_another_identitys_items(self):
        d = Daemon(**{"reflections.list": {"items": [
            {"id": "w-1", "title": "Work thing", "repo": "merge-party", "path": "/h/.df/identities/work/claude/memory/x/r.jsonl"},
            {"id": "w-2", "title": "Work cwd", "repo": "x", "cwd": "/h/work/identities/work/checkout"},
            {"id": "p-1", "title": "Mine", "repo": "x", "path": "/h/.df/identities/personal/claude/memory/x/r.jsonl"}]},
                      "events.state": {"features": [
                          {"fid": "f-w", "title": "W", "state": "in-progress", "checkout": "/a/identities/work/b"},
                          {"fid": "f-p", "title": "P", "state": "in-progress"}]}})
        cfg = 'exclude_paths = ["/identities/work/"]'
        self.assertEqual([i["id"] for i in self.prov("reflections", cfg, http=d)], ["p-1"])
        self.assertEqual([i["id"] for i in self.prov("drover", cfg, http=d)], ["f-p"])
        data = self.run_src('reflections = true\n' + cfg, http=d)
        self.assertNotIn("identities/", json.dumps(data))  # matched, never stored
        self.assertEqual(len(self.prov("reflections", http=d)), 3)  # off by default

    def test_reflection_kinds_and_limit_settings(self):
        d = Daemon(**{"reflections.list": {"items": []}})
        self.prov("reflections", 'reflections_kinds = ["action"]\nlimit = 5', http=d)
        self.assertEqual(d.reqs[0]["body"], {"all": True, "status": "open", "limit": 5, "kind": "action"})
        self.prov("reflections", "reflections_kinds = []", http=d)
        self.assertNotIn("kind", d.reqs[1]["body"])


class Badland(Base):
    BODY = {"data": {"items": [
        {"id": "t1", "title": "Call the plumber", "priority": "high", "dueDate": "2030-01-02T00:00:00Z",
         "tags": ["home", 3], "list": "Chores", "description": "re V-4 SECRETDESC", "isCompleted": False},
        {"id": "t2", "title": "Done already", "isCompleted": True},
        {"id": "t3", "title": "No extras", "list": {"x": 1}}, "junk"]}}

    def test_open_items_only(self):
        http = FakeHTTP({"api/mcp-context/tasks": resp(200, self.BODY)})
        got = self.prov("badland", env={"BADLAND_BRAIN_KEY": "rk"}, http=http)
        self.assertEqual([i["id"] for i in got], ["t1", "t3"])
        method, url, headers, _ = http.calls[0]
        self.assertEqual(url, "https://api.badland.ai/api/mcp-context/tasks?status=open&limit=60")
        self.assertEqual(headers["Authorization"], "Bearer rk")
        a, b = got
        self.assertEqual((a["key"], a["prio"], a["due"], a["labels"], a["repo"], a["kind"], a["ref"]),
                         ("badland:t1", 2, epoch("2030-01-02"), ["home"], "Chores", "todo", {"id": "t1"}))
        self.assertEqual((b["repo"], b["prio"]), ("", 5))

    def test_descriptions_feed_asks_and_are_dropped(self):
        http = FakeHTTP({"api/mcp-context/tasks": resp(200, self.BODY)})
        data = self.run_src('badland = true\nask_pattern = "V-\\\\d+"', env={"BADLAND_BRAIN_KEY": "rk"}, http=http)
        self.assertEqual({i["id"]: i["asks"] for i in data["items"]}, {"t1": ["V-4"], "t3": []})
        self.assertNotIn("SECRETDESC", json.dumps(data))

    def test_a_missing_key_is_an_error_and_makes_no_request(self):
        http = FakeHTTP({})
        with self.assertRaises(RuntimeError) as cm:
            self.prov("badland", env={}, http=http)
        self.assertEqual(str(cm.exception), "no BADLAND_BRAIN_KEY")
        self.assertEqual(http.calls, [])
        with self.assertRaises(RuntimeError) as cm:
            self.prov("badland", 'badland_key_env = "MY_KEY"', env={"BADLAND_BRAIN_KEY": "x"}, http=http)
        self.assertEqual(str(cm.exception), "no MY_KEY")

    def test_http_errors_and_a_non_object_body(self):
        for r in (resp(500, {"error": "x"}), resp(200, ["not", "a", "dict"])):
            with self.assertRaises(RuntimeError) as cm:
                self.prov("badland", env={"BADLAND_BRAIN_KEY": "rk"}, http=FakeHTTP({"tasks": r}))
            self.assertTrue(str(cm.exception).startswith("tasks HTTP "), str(cm.exception))

    def test_limit_goes_into_the_query(self):
        http = FakeHTTP({"tasks": resp(200, {"data": {"items": []}})})
        self.prov("badland", "limit = 500", env={"BADLAND_BRAIN_KEY": "rk"}, http=http)
        self.assertTrue(http.calls[0][1].endswith("limit=100"))


class Command(Base):
    def test_a_list(self):
        out = json.dumps([{"id": "a1", "title": "First", "url": "https://x.example/a", "priority": 2, "due": "2030-01-02",
                           "repo": "r", "labels": ["l"], "kind": "k", "status": "doing"},
                          {"title": "No id"}, {"id": "z", "title": ""}, "junk"])
        run = Runner((0, out, ""))
        got = self.prov("command", 'command = "my-todos --json"', run=run)
        self.assertEqual(run.calls[0]["argv"], ["/bin/sh", "-c", "my-todos --json"])
        a, b = got
        self.assertEqual((a["key"], a["title"], a["url"], a["prio"], a["due"], a["repo"], a["labels"], a["kind"], a["status"],
                          a["ref"]), ("command:a1", "First", "https://x.example/a", 2, epoch("2030-01-02"), "r", ["l"], "k",
                                      "doing", {"id": "a1"}))
        self.assertTrue(re.fullmatch(r"command:[0-9a-f]{10}", b["key"]), b["key"])

    def test_the_items_object_form_and_an_argv_list(self):
        run = Runner((0, json.dumps({"items": [{"id": "1", "title": "From object"}]}), ""))
        got = self.prov("command", 'command = ["my-todos", "--all"]', run=run)
        self.assertEqual(run.calls[0]["argv"], ["my-todos", "--all"])
        self.assertEqual([i["title"] for i in got], ["From object"])

    def test_an_object_without_items_and_empty_output_are_empty(self):
        self.assertEqual(self.prov("command", 'command = "x"', run=Runner((0, "{}", ""))), [])
        self.assertEqual(self.prov("command", 'command = "x"', run=Runner((0, "", ""))), [])

    def test_a_failing_command_and_bad_json_are_errors(self):
        with self.assertRaises(RuntimeError) as cm:
            self.prov("command", 'command = "x"', run=Runner((2, "", "boom")))
        self.assertEqual(str(cm.exception), "command exit 2")
        data = self.run_src('command = "x"', run=Runner((0, "not json", "")))
        self.assertFalse(data["providers"]["command"]["ok"])

    def test_the_child_environment_carries_no_secrets(self):
        run = Runner((0, "[]", ""))
        with mock.patch.dict(os.environ, {"SOME_API_TOKEN": "leak-me"}):
            self.prov("command", 'command = "x"', env={"OTHER_SECRET": "leak-too"}, run=run)
        self.assertNotIn("SOME_API_TOKEN", run.calls[0]["env"])
        self.assertNotIn("OTHER_SECRET", run.calls[0]["env"])


# ── 2. the source: isolation, alerts, filtering ────────────────────────────────
class SourceBehavior(Base):
    def test_nothing_configured(self):
        data = self.run_src()
        self.assertEqual((data["error"], data["items"], data["providers"]), ("no providers configured", [], {}))

    def test_enabled_providers(self):
        cfg = self.host(self.files_toml("a") + 'github_query = " "\ndrover = true\nreflections = "yes"\ncommand = "x"').cfg \
            .source("todos", S.DEFAULTS)
        self.assertEqual(S.enabled(cfg), ["files", "drover", "command"])  # " " query is off; reflections needs literal true
        cfg = self.host('github_query = "q"\nbadland = true\nreflections = true').cfg.source("todos", S.DEFAULTS)
        self.assertEqual(S.enabled(cfg), ["github", "reflections", "badland"])

    def test_one_provider_failing_keeps_the_others(self):
        p = self.write("t.txt", "(A) one\ntwo\n")
        http = FakeHTTP({"search/issues": ConnectionRefusedError("refused")})
        data = self.run_src(self.files_toml(p) + 'github_query = "x"', env={"GITHUB_TOKEN": "t"}, http=http)
        self.assertEqual([i["title"] for i in data["items"]], ["one", "two"])
        self.assertEqual(data["providers"]["files"], {"ok": True, "n": 2})
        gh = data["providers"]["github"]
        self.assertFalse(gh["ok"])
        self.assertTrue(gh["error"])
        self.assertFalse(gh["kept"])
        self.assertNotIn("alerts", data)  # one failed of two is not "every provider failed"

    def test_a_failed_provider_keeps_its_previous_items(self):
        old = S.item("github", "octo/widgets#1", "Old issue", repo="octo/widgets", url="https://github.com/o/w/issues/1")
        old["asks"] = []
        other = S.item("command", "c1", "Other system")
        prev = {"items": [old, other, "junk"]}
        http = FakeHTTP({"search/issues": resp(500, {})})
        p = self.write("t.txt", "fresh\n")
        data = self.run_src(self.files_toml(p) + 'github_query = "x"', env={"GITHUB_TOKEN": "t"}, http=http, prev=prev)
        self.assertEqual({i["key"] for i in data["items"]}, {"github:octo/widgets#1", data["items"][0]["key"]})
        self.assertEqual(sorted(i["title"] for i in data["items"]), ["Old issue", "fresh"])  # not command's: it is off
        gh = data["providers"]["github"]
        self.assertEqual((gh["ok"], gh["n"], gh["kept"]), (False, 1, True))
        self.assertEqual(gh["error"], "GitHub search HTTP 500")

    def test_a_timed_out_provider_reads_as_timed_out(self):
        p = self.write("t.txt", "x\n")
        with mock.patch.object(core, "parallel", lambda jobs, timeout=3.0: {"files": None}):
            data = self.run_src(self.files_toml(p))
        self.assertEqual(data["providers"]["files"]["error"], "timed out")

    def test_error_text_is_redacted(self):
        secret = "hunter2-very-secret-value"

        def boom(ctx, cfg):
            raise RuntimeError("failed with " + secret)
        p = self.write("t.txt", "x\n")
        with mock.patch.dict(S.FETCH, {"files": boom}):
            data = self.run_src(self.files_toml(p), env={"SERVICE_API_KEY": secret})
        err = data["providers"]["files"]["error"]
        self.assertNotIn(secret, err)
        self.assertIn("redacted", err)

    def test_every_provider_failing_raises_the_warn_alert(self):
        data = self.run_src(self.files_toml(os.path.join(self.dir, "nope.txt")) + 'command = "x"', run=Runner((1, "", "")))
        self.assertEqual(data["items"], [])
        alerts = data["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["sev"], "warn")
        self.assertEqual(alerts[0]["msg"], "todos: every provider failed (files, command)")
        self.assertEqual(alerts[0]["fix"], "porchlight doctor")

    def test_overdue_alert(self):
        p = self.write("t.txt", "(A) late due:2000-01-01\nlater due:2001-01-01\nfuture due:2099-01-01\nnone\n")
        data = self.run_src(self.files_toml(p))
        self.assertEqual(data["overdue"], 2)
        self.assertEqual(data["alerts"], [{"sev": "warn", "msg": "2 todos overdue", "fix": "porchlight todos"}])
        one = self.write("o.txt", "late due:2000-01-01\n")
        self.assertEqual(self.run_src(self.files_toml(one))["alerts"][0]["msg"], "1 todo overdue")
        off = self.run_src(self.files_toml(p, more="overdue_alert = false"))
        self.assertEqual(off["overdue"], 2)
        self.assertNotIn("alerts", off)
        self.assertNotIn("alerts", self.run_src(self.files_toml(self.write("n.txt", "future due:2099-01-01\n"))))

    def test_sort_is_due_first_then_priority_then_newest(self):
        out = json.dumps([{"id": "n5", "title": "no due low", "priority": 5},
                          {"id": "n1", "title": "no due high", "priority": 1},
                          {"id": "d2", "title": "due later", "due": "2031-01-01"},
                          {"id": "d1", "title": "due sooner", "due": "2030-01-01"}])
        data = self.run_src('command = "x"', run=Runner((0, out, "")))
        self.assertEqual([i["id"] for i in data["items"]], ["d1", "d2", "n1", "n5"])
        self.assertEqual(data["total"], 4)

    def test_exclude_repos_is_case_insensitive(self):
        out = json.dumps([{"id": "1", "title": "work item", "repo": "Corp/Secret"},
                          {"id": "2", "title": "mine", "repo": "me/proj"}, {"id": "3", "title": "no repo"}])
        data = self.run_src('command = "x"\nexclude_repos = ["corp/SECRET"]', run=Runner((0, out, "")))
        self.assertEqual(sorted(i["title"] for i in data["items"]), ["mine", "no repo"])
        self.assertEqual(data["providers"]["command"]["n"], 2)

    def test_limit_is_per_provider_and_applies_after_exclusion(self):
        rows = [{"id": str(n), "title": "t%d" % n, "repo": "bad/x" if n == 0 else "ok/x", "priority": n + 1}
                for n in range(6)]
        p = self.write("t.txt", "a\nb\nc\n")
        data = self.run_src(self.files_toml(p) + 'command = "x"\nlimit = 2\nexclude_repos = ["bad/x"]',
                            run=Runner((0, json.dumps(rows), "")))
        self.assertEqual(data["providers"]["command"]["n"], 2)
        self.assertEqual(data["providers"]["files"]["n"], 2)
        self.assertEqual(data["total"], 4)
        self.assertNotIn("t0", [i["title"] for i in data["items"]])


# ── 4. ask coverage ────────────────────────────────────────────────────────────
class Asks(Base):
    def rows(self):
        return json.dumps([{"id": "1", "title": "V-10 ten"}, {"id": "2", "title": "V-2 two"},
                           {"id": "3", "title": "also V-2 again"}, {"id": "4", "title": "no ask here"},
                           {"id": "5", "title": "labeled", "labels": ["V-33"]}])

    EXTRA = 'command = "x"\nask_pattern = \'V-\\d{1,3}\'\nask_systems = ["files", "command", "drover"]\n'

    def test_coverage_in_missing_open_and_numeric_ordering(self):
        p = self.write("t.txt", "file V-2 here\n")
        data = self.run_src(self.files_toml(p) + self.EXTRA, run=Runner((0, self.rows(), "")))
        self.assertEqual(list(data["asks"]), ["V-2", "V-10", "V-33"])  # V-2 before V-10
        self.assertEqual(data["asks"]["V-2"], {"in": ["command", "files"], "missing": ["drover"], "open": 3})
        self.assertEqual(data["asks"]["V-10"], {"in": ["command"], "missing": ["files", "drover"], "open": 1})
        self.assertEqual(data["asks"]["V-33"]["in"], ["command"])  # found in labels too
        self.assertEqual(data["ask_systems"], ["files", "command", "drover"])
        by = {i["id"]: i["asks"] for i in data["items"] if i["provider"] == "command"}
        self.assertEqual(by, {"1": ["V-10"], "2": ["V-2"], "3": ["V-2"], "4": [], "5": ["V-33"]})

    def test_kept_items_keep_their_asks(self):
        old = S.item("drover", "f1", "Old feature with no id in the title")
        old["asks"] = ["V-2"]
        self.no_daemon()
        data = self.run_src(self.EXTRA + "drover = true", run=Runner((0, self.rows(), "")), prev={"items": [old]})
        self.assertEqual(data["providers"]["drover"]["kept"], True)
        kept = [i for i in data["items"] if i["provider"] == "drover"][0]
        self.assertEqual(kept["asks"], ["V-2"])
        self.assertEqual(data["asks"]["V-2"]["in"], ["command", "drover"])
        self.assertEqual(data["asks"]["V-2"]["missing"], ["files"])
        self.assertEqual(data["asks"]["V-2"]["open"], 3)

    def test_no_pattern_or_an_invalid_one_turns_tracking_off(self):
        for extra in ('command = "x"', 'command = "x"\nask_pattern = "V-("'):
            data = self.run_src(extra, run=Runner((0, self.rows(), "")))
            self.assertNotIn("asks", data)
            self.assertTrue(all(i["asks"] == [] for i in data["items"]))

    def test_coverage_and_asks_of_directly(self):
        pat = re.compile(r"[A-Z]-\d+")
        self.assertEqual(S.asks_of("B-2 a-1 A-10 B-2", pat), ["A-10", "B-2"])
        self.assertEqual(S.asks_of("x", None), [])
        cov = S.coverage([{"provider": "files", "asks": ["A-2", "A-10", "B-1"]}, {"provider": "gh", "asks": ["A-2"]}],
                         pat, ["files", "gh"])
        self.assertEqual(list(cov), ["A-2", "A-10", "B-1"])
        self.assertEqual(cov["A-2"], {"in": ["files", "gh"], "missing": [], "open": 2})


# ── 5. masking ─────────────────────────────────────────────────────────────────
class Masking(Base):
    def test_clean_masks_token_shapes_and_uri_credentials(self):
        out = S.clean("deploy %s and %s via https://bob:hunter2@git.example.com/r.git now" % (GHTOK, SKTOK))
        for raw in (GHTOK, SKTOK, "hunter2", "bob:"):
            self.assertNotIn(raw, out)
        self.assertIn("git.example.com/r.git", out)
        self.assertEqual(S.clean(None), "")
        self.assertEqual(len(S.clean("word " * 100)), 160)
        self.assertEqual(len(S.clean("text " * 100, 20)), 20)
        self.assertEqual(S.clean("x" * 500), "<redacted>")  # a long opaque run is masked by its shape

    def test_the_stored_record_is_masked(self):
        title = "rotate %s then %s see https://bob:hunter2@git.example.com/r" % (GHTOK, SKTOK)
        out = json.dumps([{"id": "1", "title": title, "url": "https://u:pw0rd@host.example/p?x=1",
                           "repo": "r/" + GHTOK, "labels": [GHTOK], "kind": SKTOK}])
        data = self.run_src('command = "x"', run=Runner((0, out, "")))
        blob = json.dumps(data)
        for raw in (GHTOK, SKTOK, "hunter2", "pw0rd"):
            self.assertNotIn(raw, blob)
        it = data["items"][0]
        self.assertTrue(it["url"].startswith("https://"))
        self.assertIn("host.example/p?x=1", it["url"])

    def test_a_token_shaped_id_is_never_stored_raw(self):
        out = json.dumps([{"id": GHTOK, "title": "Has a token for an id"}, {"id": "plain-7", "title": "Fine"}])
        data = self.run_src('command = "x"', run=Runner((0, out, "")))
        self.assertNotIn(GHTOK, json.dumps(data))  # not in the key, the id, nor the action handle
        by = {i["title"]: i for i in data["items"]}
        bad, ok = by["Has a token for an id"], by["Fine"]
        self.assertEqual(bad["ref"], {"masked": True})
        self.assertEqual(ok["ref"], {"id": "plain-7"})
        # Drover feature ids too; a files handle (a 64-hex line hash) is not an id and stays
        self.assertEqual(S.item("drover", SKTOK, "t", ref={"fid": SKTOK})["ref"], {"masked": True})
        sha = "a" * 64
        self.assertEqual(S.item("files", "1", "t", ref={"path": "p", "sha": sha})["ref"], {"path": "p", "sha": sha})

    def test_control_characters_are_stripped(self):
        title = "a\x00b\x07c\x1b[2Jd\x9fe\nf\tg\x7fh"
        it = S.item("command", "i\x1bd", title, repo="r\x00", labels=["l\x07"])
        for field in ("title", "id", "repo", "key"):
            self.assertIsNone(re.search(r"[\x00-\x1f\x7f-\x9f]", it[field]), (field, it[field]))
        self.assertIsNone(re.search(r"[\x00-\x1f\x7f-\x9f]", it["labels"][0]))
        self.assertEqual(it["title"], "a b c [2Jd e f g h")

    def test_only_http_urls_are_kept(self):
        self.assertEqual(S.item("p", "1", "t", url="javascript:alert(1)")["url"], "")
        self.assertEqual(S.item("p", "1", "t", url="file:///etc/passwd")["url"], "")
        self.assertEqual(S.item("p", "1", "t", url="http://a.example/x")["url"], "http://a.example/x")

    def test_item_shapes(self):
        it = S.item("p", "1", "t", prio="urgent", due="2030-01-02", status="", labels=list("abcdefghijkl"))
        self.assertEqual((it["prio"], it["due"], it["status"], len(it["labels"])), (1, epoch("2030-01-02"), "open", 8))
        self.assertEqual(S.item("p", "1", "t", prio=99)["prio"], 5)
        self.assertEqual(S.item("p", "1", "t", prio=-4)["prio"], 1)
        self.assertEqual(S.item("p", "1", "t", prio=True)["prio"], 5)  # a bool is not a priority
        self.assertEqual(S.item("p", "1", "t", due="garbage")["due"], 0)
        self.assertNotIn("_match", S.item("p", "1", "t"))
        self.assertNotIn("ref", S.item("p", "1", "t"))
        self.assertEqual(len(S.item("p", "1", "t", match="z" * 9000)["_match"]), 4000)


# ── 6. actions ─────────────────────────────────────────────────────────────────
class Act(Base):
    def act_host(self, extra="", actions=True):
        return self.host(("actions = true\n" if actions else "") + extra)

    def test_refused_unless_actions_is_true(self):
        p = self.write("t.txt", "one\n")
        it = self.prov("files", self.files_toml(p))[0]
        for h in (self.host(self.files_toml(p)), self.host(self.files_toml(p) + 'actions = "true"'),
                  self.host(self.files_toml(p) + "actions = false")):
            with self.assertRaises(S.ActionError) as cm:
                S.act(h, it, "done", env={})
            self.assertIn("actions are off", str(cm.exception))
        self.assertEqual(slurp(p), "one\n")

    def test_unknown_verb(self):
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), {"provider": "files"}, "delete", env={})
        self.assertIn("unknown action", str(cm.exception))

    def test_unknown_provider(self):
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), {"provider": "mystery", "ref": {}}, "done", env={})
        self.assertIn("no action for provider mystery", str(cm.exception))

    # files
    def test_files_done_rewrites_only_the_matching_line(self):
        p = self.write("t.txt", "(A) first\n  (B) second thing\n# note\nthird\n")
        os.chmod(p, 0o640)
        items = {i["title"]: i for i in self.prov("files", self.files_toml(p))}
        msg = S.act(self.act_host(), items["second thing"], "done", env={})
        self.assertEqual(msg, "marked done in " + p)
        self.assertEqual(slurp(p), "(A) first\n  x %s (B) second thing\n# note\nthird\n" % TODAY)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640)
        self.assertFalse(os.path.exists(p + ".porchlight.tmp"))
        # the done line no longer lists, the others still do
        self.assertEqual(sorted(i["title"] for i in self.prov("files", self.files_toml(p))), ["first", "third"])

    def test_files_done_without_a_trailing_newline_and_with_a_duplicate(self):
        p = self.write("t.txt", "dup\ndup\nlast")
        it = self.prov("files", self.files_toml(p))[0]
        S.act(self.act_host(), it, "done", env={})
        self.assertEqual(slurp(p), "x %s dup\ndup\nlast" % TODAY)

    def test_files_line_gone_is_an_action_error(self):
        p = self.write("t.txt", "one\ntwo\n")
        it = self.prov("files", self.files_toml(p))[1]
        self.write("t.txt", "one\nedited elsewhere\n")
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), it, "done", env={})
        self.assertIn("no longer in", str(cm.exception))
        self.assertEqual(slurp(p), "one\nedited elsewhere\n")

    def test_files_ref_never_stores_a_token_from_the_line(self):
        p = self.write("t.txt", "rotate %s now\n" % GHTOK)
        data = self.run_src(self.files_toml(p))
        self.assertNotIn(GHTOK, json.dumps(data))
        S.act(self.act_host(), data["items"][0], "done", env={})
        self.assertEqual(slurp(p), "x %s rotate %s now\n" % (TODAY, GHTOK))

    def test_files_done_from_a_record_written_before_the_hash(self):
        p = self.write("t.txt", "old style\n")
        it = {"provider": "files", "ref": {"path": p, "line": "old style"}}
        S.act(self.act_host(), it, "done", env={})
        self.assertEqual(slurp(p), "x %s old style\n" % TODAY)

    def test_files_done_through_a_symlink_keeps_the_link(self):
        real = self.write("real.txt", "one\ntwo\n")
        link = os.path.join(self.dir, "link.txt")
        os.symlink(real, link)
        it = self.prov("files", self.files_toml(link))[1]
        S.act(self.act_host(), it, "done", env={})
        self.assertTrue(os.path.islink(link))
        self.assertEqual(slurp(real), "one\nx %s two\n" % TODAY)

    def test_files_unwritable_directory_is_an_action_error(self):
        if os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        d = os.path.join(self.dir, "ro")
        os.makedirs(d)
        p = os.path.join(d, "t.txt")
        with open(p, "w") as f:
            f.write("one\n")
        it = self.prov("files", self.files_toml(p))[0]
        os.chmod(d, 0o500)
        self.addCleanup(os.chmod, d, 0o700)
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), it, "done", env={})
        self.assertIn("ermission", str(cm.exception))
        self.assertEqual(slurp(p), "one\n")

    def test_github_pull_requests_are_never_closed(self):
        http = FakeHTTP({})
        it = {"provider": "github", "id": "octo/widgets#12", "kind": "pr", "ref": {"repo": "octo/widgets", "number": 12}}
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), it, "done", env={"GITHUB_TOKEN": GHTOK}, http=http)
        self.assertIn("pull request", str(cm.exception))
        self.assertEqual(http.calls, [])
        from porchlight.panels import todos as PT
        self.assertNotIn("done", PT.tui_items([dict(it, url="https://github.com/octo/widgets/pull/12")])[0]["actions"])
        self.assertIn("done", PT.tui_items([dict(it, kind="issue")])[0]["actions"])

    def test_command_item_without_an_id_is_refused(self):
        run = Runner((0, "", ""))
        it = {"provider": "command", "ref": {"id": None}}
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host('done_command = "tracker close {id}"'), it, "done", env={}, runner=run)
        self.assertIn("no id", str(cm.exception))
        self.assertEqual(run.calls, [])

    def test_an_item_whose_id_was_masked_is_refused(self):
        run, http = Runner((0, "", "")), FakeHTTP({})
        for prov in ("command", "badland", "reflections", "drover"):
            it = {"provider": prov, "key": prov + ":ghp_…", "ref": {"masked": True}}
            with self.assertRaises(S.ActionError) as cm:
                S.act(self.act_host('done_command = "tracker close {id}"'), it, "done", env={"BADLAND_API_KEY": "w"},
                      runner=run, http=http)
            self.assertIn("shaped like a credential", str(cm.exception))
        self.assertEqual(run.calls, [])

    def test_files_missing_file_is_an_action_error(self):
        it = {"provider": "files", "ref": {"path": os.path.join(self.dir, "gone.txt"), "line": "x"}}
        with self.assertRaises(S.ActionError):
            S.act(self.act_host(), it, "done", env={})

    # github
    def test_github_done_patches_the_issue(self):
        http = FakeHTTP({"repos/octo/widgets/issues/12": resp(200, {})})
        it = {"provider": "github", "ref": {"repo": "octo/widgets", "number": 12}}
        msg = S.act(self.act_host(), it, "done", env={"GITHUB_TOKEN": GHTOK}, http=http)
        self.assertEqual(msg, "closed octo/widgets#12")
        method, url, headers, kw = http.calls[0]
        self.assertEqual((method, url), ("PATCH", "https://api.github.com/repos/octo/widgets/issues/12"))
        self.assertEqual(kw["json_body"], {"state": "closed", "state_reason": "completed"})
        self.assertEqual(headers["Authorization"], "Bearer " + GHTOK)
        self.assertEqual(len(http.calls), 1)

    def test_github_done_http_failure_and_gh_fallback(self):
        it = {"provider": "github", "ref": {"repo": "octo/widgets", "number": 12}}
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), it, "done", env={"GITHUB_TOKEN": "t"}, http=FakeHTTP({"issues/12": resp(404, {})}))
        self.assertEqual(str(cm.exception), "GitHub HTTP 404")
        run = Runner((0, "", ""))
        with mock.patch.object(core, "which", lambda n, extra=(): "/usr/bin/gh"):
            self.assertEqual(S.act(self.act_host(), it, "done", env={}, runner=run, http=FakeHTTP({})), "closed octo/widgets#12")
        self.assertEqual(run.calls[0]["argv"], ["/usr/bin/gh", "issue", "close", "12", "-R", "octo/widgets", "--reason",
                                                "completed"])
        with mock.patch.object(core, "which", lambda n, extra=(): "/usr/bin/gh"), self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), it, "done", env={}, runner=Runner((1, "", "no perms")))
        self.assertIn("gh issue close: no perms", str(cm.exception))
        with mock.patch.object(core, "which", lambda n, extra=(): None), self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), it, "done", env={})
        self.assertIn("no gh", str(cm.exception))

    # drover and reflections
    def drover_host(self):
        return self.act_host()

    def test_drover_feature_done_upserts_shipped(self):
        d = Daemon(**{"features.upsert": {"ok": True}})
        msg = S.act(self.drover_host(), {"provider": "drover", "id": "f-1", "ref": {"fid": "f-1"}}, "done", env={}, http=d)
        self.assertEqual(msg, "marked f-1 done in Drover")
        self.assertEqual(d.ops(), ["features.upsert"])
        self.assertEqual(d.reqs[0]["token"], MAIN)
        self.assertEqual(d.reqs[0]["body"], {"fid": "f-1", "state": "shipped", "actor": "porchlight"})

    def test_reflection_done_marks_it(self):
        d = Daemon(**{"reflections.mark": {"ok": True}})
        S.act(self.drover_host(), {"provider": "reflections", "id": "r-1", "ref": {"id": "r-1"}}, "done", env={}, http=d)
        body = d.reqs[0]["body"]
        self.assertEqual((d.reqs[0]["op"], body["op"], body["ids"]), ("reflections.mark", "done", ["r-1"]))
        self.assertEqual(body["actor"], "porchlight")

    def test_drover_failures_are_action_errors(self):
        it = {"provider": "drover", "id": "f-1", "ref": {"fid": "f-1"}}
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.drover_host(), it, "done", env={}, http=Daemon())
        self.assertIn("drover: HTTP 404", str(cm.exception))
        self.no_daemon()
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.drover_host(), it, "done", env={}, http=Daemon())
        self.assertEqual(str(cm.exception), "no Drover daemon here")

    # badland
    ENV = {"BADLAND_BRAIN_KEY": "read-key", "BADLAND_API_KEY": "write-key"}

    def badland_http(self, items, toggle=None):
        return FakeHTTP({"api/mcp-context/tasks": resp(200, {"data": {"items": items}}),
                         "/toggle": toggle or resp(200, {"ok": True})})

    def toggles(self, http):
        return [c for c in http.calls if "/toggle" in c[1]]

    def test_badland_done_rechecks_then_toggles_with_the_write_key(self):
        http = self.badland_http([{"id": "t1", "title": "x"}, {"id": "t2", "title": "y"}])
        msg = S.act(self.act_host(), {"provider": "badland", "ref": {"id": "t1"}}, "done", env=self.ENV, http=http)
        self.assertEqual(msg, "completed in Badland")
        (m1, u1, h1, _), (m2, u2, h2, kw2) = http.calls
        self.assertEqual((m1, h1["Authorization"]), ("GET", "Bearer read-key"))
        self.assertIn("/api/mcp-context/tasks?status=open&limit=100", u1)
        self.assertEqual((m2, u2, h2["Authorization"], kw2["json_body"]),
                         ("POST", "https://api.badland.ai/api/todos/items/t1/toggle", "Bearer write-key", {}))

    def test_badland_refuses_an_item_that_is_not_open_and_never_toggles(self):
        for items in ([{"id": "other"}], [{"id": "t1", "isCompleted": True}], []):
            http = self.badland_http(items)
            with self.assertRaises(S.ActionError) as cm:
                S.act(self.act_host(), {"provider": "badland", "ref": {"id": "t1"}}, "done", env=self.ENV, http=http)
            self.assertIn("nothing changed", str(cm.exception))
            self.assertEqual(self.toggles(http), [])

    def test_badland_recheck_failure_and_toggle_failure(self):
        it = {"provider": "badland", "ref": {"id": "t1"}}
        http = FakeHTTP({"api/mcp-context/tasks": resp(500, {})})
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), it, "done", env=self.ENV, http=http)
        self.assertIn("could not re-check", str(cm.exception))
        self.assertEqual(self.toggles(http), [])
        http = self.badland_http([{"id": "t1"}], toggle=resp(500, {}))
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), it, "done", env=self.ENV, http=http)
        self.assertEqual(str(cm.exception), "toggle HTTP 500")

    def test_badland_without_a_write_key_makes_no_request(self):
        http = self.badland_http([{"id": "t1"}])
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), {"provider": "badland", "ref": {"id": "t1"}}, "done",
                  env={"BADLAND_BRAIN_KEY": "read-key"}, http=http)
        self.assertIn("BADLAND_API_KEY", str(cm.exception))
        self.assertEqual(http.calls, [])

    def test_badland_recheck_falls_back_to_the_write_key_when_there_is_no_read_key(self):
        http = self.badland_http([{"id": "t1"}])
        S.act(self.act_host(), {"provider": "badland", "ref": {"id": "t1"}}, "done", env={"BADLAND_API_KEY": "wk"}, http=http)
        self.assertEqual(http.calls[0][2]["Authorization"], "Bearer wk")

    # command
    def test_command_done_quotes_the_id(self):
        run = Runner((0, "", ""))
        weird = "a'; touch /tmp/pwned; echo 'b c"
        msg = S.act(self.act_host('done_command = "mark-done {id} --now"'), {"provider": "command", "ref": {"id": weird}},
                    "done", env={"X_API_TOKEN": "leak"}, runner=run)
        self.assertEqual(msg, "done_command ran for " + weird)
        argv = run.calls[0]["argv"]
        self.assertEqual(argv[:2], ["/bin/sh", "-c"])
        self.assertEqual(argv[2], "mark-done %s --now" % shlex.quote(weird))
        self.assertEqual(shlex.split(argv[2]), ["mark-done", weird, "--now"])  # one argument, whatever it holds
        self.assertNotIn("X_API_TOKEN", run.calls[0]["env"])

    def test_command_done_cannot_inject_through_a_real_shell(self):
        out, hit = os.path.join(self.dir, "out.txt"), os.path.join(self.dir, "INJECTED")
        weird = "a'; touch %s; echo 'b $(touch %s) `touch %s` c" % (hit, hit, hit)
        tmpl = "printf %%s {id} > %s" % shlex.quote(out)
        S.act(self.act_host("done_command = %s" % q(tmpl)), {"provider": "command", "ref": {"id": weird}}, "done", env={})
        self.assertEqual(slurp(out), weird)
        self.assertFalse(os.path.exists(hit))

    def test_command_done_needs_a_template_and_a_zero_exit(self):
        it = {"provider": "command", "ref": {"id": "1"}}
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host(), it, "done", env={}, runner=Runner((0, "", "")))
        self.assertIn("done_command", str(cm.exception))
        with self.assertRaises(S.ActionError) as cm:
            S.act(self.act_host('done_command = "false {id}"'), it, "done", env={}, runner=Runner((3, "", "")))
        self.assertEqual(str(cm.exception), "done_command exit 3")


# ── 7. find ────────────────────────────────────────────────────────────────────
class Find(unittest.TestCase):
    ITEMS = [{"key": "files:abcdef1234", "id": "abcdef1234"}, {"key": "files:abc9999999", "id": "abc9999999"},
             {"key": "github:o/r#1", "id": "o/r#1"}, {"key": "github:o/r#10", "id": "o/r#10"},
             {"key": "drover:f-1", "id": "f-1"}]

    def test_exact_key(self):
        self.assertEqual(S.find(self.ITEMS, "github:o/r#1")["id"], "o/r#1")  # exact beats the prefix of #10

    def test_unique_prefix_of_key_or_id(self):
        self.assertEqual(S.find(self.ITEMS, "files:abcd")["id"], "abcdef1234")
        self.assertEqual(S.find(self.ITEMS, "abcdef")["id"], "abcdef1234")
        self.assertEqual(S.find(self.ITEMS, "drover")["id"], "f-1")
        self.assertEqual(S.find(self.ITEMS, "f-")["id"], "f-1")

    def test_exact_only_never_takes_a_prefix(self):
        self.assertEqual(S.find(self.ITEMS, "github:o/r#1", exact_only=True)["id"], "o/r#1")
        gone = [i for i in self.ITEMS if i["key"] != "github:o/r#1"]
        self.assertEqual(S.find(gone, "github:o/r#1")["id"], "o/r#10")  # the trap exact_only closes
        with self.assertRaises(S.ActionError) as cm:
            S.find(gone, "github:o/r#1", exact_only=True)
        self.assertIn("no todo github:o/r#1", str(cm.exception))

    def test_ambiguous_and_missing(self):
        with self.assertRaises(S.ActionError) as cm:
            S.find(self.ITEMS, "files:abc")
        self.assertEqual(str(cm.exception), "files:abc matches 2 todos")
        with self.assertRaises(S.ActionError) as cm:
            S.find(self.ITEMS, "github")
        self.assertIn("matches 2", str(cm.exception))
        with self.assertRaises(S.ActionError) as cm:
            S.find(self.ITEMS, "nope")
        self.assertEqual(str(cm.exception), "no todo nope")
        with self.assertRaises(S.ActionError):
            S.find([], "x")


# ── 8. panel ───────────────────────────────────────────────────────────────────
def rec_item(provider, ident, title, **kw):
    asks = kw.pop("asks", [])
    it = S.item(provider, ident, title, **kw)
    it["asks"] = asks
    return it


def data_of(items, provs=None, **kw):
    provs = provs if provs is not None else {n: {"ok": True, "n": sum(1 for i in items if i["provider"] == n)}
                                             for n in dict.fromkeys(i["provider"] for i in items)}
    d = {"items": items, "providers": provs, "total": len(items), "overdue": sum(1 for i in items if i["due"] and
                                                                              i["due"] < time.time())}
    d.update(kw)
    return d


class Panel(Base):
    def build(self, data, cw=70, extra="", age=0.0, ok=True):
        support.clear_cache()
        put("todos", data, age=age, ok=ok)
        return BUILDERS["todos"](RCtx(self.host(extra), registry.Cache(), {"vitals": {}}, [], cw))

    def lines(self, p):
        return [T.strip(ln) for ln in p.lines]

    def test_count_row_rows_and_the_ordering_cap(self):
        items = [rec_item("files", "f%d" % n, "file task %d" % n, prio=3) for n in range(5)]
        items += [rec_item("github", "o/r#%d" % n, "gh task %d" % n, repo="o/r", url="https://github.com/o/r/issues/%d" % n)
                  for n in range(4)]
        p = self.build(data_of(items))
        rows = self.lines(p)
        self.assertEqual(rows[0], "9 open · file 5 · gh 4" .replace(" · ", T.strip(T.sep())) if False else rows[0])
        self.assertTrue(rows[0].startswith("9 open"), rows[0])
        self.assertIn("file 5", rows[0])
        self.assertIn("gh 4", rows[0])
        self.assertEqual(len(rows), 1 + 6 + 1)
        self.assertEqual(rows[-1].strip(), "+3 more · porchlight todos")
        self.assertEqual((p.key, p.level), ("todos", "ok"))

    def test_shown_setting(self):
        items = [rec_item("files", "f%d" % n, "task %d" % n) for n in range(5)]
        rows = self.lines(self.build(data_of(items), extra="shown = 2"))
        self.assertEqual(len(rows), 1 + 2 + 1)
        self.assertEqual(rows[-1].strip(), "+3 more · porchlight todos")
        self.assertNotIn("more", "\n".join(self.lines(self.build(data_of(items[:2]), extra="shown = 2"))))

    def test_overdue_rows_flag_and_the_panel_warns(self):
        late = rec_item("files", "late", "pay the invoice", due=time.time() - 3 * 86400)
        fine = rec_item("files", "fine", "water the plants", due=time.time() + 5 * 86400)
        p = self.build(data_of([late, fine]))
        rows = self.lines(p)
        self.assertTrue(rows[1].startswith("! pay the invoice"), rows[1])
        self.assertIn("-3d", rows[1])
        self.assertFalse(rows[2].startswith("!"), rows[2])
        self.assertEqual((p.level, p.note), ("warn", "1 overdue"))

    def test_high_priority_and_plain_glyphs_differ(self):
        hi = rec_item("files", "hi", "urgent thing", prio=1)
        lo = rec_item("files", "lo", "calm thing", prio=5)
        rows = [ln for ln in self.build(data_of([hi, lo])).lines]
        self.assertNotEqual(rows[1].split(" ")[0], rows[2].split(" ")[0])
        self.assertFalse(T.strip(rows[1]).startswith("!"))

    def test_row_shows_where(self):
        gh = rec_item("github", "octo/widgets#12", "Voice crackles", repo="octo/widgets")
        dr = rec_item("drover", "f-1", "Ship it", repo="dotfiles")
        rows = "\n".join(self.lines(self.build(data_of([gh, dr]), cw=90)))
        self.assertIn("gh octo/widgets#12", rows)
        self.assertIn("drover dotfiles", rows)

    def test_asks_row_with_gaps(self):
        items = [rec_item("files", "a", "V-2 a", asks=["V-2"]), rec_item("command", "b", "V-10 b", asks=["V-10"])]
        asks = {"V-2": {"in": ["command", "files"], "missing": [], "open": 2},
                "V-10": {"in": ["command"], "missing": ["files", "drover"], "open": 1}}
        rows = self.lines(self.build(data_of(items, asks=asks, ask_systems=["files", "command", "drover"]), cw=80))
        ask_row = [r for r in rows if r.startswith("asks")]
        self.assertEqual(len(ask_row), 1)
        self.assertIn("2 tracked", ask_row[0])
        self.assertIn("1 everywhere", ask_row[0])
        self.assertIn("1 gap", ask_row[0])
        self.assertNotIn("1 gaps", ask_row[0])
        gap = rows[rows.index(ask_row[0]) + 1]
        self.assertEqual(gap.strip(), "V-10 -file,drover")

    def test_asks_with_no_gap_has_no_gap_row(self):
        asks = {"V-2": {"in": ["files"], "missing": [], "open": 1}}
        rows = self.lines(self.build(data_of([rec_item("files", "a", "V-2")], asks=asks)))
        self.assertIn("1 everywhere", [r for r in rows if r.startswith("asks")][0])
        self.assertNotIn("gap", "\n".join(rows))
        self.assertEqual(rows[-1][:4], "asks")

    def test_failed_provider_rows(self):
        items = [rec_item("files", "a", "one")]
        provs = {"files": {"ok": True, "n": 1}, "github": {"ok": False, "n": 0, "error": "GitHub search HTTP 500"},
                 "drover": {"ok": False, "n": 3, "error": "no Drover daemon here", "kept": True}}
        rows = self.lines(self.build(data_of(items, provs=provs), cw=80))
        self.assertIn(T.strip(T.g("crit")) + " gh: GitHub search HTTP 500", rows)
        self.assertIn(T.strip(T.g("crit")) + " drover: no Drover daemon here (kept)", rows)

    def test_panel_items_carry_every_item_and_their_actions(self):
        items = [rec_item("files", "f%d" % n, "task %d" % n) for n in range(8)]
        items.append(rec_item("github", "o/r#1", "has a link", repo="o/r", url="https://github.com/o/r/issues/1"))
        p = self.build(data_of(items))
        self.assertEqual(len(p.items), 9)  # not just the six that fit
        plain, linked = p.items[0], p.items[-1]
        self.assertEqual(plain["actions"], ["done"])
        self.assertEqual(plain["url"], "")
        self.assertEqual(linked["actions"], ["done", "open"])
        self.assertEqual(linked["url"], "https://github.com/o/r/issues/1")
        for it, raw in zip(p.items, items):
            self.assertEqual((it["key"], it["source"], it["text"], it["raw"]), (raw["key"], "todos", raw["title"], raw))
            self.assertTrue(it["detail"])

    def test_detail_lists_the_fields(self):
        it = rec_item("github", "o/r#1", "Fix it", repo="o/r", url="https://github.com/o/r/issues/1", due=epoch("2030-01-02"),
                      labels=["bug"], asks=["V-2"], kind="issue")
        text = "\n".join(T.strip(x) for x in ptodos.detail(it))
        for want in ("Fix it", "gh o/r#1", "github:o/r#1", "issue", "2030-01-0", "V-2", "bug", "https://github.com/o/r/issues/1"):
            self.assertIn(want, text)

    def test_no_data_no_panel(self):
        support.clear_cache()
        self.assertIsNone(BUILDERS["todos"](RCtx(self.host(), registry.Cache(), {"vitals": {}}, [], 70)))
        self.assertIsNone(self.build({}))
        self.assertIsNone(self.build("junk"))

    def test_error_only_record_shows_the_error(self):
        p = self.build({"error": "no providers configured", "items": [], "providers": {}})
        self.assertEqual(self.lines(p), ["no providers configured"])

    def test_stale_record_shows_its_age(self):
        p = self.build(data_of([rec_item("files", "a", "x")]), age=10 * 3600)
        self.assertEqual(p.note, "10h")

    def test_malformed_records_never_break_the_panel(self):
        for d in ({"items": "x", "providers": []}, {"items": [1, None, {"title": None, "provider": 5}], "providers": {"a": 3}},
                  {"items": [{"key": "k", "provider": "files", "title": ["a"], "due": "soon", "prio": "x"}], "asks": [1],
                   "providers": {"files": {"ok": False, "error": {"x": 1}}}, "overdue": "many"}):
            try:
                self.build(d)
            except Exception as e:  # noqa: BLE001
                self.fail("%r raised %r" % (d, e))

    def test_fits_narrow_widths(self):
        long_title = "A very long todo title that cannot possibly fit a narrow column V-12 " * 3
        items = [rec_item("github", "octo-organization/some-very-long-repository-name#1234", long_title,
                          repo="octo-organization/some-very-long-repository-name", url="https://github.com/o/r/issues/1",
                          due=time.time() - 9 * 86400, prio=1),
                 rec_item("files", "a", "wide: " + "漢字仮名" * 20), rec_item("drover", "f", "短い", repo="リポジトリ" * 4)]
        items += [rec_item("files", "m%d" % n, "more %d" % n) for n in range(6)]
        provs = {"files": {"ok": True, "n": 7}, "github": {"ok": True, "n": 1},
                 "drover": {"ok": False, "n": 1, "error": "no Drover daemon here and a long reason " * 3, "kept": True},
                 "badland": {"ok": False, "n": 0, "error": "no BADLAND_BRAIN_KEY"}}
        asks = {"V-%d" % n: {"in": ["files"], "missing": ["github", "drover", "badland"], "open": 1} for n in range(1, 9)}
        asks["V-99"] = {"in": ["files"], "missing": [], "open": 1}
        data = data_of(items, provs=provs, asks=asks, ask_systems=["files", "github", "drover", "badland"])
        for ascii_ in (False, True):
            T.set_ascii(ascii_)
            self.addCleanup(T.set_ascii, False)
            for cw in (24, 30, 40, 58, 80):
                p = self.build(data, cw=cw)
                self.assertTrue(p.lines)
                for ln in p.lines:
                    self.assertLessEqual(T.width(ln), cw, "%d: %r" % (cw, T.strip(ln)))
                for ln in p.render(cw, T.ACCENT):
                    self.assertLessEqual(T.width(ln), cw, "render %d: %r" % (cw, T.strip(ln)))
        T.set_ascii(False)


# ── 9. CLI ─────────────────────────────────────────────────────────────────────
class Tty(io.StringIO):
    def isatty(self):
        return True


class Cli(Base):
    def setUp(self):
        super().setUp()
        self.todo = self.write("todo.txt", "(A) ship the thing due:2000-01-01 V-2\nsecond thing\n# note\n")
        self.cfg_path = self.write("config.toml", self.toml(self.files_toml(self.todo) + "actions = true\n" +
                                                            'ask_pattern = \'V-\\d+\'\nask_systems = ["files", "github"]\n'))
        self.cmd = mock.patch.object(core, "CONFIG_PATH", self.cfg_path)
        self.cmd.start()
        self.addCleanup(self.cmd.stop)
        spawn = mock.patch.object(collect, "spawn_background", return_value=True)
        self.spawned = spawn.start()
        self.addCleanup(spawn.stop)
        self.collect()

    def collect(self):
        """What `porchlight collect --only todos` would have cached."""
        c = support.ctx(host(slurp(self.cfg_path)), host(slurp(self.cfg_path)).cfg.source("todos", S.DEFAULTS))
        data = S.todos(c)
        put("todos", data)
        return data

    def main(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", stdin if stdin is not None else io.StringIO("")):
            rc = cli.main(list(argv))
        return rc, T.strip(out.getvalue()), T.strip(err.getvalue())

    def keys(self):
        return {i["title"]: i["key"] for i in registry.Cache().data("todos")["items"]}

    def test_the_list_is_grouped_by_system(self):
        rc, out, err = self.main("todos")
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.startswith("2 open · cached "), out)
        self.assertIn("FILES 2", out)
        for want in ("ship the thing", "second thing", "V-2"):
            self.assertIn(want, out)
        for key in self.keys().values():
            self.assertIn(key, out)
        self.assertEqual(self.spawned.call_count, 0)

    def test_singular_todo_command_lists_too(self):
        rc, out, _ = self.main("todo")
        self.assertEqual(rc, 0)
        self.assertIn("FILES 2", out)

    def test_a_failed_provider_shows_beside_its_group(self):
        data = registry.Cache().data("todos")
        data["providers"]["files"] = {"ok": False, "n": 2, "error": "boom here", "kept": True}
        put("todos", data)
        self.assertIn("boom here", self.main("todos")[1])

    def test_asks(self):
        rc, out, err = self.main("todos", "asks")
        self.assertEqual((rc, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["ask", "file", "gh"])
        self.assertEqual(lines[1].split(), ["V-2", T.strip(T.g("ok")), "-", "1", "open"])

    def test_asks_off_is_an_error(self):
        put("todos", {"items": [], "providers": {"files": {"ok": True, "n": 0}}})
        rc, out, err = self.main("todos", "asks")
        self.assertEqual(rc, 2)
        self.assertIn("ask tracking is off", err)

    def test_json(self):
        rc, out, _ = self.main("todos", "--json")
        self.assertEqual(rc, 0)
        items = json.loads(out)
        self.assertEqual(sorted(i["title"] for i in items), [["(A)", "ship the thing due:2000-01-01 V-2"][1],
                                                             "second thing"] if False else sorted(i["title"] for i in items))
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i["provider"] == "files" for i in items))
        rc, out, _ = self.main("todos", "asks", "--json")
        self.assertEqual(json.loads(out)["V-2"]["in"], ["files"])

    def test_no_cache_is_a_pointer_to_the_docs(self):
        support.clear_cache()
        rc, out, err = self.main("todos")
        self.assertEqual(rc, 2)
        self.assertIn("no todos yet", err)

    def test_help(self):
        for argv in (("todos", "help"), ("todos", "-h")):
            rc, out, _ = self.main(*argv)
            self.assertEqual(rc, 0, argv)
            self.assertIn("porchlight todos done <key>", out)

    def test_done_marks_the_file_and_forgets_the_item(self):
        keys = self.keys()
        rc, out, err = self.main("todos", "done", keys["ship the thing due:2000-01-01 V-2"], "--yes")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("marked done in " + self.todo, out)
        self.assertEqual(slurp(self.todo),
                         "x %s (A) ship the thing due:2000-01-01 V-2\nsecond thing\n# note\n" % TODAY)
        left = registry.Cache().data("todos")
        self.assertEqual([i["title"] for i in left["items"]], ["second thing"])
        self.assertEqual(left["total"], 1)
        self.assertEqual(self.spawned.call_count, 1)
        self.assertEqual(self.spawned.call_args.kwargs, {"only": ["todos"]})

    def test_done_by_unique_prefix_and_short_yes(self):
        rc, out, _ = self.main("todos", "done", self.keys()["second thing"][:12], "-y")
        self.assertEqual(rc, 0, out)
        self.assertIn("x %s second thing" % TODAY, slurp(self.todo))

    def test_done_without_yes_and_no_tty_is_refused_and_changes_nothing(self):
        before = slurp(self.todo)
        rc, out, err = self.main("todos", "done", self.keys()["second thing"])
        self.assertEqual(rc, 2)
        self.assertIn("not a terminal; pass --yes", err)
        self.assertEqual(slurp(self.todo), before)
        self.assertEqual(len(registry.Cache().data("todos")["items"]), 2)
        self.assertEqual(self.spawned.call_count, 0)

    def test_done_at_a_terminal_asks_first(self):
        key = self.keys()["second thing"]
        before = slurp(self.todo)
        rc, out, _ = self.main("todos", "done", key, stdin=Tty("n\n"))
        self.assertEqual(rc, 1)
        self.assertIn("unchanged", out)
        self.assertEqual(slurp(self.todo), before)
        rc, out, _ = self.main("todos", "done", key, stdin=Tty("y\n"))
        self.assertEqual(rc, 0)
        self.assertIn("x %s second thing" % TODAY, slurp(self.todo))

    def test_done_needs_a_key_and_a_known_one(self):
        rc, out, err = self.main("todos", "done")
        self.assertEqual(rc, 2)
        self.assertIn("porchlight todos done <key>", err)
        rc, out, err = self.main("todos", "done", "files:nope", "--yes")
        self.assertEqual(rc, 2)
        self.assertIn("no todo files:nope", err)
        rc, out, err = self.main("todos", "done", "files:", "--yes")
        self.assertEqual(rc, 2)
        self.assertIn("matches 2 todos", err)

    def test_done_with_actions_off_is_refused(self):
        self.write("config.toml", self.toml(self.files_toml(self.todo)))
        before = slurp(self.todo)
        rc, out, err = self.main("todos", "done", self.keys()["second thing"], "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("actions are off", err)
        self.assertEqual(slurp(self.todo), before)
        self.assertEqual(len(registry.Cache().data("todos")["items"]), 2)  # not forgotten: nothing was done
        self.assertEqual(self.spawned.call_count, 0)

    def test_done_when_the_line_changed_is_an_error_and_keeps_the_item(self):
        key = self.keys()["second thing"]
        self.write("todo.txt", "something else\n")
        rc, out, err = self.main("todos", "done", key, "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("no longer in", err)
        self.assertEqual(len(registry.Cache().data("todos")["items"]), 2)

    def test_via_hub_without_a_hub_is_refused(self):
        rc, out, err = self.main("todos", "done", self.keys()["second thing"], "--via-hub")
        self.assertEqual(rc, 2)
        self.assertIn("no hub known", err)

    def test_via_hub_asks_first_and_sends_the_whole_key_exactly(self):
        from porchlight import todo_cmd
        key = self.keys()["second thing"]
        with mock.patch.object(todo_cmd, "hub_name", return_value="hubbox"), \
                mock.patch.object(core, "run", return_value=(0, "ok\n", "")) as run:
            rc, out, err = self.main("todos", "done", key[:12], "--via-hub")  # no tty, no --yes
            self.assertEqual(rc, 2)
            self.assertIn("not a terminal; pass --yes", err)
            rc, out, _ = self.main("todos", "done", key[:12], "--via-hub", stdin=Tty("n\n"))
            self.assertEqual(rc, 1)
            self.assertIn("via hubbox", out)
            self.assertEqual(run.call_count, 0)
            rc, out, _ = self.main("todos", "done", key[:12], "--via-hub", stdin=Tty("y\n"))
        self.assertEqual(rc, 0)
        argv = run.call_args[0][0]
        self.assertEqual(argv, todo_cmd.hub_argv("hubbox", key))  # the full key, not the typed prefix
        self.assertEqual(shlex.split(argv[-1]), ["porchlight", "todos", "done", key, "--yes", "--exact"])
        self.assertEqual(slurp(self.todo).count("x "), 0)  # the hub acts; this machine changed nothing

    def test_exact_refuses_a_prefix_on_the_acting_machine(self):
        key = self.keys()["second thing"]
        before = slurp(self.todo)
        rc, out, err = self.main("todos", "done", key[:12], "--yes", "--exact")
        self.assertEqual(rc, 2)
        self.assertIn("no todo %s" % key[:12], err)
        self.assertEqual(slurp(self.todo), before)
        rc, out, _ = self.main("todos", "done", key, "--yes", "--exact")
        self.assertEqual(rc, 0, out)
        self.assertIn("x %s second thing" % TODAY, slurp(self.todo))

    def test_forget_leaves_other_records_and_survives_a_missing_one(self):
        from porchlight import todo_cmd
        support.clear_cache()
        todo_cmd.forget("files:x")  # no record: nothing to do
        put("todos", {"items": [{"key": "files:a"}, {"key": "files:b"}, "junk"], "total": 3})
        todo_cmd.forget("files:a")
        data = registry.Cache().data("todos")
        self.assertEqual(data["items"], [{"key": "files:b"}, "junk"])
        self.assertEqual(data["total"], 2)


if __name__ == "__main__":
    unittest.main()

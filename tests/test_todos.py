"""The todos source (sources/todos.py) and the TODOS panel (panels/todos.py).

Offline: every call is a fake routed by URL substring (FakeHTTP) or a patched Drover
daemon (a temp state dir + a fake op dispatcher). Contract: docs/contracts/todo-item-v1.md.
"""
import support  # noqa: F401 — first: scratch cache and config

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from porchlight import actions, config, core, registry, secrets, view
from porchlight.panels import todos as ptodos  # noqa: F401 — registers the panel
from porchlight.registry import Cache
from porchlight.sources import todos
from porchlight.ui import BUILDERS, RCtx
from support import clear_cache, ctx, host, put, resp

NOW = time.time()
CANARY = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2  # a token shape, built at runtime: never scanner bait


# ── Small fixtures ────────────────────────────────────────────────────────────
def badland_lists(*lists):
    return resp(200, {"success": True, "data": list(lists)})


def badland_items(*items):
    return resp(200, {"success": True, "data": list(items)})


def bl_list(id="l1", title="Voice Gateway", count=5, done=0, archived=False):
    return {"id": id, "title": title, "isArchived": archived, "itemCount": count, "completedCount": done}


def bl_item(id="i1", title="do the thing", priority="high", due=None, completed=False, archived=False, tags=None):
    return {"id": id, "listId": "l1", "title": title, "description": "", "isCompleted": completed,
            "isArchived": archived, "priority": priority, "dueDate": due, "tags": tags or [],
            "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-05T00:00:00Z"}


class DroverDaemon:
    """A fake Drover daemon: op -> result, every request recorded with the token it carried."""

    def __init__(self, results=None):
        self.results = dict(results or {})
        self.reqs = []

    def __call__(self, url, method="GET", headers=None, json_body=None, timeout=6.0, **kw):
        op = url.rsplit("/", 1)[-1]
        auth = (headers or {}).get("Authorization", "")
        tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        self.reqs.append({"op": op, "token": tok, "body": json_body})
        if op not in self.results:
            return core.Resp(404, {"ok": False, "error": "unknown op %s" % op}, 1)
        result = self.results[op]
        if isinstance(result, Exception):
            raise result
        return core.Resp(200, {"ok": True, "result": result}, 3)


def daemon_state(main="main-tok-fixture-9f2c1a"):
    """A temp Drover state dir (daemon.json + token); returns (dir, env)."""
    d = tempfile.mkdtemp(prefix="todos-drover-state-")
    with open(os.path.join(d, "daemon.json"), "w") as f:
        json.dump({"url": "http://127.0.0.1:7337/"}, f)
    with open(os.path.join(d, "token"), "w") as f:
        f.write(main + "\n")
    return d, {"DROVER_STATE": d}


BADLAND_CFG = {"base_url": "https://api.example.com", "web_url": "https://example.com", "key_env": "BADLAND_API_KEY",
               "lists": []}
DROVER_CFG = {"enabled": True, "states": ["not-started", "in-progress"]}
REFLECTIONS_CFG = {"enabled": True, "kinds": ["action", "question"]}
GITHUB_CFG = {"owner": "acme", "labels": ["voice-gateway"], "query": "", "token_env": ["GH_TOKEN", "GITHUB_TOKEN"]}


# ── Providers: parsing ───────────────────────────────────────────────────────
class Badland(unittest.TestCase):
    def test_parses_open_items_across_non_archived_lists_with_open_work(self):
        h = host("")
        fake = support.FakeHTTP({
            "api.example.com/api/todos/lists": badland_lists(
                bl_list("l1", "Voice Gateway", count=3, done=0),
                bl_list("l2", "Done list", count=2, done=2),           # fully done: skipped
                bl_list("l3", "Archived", count=1, done=0, archived=True)),  # archived: skipped
            "api.example.com/api/todos/lists/l1/items": badland_items(
                bl_item("i1", "V-43 ship the panel", priority="high", due="2026-11-05T00:00:00Z"),
                bl_item("i2", "done already", completed=True),         # completed: skipped
                bl_item("i3", "buy milk", priority="none", tags=["errand"])),
        })
        c = ctx(h, env={"BADLAND_API_KEY": "tok"}, http=fake)
        res = todos._provider_badland(c, BADLAND_CFG, [])
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["items"]), 2)
        it = res["items"][0]
        self.assertEqual(it["id"], "badland:i1")
        self.assertEqual(it["provider"], "badland")
        self.assertEqual(it["group"], "Voice Gateway")
        self.assertEqual(it["priority"], "high")
        self.assertEqual(it["url"], "https://example.com/todos/l1")
        self.assertEqual(it["can"], ["done"])
        self.assertIsNotNone(it["due"])
        self.assertEqual(res["items"][1]["tags"], ["errand"])
        # The Authorization header carried the key, and only the configured lists were fetched.
        self.assertTrue(any(h.get("Authorization") == "Bearer tok" for _, _, h, _ in fake.calls))
        self.assertEqual(sum(1 for _, u, _, _ in fake.calls if "/items" in u), 1)

    def test_off_without_a_base_url(self):
        c = ctx(host(""), env={}, http=support.FakeHTTP({}))
        self.assertIsNone(todos._provider_badland(c, {"base_url": "", "web_url": "", "key_env": "BADLAND_API_KEY", "lists": []}, []))

    def test_the_lists_setting_filters_by_title_or_id(self):
        h = host("")
        fake = support.FakeHTTP({
            "api.example.com/api/todos/lists": badland_lists(bl_list("l1", "Voice Gateway"), bl_list("l2", "Chores")),
            "api.example.com/api/todos/lists/l2/items": badland_items(bl_item("i9", "mow the lawn")),
        })
        c = ctx(h, env={"BADLAND_API_KEY": "tok"}, http=fake)
        res = todos._provider_badland(c, dict(BADLAND_CFG, lists=["Chores"]), [])
        self.assertEqual([it["ref"] for it in res["items"]], ["i9"])


class Drover(unittest.TestCase):
    def features_result(self):
        return {"features": [
            {"fid": "feat-1", "title": "V-43 todos panel", "repo": "porchlight", "state": "in-progress",
             "created": "2026-10-01T00:00:00Z", "updated": "2026-10-20T00:00:00Z", "links": ["https://example.com/f1"]},
            {"fid": "feat-2", "title": "already shipped", "repo": "porchlight", "state": "shipped",
             "created": "2026-09-01T00:00:00Z", "updated": "2026-09-02T00:00:00Z"},
        ]}

    def test_parses_open_and_in_progress_features_mapping_state(self):
        d, env = daemon_state()
        self.addCleanup(shutil.rmtree, d, True)
        daemon = DroverDaemon({"events.state": self.features_result()})
        with mock.patch.object(registry.Ctx, "http", staticmethod(daemon)):
            c = ctx(host(""), env=env)
            res = todos._provider_drover(c, DROVER_CFG, [])
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["items"]), 1)  # "shipped" is filtered out by the default states
        it = res["items"][0]
        self.assertEqual((it["id"], it["state"], it["group"], it["url"]),
                         ("drover:feat-1", "in-progress", "porchlight", "https://example.com/f1"))
        self.assertEqual(daemon.reqs[0]["token"], "main-tok-fixture-9f2c1a")

    def test_off_without_a_daemon(self):
        c = ctx(host(""), env={"DROVER_STATE": tempfile.mkdtemp(prefix="todos-no-daemon-")})
        self.assertIsNone(todos._provider_drover(c, DROVER_CFG, []))

    def test_disabled_is_off(self):
        c = ctx(host(""), env={})
        self.assertIsNone(todos._provider_drover(c, dict(DROVER_CFG, enabled=False), []))


class Reflections(unittest.TestCase):
    def test_parses_items_and_never_stores_the_body(self):
        d, env = daemon_state()
        self.addCleanup(shutil.rmtree, d, True)
        result = {"items": [{"id": "r-1", "title": "decide: V-39 shared panels", "kind": "action", "status": "open",
                             "priority": 2, "host": "atlas", "repo": "porchlight", "created_at": "2026-10-01T00:00:00Z",
                             "updated_at": "2026-10-10T00:00:00Z", "tags": ["decision"],
                             "body": "a long private body " + CANARY}]}
        daemon = DroverDaemon({"reflections.list": result})
        with mock.patch.object(registry.Ctx, "http", staticmethod(daemon)):
            c = ctx(host(""), env=env, cfg={"keep": 50})
            res = todos._provider_reflections(c, REFLECTIONS_CFG, [])
        self.assertTrue(res["ok"])
        it = res["items"][0]
        self.assertEqual((it["id"], it["priority"], it["host"], it["group"]), ("reflections:r-1", "high", "atlas", "porchlight"))
        self.assertNotIn("body", it)
        self.assertNotIn(CANARY, json.dumps(res))
        self.assertEqual(daemon.reqs[0]["body"], {"all": True, "status": "open", "kind": "action,question", "limit": 50})


class Github(unittest.TestCase):
    def test_off_without_labels_or_query(self):
        c = ctx(host(""), env={}, http=support.FakeHTTP({}))
        self.assertIsNone(todos._provider_github(c, {"owner": "acme", "labels": [], "query": "", "token_env": ["GH_TOKEN"]}, []))

    def test_parses_issues_for_each_label(self):
        h = host("")
        issue = {"title": "V-41 open-source ai-cli", "number": 7, "repository_url": "https://api.github.com/repos/acme/ai-cli",
                 "html_url": "https://github.com/acme/ai-cli/issues/7", "created_at": "2026-10-01T00:00:00Z",
                 "updated_at": "2026-10-02T00:00:00Z", "labels": [{"name": "voice-gateway"}]}
        fake = support.FakeHTTP({"api.github.com/search/issues": resp(200, {"items": [issue]})})
        c = ctx(h, env={"GH_TOKEN": "tok"}, http=fake)
        res = todos._provider_github(c, GITHUB_CFG, [])
        self.assertTrue(res["ok"])
        it = res["items"][0]
        self.assertEqual((it["id"], it["group"], it["tags"]), ("github:acme/ai-cli#7", "acme/ai-cli", ["voice-gateway"]))
        q = fake.calls[0][1]
        self.assertIn("label%3A%22voice-gateway%22", q)
        self.assertIn("user%3Aacme", q)

    def test_a_raw_query_replaces_the_default_one(self):
        h = host("")
        fake = support.FakeHTTP({"api.github.com/search/issues": resp(200, {"items": []})})
        c = ctx(h, env={"GH_TOKEN": "tok"}, http=fake)
        todos._provider_github(c, dict(GITHUB_CFG, query="repo:acme/x is:open"), [])
        self.assertIn("repo%3Aacme%2Fx", fake.calls[0][1])

    def test_no_token_is_a_reported_failure_not_an_exception(self):
        c = ctx(host(""), env={}, http=support.FakeHTTP({}))
        with mock.patch.object(core, "which", return_value=None):
            res = todos._provider_github(c, GITHUB_CFG, [{"id": "github:acme/x#1"}])
        self.assertFalse(res["ok"])
        self.assertIn("no GitHub token", res["error"])
        self.assertEqual(res["items"], [{"id": "github:acme/x#1"}])  # prev kept

    def test_token_order_env_before_gh_auth_token(self):
        self.assertEqual(todos._github_token({"GH_TOKEN": "a", "GITHUB_TOKEN": "b"}, {"token_env": ["GH_TOKEN", "GITHUB_TOKEN"]}), "a")
        self.assertEqual(todos._github_token({"GITHUB_TOKEN": "b"}, {"token_env": ["GH_TOKEN", "GITHUB_TOKEN"]}), "b")
        with mock.patch.object(core, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(core, "run", return_value=(0, "gh-cli-token\n", "")):
            self.assertEqual(todos._github_token({}, {"token_env": ["GH_TOKEN", "GITHUB_TOKEN"]}), "gh-cli-token")
        with mock.patch.object(core, "which", return_value=None):
            self.assertIsNone(todos._github_token({}, {"token_env": ["GH_TOKEN", "GITHUB_TOKEN"]}))


class File(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="todos-file-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_todotxt_lines_skip_done_and_read_priority(self):
        p = os.path.join(self.tmp, "todo.txt")
        with open(p, "w") as f:
            f.write("(A) call the vet\nx done already 2026-10-01\n(C) water the plants\nno priority line\n")
        res = todos._provider_file(ctx(host("")), {"paths": [p]}, [])
        self.assertTrue(res["ok"])
        titles = [(it["title"], it["priority"]) for it in res["items"]]
        self.assertEqual(titles, [("call the vet", "high"), ("water the plants", "low"), ("no priority line", "none")])

    def test_markdown_checkboxes(self):
        p = os.path.join(self.tmp, "notes.md")
        with open(p, "w") as f:
            f.write("# Notes\n- [ ] ship the panel\n- [x] already done\n* [ ] another one\nplain text\n")
        res = todos._provider_file(ctx(host("")), {"paths": [p]}, [])
        self.assertEqual([it["title"] for it in res["items"]], ["ship the panel", "another one"])

    def test_off_without_paths(self):
        self.assertIsNone(todos._provider_file(ctx(host("")), {"paths": []}, []))


# ── Isolation: a failed provider keeps ctx.prev and never affects the others ─
class Isolation(unittest.TestCase):
    def test_a_failing_provider_keeps_its_previous_items_and_reports_a_short_error(self):
        c = ctx(host(""), env={"BADLAND_API_KEY": "tok"}, http=support.FakeHTTP({}))  # 404s everywhere
        prev = [todos._item("badland", "old-1", "kept from last run")]
        res = todos._provider_badland(c, BADLAND_CFG, prev)
        self.assertFalse(res["ok"])
        self.assertEqual(res["items"], prev)
        self.assertTrue(res["error"])
        self.assertNotIn("api.example.com", res["error"])  # never a response body or a URL

    def test_the_full_source_isolates_each_provider(self):
        """One provider (badland) fails; drover, reflections and file still run and report ok."""
        d, denv = daemon_state()
        self.addCleanup(shutil.rmtree, d, True)
        daemon = DroverDaemon({"events.state": Drover().features_result(), "reflections.list": {"items": []}})
        tmp = tempfile.mkdtemp(prefix="todos-file-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "todo.txt"), "w") as f:
            f.write("a plain todo\n")
        toml = """
[sources.todos]
[sources.todos.badland]
base_url = "https://api.example.com"
web_url = "https://example.com"
[sources.todos.drover]
[sources.todos.reflections]
[sources.todos.github]
[sources.todos.file]
paths = ["%s"]
""" % os.path.join(tmp, "todo.txt")
        h = host(toml)
        cfg = registry.SOURCES["todos"].config(h)
        env = dict(denv, BADLAND_API_KEY="tok")
        with mock.patch.object(registry.Ctx, "http", staticmethod(daemon)):
            c = registry.Ctx(h, env, None, cfg)
            data = todos.todos(c)
        self.assertFalse(data["providers"]["badland"]["ok"])
        self.assertTrue(data["providers"]["drover"]["ok"])
        self.assertTrue(data["providers"]["reflections"]["ok"])
        self.assertTrue(data["providers"]["file"]["ok"])
        self.assertNotIn("github", data["providers"])  # not configured: absent entirely
        json.dumps(data)  # the whole record is JSON-able


# ── Masking ───────────────────────────────────────────────────────────────────
class Masking(unittest.TestCase):
    def test_a_canary_in_a_title_never_reaches_the_record(self):
        it = todos._item("badland", "i1", "token leaked " + CANARY)
        self.assertNotIn(CANARY, it["title"])
        self.assertIn("ghp_", it["title"])  # the shape's prefix survives; the secret body does not

    def test_control_characters_and_whitespace_are_cleaned(self):
        self.assertEqual(todos._text("a\x1b[31mred\x07  line\nbreak"), "a [31mred line break")


# ── Sort and keep ─────────────────────────────────────────────────────────────
class SortKeep(unittest.TestCase):
    def test_sorts_by_priority_then_due_then_newest_updated(self):
        items = [
            todos._item("a", "1", "none pri, old", priority="none", updated=1),
            todos._item("a", "2", "urgent, no due", priority="urgent", updated=5),
            todos._item("a", "3", "high due soon", priority="high", due=100, updated=2),
            todos._item("a", "4", "high due later", priority="high", due=200, updated=2),
            todos._item("a", "5", "none pri, new", priority="none", updated=9),
        ]
        items.sort(key=todos._sort_key)
        self.assertEqual([it["ref"] for it in items], ["2", "3", "4", "5", "1"])

    def test_keep_cuts_the_record_but_total_counts_everything(self):
        c = ctx(host(""), env={})
        with mock.patch.object(todos, "_PROVIDERS", (("file", lambda ctx, cfg, prev: {
                "items": [todos._item("file", str(i), "t%d" % i) for i in range(5)], "ok": True, "ms": 1, "error": ""}),)):
            c.cfg = {"keep": 2, "ask_pattern": "", "ask_systems": [], "file": {"paths": ["x"]}}
            data = todos.todos(c)
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["items"]), 2)


# ── Asks: the "track every ask" view ─────────────────────────────────────────
class Asks(unittest.TestCase):
    def test_apply_asks_sets_item_asks_and_builds_the_ids_map(self):
        items = [todos._item("badland", "1", "V-43 ship it", state="open"),
                 todos._item("drover", "2", "V-43/V-44 in progress", state="in-progress")]
        ids, err = todos._apply_asks(items, r"\bV-\d{2,3}\b")
        self.assertIsNone(err)
        self.assertEqual(items[0]["asks"], ["V-43"])
        self.assertEqual(sorted(items[1]["asks"]), ["V-43", "V-44"])
        self.assertEqual(ids["V-43"], {"badland": "open", "drover": "in-progress"})
        self.assertEqual(ids["V-44"], {"drover": "in-progress"})

    def test_no_pattern_sets_empty_asks_and_no_error(self):
        items = [todos._item("badland", "1", "V-43 ship it")]
        ids, err = todos._apply_asks(items, "")
        self.assertEqual((ids, err, items[0]["asks"]), ({}, None, []))

    def test_an_invalid_pattern_is_reported_not_raised(self):
        items = [todos._item("badland", "1", "V-43 ship it")]
        ids, err = todos._apply_asks(items, "(unterminated[")
        self.assertEqual((ids, items[0]["asks"]), ({}, []))
        self.assertTrue(err)
        doc = todos._asks_doc("(unterminated[", ["badland"], ids, err, {})
        self.assertEqual(doc, {"pattern": "(unterminated[", "ids": {}, "systems": [], "error": err})

    def test_systems_are_only_the_providers_that_answered_this_run_in_ask_systems_order(self):
        providers = {"badland": {"ok": True}, "github": {"ok": False}, "drover": {"ok": True}}
        with mock.patch.object(todos, "_brain_recent_titles", return_value=[]):
            doc = todos._asks_doc(r"\bV-\d{2,3}\b", ["badland", "github", "drover", "brain"], {}, None, providers)
        self.assertEqual(doc["systems"], ["badland", "drover"])  # github failed: excluded; order preserved

    def test_brain_notes_ids_from_knowledge_recent_titles_but_claims_no_gaps(self):
        # A few recent titles are not a full read of the brain: they mark asks "noted" but
        # never make the brain a system an ask can be "missing" from.
        with mock.patch.object(todos, "_brain_recent_titles", return_value=["notes on V-39 and the panel"]):
            doc = todos._asks_doc(r"\bV-\d{2,3}\b", ["badland", "brain"], {}, None, {"badland": {"ok": True}})
        self.assertEqual(doc["ids"]["V-39"], {"brain": "noted"})
        self.assertNotIn("brain", doc["systems"])

    def test_overdue_alerts_only_for_high_or_urgent_overdue_more_than_a_day(self):
        now = core.now()
        items = [todos._item("badland", "1", "barely late", priority="high", due=now - 3600),       # < 1 day: no alert
                 todos._item("badland", "2", "very late", priority="high", due=now - 2 * 86400),     # alert
                 todos._item("badland", "3", "late but low", priority="low", due=now - 2 * 86400)]   # not high/urgent
        alerts = todos._overdue_alerts(items)
        self.assertEqual(len(alerts), 1)
        self.assertIn("very late", alerts[0]["msg"])
        self.assertEqual(alerts[0]["sev"], "warn")


# ── Write handlers: policy and effects ───────────────────────────────────────
class Writes(unittest.TestCase):
    def _act(self, provider_id):
        from porchlight.ui import Action
        return Action("todos.done", "mark done", "d", {"id": provider_id}, writes=True)

    def test_refused_without_allow_writes(self):
        r = actions.run(host(""), self._act("badland:i1"), confirmed=True)
        self.assertFalse(r.ok)
        self.assertIn("allow_writes", r.msg)

    def test_badland_done_reads_then_toggles_only_if_not_already_complete(self):
        h = host("[interactive]\nallow_writes = true\n[sources.todos.badland]\nbase_url = \"https://api.example.com\"\n"
                 "web_url = \"https://example.com\"\n")
        fake = support.FakeHTTP({
            "api.example.com/api/todos/items/i1": resp(200, {"success": True, "data": {"isCompleted": False}}),
            "api.example.com/api/todos/items/i1/toggle": resp(200, {"success": True}),
        })
        with mock.patch.object(secrets, "_ENV", {"BADLAND_API_KEY": "tok"}), mock.patch.object(core, "http", fake):
            r = actions.run(h, self._act("badland:i1"), confirmed=True)
        self.assertTrue(r.ok)
        self.assertEqual([m for m, _, _, _ in fake.calls], ["GET", "POST"])

    def test_an_already_complete_badland_item_is_never_toggled(self):
        h = host("[interactive]\nallow_writes = true\n[sources.todos.badland]\nbase_url = \"https://api.example.com\"\n"
                 "web_url = \"https://example.com\"\n")
        fake = support.FakeHTTP({
            "api.example.com/api/todos/items/i1": resp(200, {"success": True, "data": {"isCompleted": True}}),
        })
        with mock.patch.object(secrets, "_ENV", {"BADLAND_API_KEY": "tok"}), mock.patch.object(core, "http", fake):
            r = actions.run(h, self._act("badland:i1"), confirmed=True)
        self.assertTrue(r.ok)
        self.assertEqual([u for _, u, _, _ in fake.calls], ["https://api.example.com/api/todos/items/i1"])  # no toggle

    def test_drover_done_calls_features_upsert_shipped(self):
        d, env = daemon_state()
        self.addCleanup(shutil.rmtree, d, True)
        daemon = DroverDaemon({"features.upsert": {"id": "ev-1"}})
        h = host("[interactive]\nallow_writes = true\n")
        with mock.patch.object(secrets, "_ENV", dict(env)), mock.patch.object(core, "http", daemon):
            r = actions.run(h, self._act("drover:feat-1"), confirmed=True)
        self.assertTrue(r.ok)
        self.assertEqual(daemon.reqs[0]["body"], {"fid": "feat-1", "state": "shipped"})

    def test_reflections_done_calls_reflections_mark(self):
        d, env = daemon_state()
        self.addCleanup(shutil.rmtree, d, True)
        daemon = DroverDaemon({"reflections.mark": {"marked": ["r-1"]}})
        h = host("[interactive]\nallow_writes = true\n")
        with mock.patch.object(secrets, "_ENV", dict(env)), mock.patch.object(core, "http", daemon):
            r = actions.run(h, self._act("reflections:r-1"), confirmed=True)
        self.assertTrue(r.ok)
        self.assertEqual(daemon.reqs[0]["body"], {"op": "done", "ids": ["r-1"]})

    def test_github_done_closes_the_issue(self):
        fake = support.FakeHTTP({"api.github.com/repos/acme/x/issues/7": resp(200, {})})
        h = host("[interactive]\nallow_writes = true\n")
        with mock.patch.object(secrets, "_ENV", {"GH_TOKEN": "tok"}), mock.patch.object(core, "http", fake):
            r = actions.run(h, self._act("github:acme/x#7"), confirmed=True)
        self.assertTrue(r.ok)
        method, url, headers, kw = fake.calls[0]
        self.assertEqual((method, url), ("PATCH", "https://api.github.com/repos/acme/x/issues/7"))
        self.assertEqual(kw["json_body"], {"state": "closed"})

    def test_file_is_unsupported(self):
        h = host("[interactive]\nallow_writes = true\n")
        r = actions.run(h, self._act("file:/tmp/todo.txt:3"), confirmed=True)
        self.assertFalse(r.ok)

    def test_a_result_message_never_carries_a_credential(self):
        h = host("[interactive]\nallow_writes = true\n")
        with mock.patch.object(secrets, "_ENV", {"GH_TOKEN": CANARY}), \
             mock.patch.object(core, "http", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("token " + CANARY))):
            r = actions.run(h, self._act("github:acme/x#7"), confirmed=True)
        self.assertFalse(r.ok)
        self.assertNotIn(CANARY, r.msg)


# ── check_settings: a full config declares every setting ────────────────────
class Settings(unittest.TestCase):
    def test_a_full_sources_todos_table_has_no_unknown_setting(self):
        text = """
[sources.todos]
keep = 50
shown = 4
ask_pattern = '\\bV-\\d{2,3}\\b'
ask_systems = ["badland", "github", "drover", "brain"]
[sources.todos.badland]
base_url = "https://api.example.com"
web_url = "https://example.com"
key_env = "BADLAND_API_KEY"
lists = ["Voice Gateway"]
[sources.todos.drover]
enabled = true
states = ["not-started", "in-progress"]
[sources.todos.reflections]
enabled = true
kinds = ["action", "question"]
[sources.todos.github]
owner = "acme"
labels = ["voice-gateway"]
query = ""
token_env = ["GH_TOKEN", "GITHUB_TOKEN"]
[sources.todos.file]
paths = ["~/todo.txt"]
"""
        cfg = config.loads(text)
        self.assertEqual(cfg.problems + registry.check_settings(cfg), [])


# ── The panel ─────────────────────────────────────────────────────────────────
def demo_record():
    with open(os.path.join(support.ROOT, "porchlight", "data", "demo", "todos.json"), encoding="utf-8") as f:
        raw = json.load(f)
    from porchlight import demo as pdemo
    return pdemo._relative(raw["todos"], time.time())


class Panel(unittest.TestCase):
    def setUp(self):
        from porchlight import theme as T
        clear_cache()
        self.addCleanup(T.set_mode, "truecolor")
        self.addCleanup(T.set_ascii, False)

    def build(self, data=None, cw=100, toml=""):
        put("todos", demo_record() if data is None else data)
        return BUILDERS["todos"](RCtx(host(toml), Cache(), {"vitals": {}}, [], cw))

    def test_renders_the_summary_items_and_asks_rows(self):
        p = self.build()
        from porchlight import theme as T
        rows = [T.strip(ln) for ln in p.lines]
        self.assertTrue(rows[0].startswith("open     badland 4"))
        self.assertIn("github (no GitHub token", rows[0])
        self.assertTrue(any(r.startswith("items") for r in rows))
        self.assertTrue(any(r.startswith("asks") for r in rows))
        self.assertEqual(p.level, "warn")  # github is down
        self.assertIn("9 open", p.note)
        self.assertEqual(p.items[0].id, "todos:badland:bl-2")
        self.assertTrue(any(a.name == "todos.done" for a in p.items[0].actions))

    def test_renders_at_several_widths_without_crashing(self):
        for cw in (40, 80, 140):
            p = self.build(cw=cw)
            self.assertTrue(p.lines)
            rendered = p.render(cw, ("sky",))
            self.assertTrue(rendered)

    def test_renders_in_ascii_mode(self):
        from porchlight import theme as T
        T.set_ascii(True)
        p = self.build(cw=80)
        text = "\n".join(p.lines)
        self.assertNotIn("●", text)
        self.assertNotIn("○", text)

    def test_an_empty_record_does_not_crash(self):
        p = self.build(data={"v": 1, "items": [], "providers": {}, "total": 0})
        self.assertEqual(p.level, "ok")
        self.assertEqual(p.items, [])

    def test_an_error_only_record_does_not_crash(self):
        p = self.build(data={"v": 1, "items": [], "providers": {
            "badland": {"ok": False, "open": 0, "ms": 0, "error": "no BADLAND_API_KEY"}}, "total": 0})
        self.assertEqual(p.level, "warn")

    def test_a_missing_record_hides_the_panel(self):
        clear_cache()
        p = BUILDERS["todos"](RCtx(host(""), Cache(), {"vitals": {}}, [], 100))
        self.assertIsNone(p)

    def test_an_invalid_ask_pattern_shows_as_an_amber_note_not_a_crash(self):
        data = demo_record()
        data["asks"] = {"pattern": "(bad[", "ids": {}, "systems": [], "error": "invalid pattern"}
        p = self.build(data=data)
        from porchlight import theme as T
        self.assertTrue(any("invalid pattern" in T.strip(ln) for ln in p.lines))


# ── `porchlight view` ─────────────────────────────────────────────────────────
class View(unittest.TestCase):
    def test_view_build_includes_todos_items_and_actions(self):
        clear_cache()
        put("todos", demo_record())
        h = host("")
        doc = view.build(h, Cache(), only=["todos"])
        p = next(p for p in doc["panels"] if p["key"] == "todos")
        self.assertTrue(p["items"])
        ids = {it["id"] for it in p["items"]}
        self.assertIn("todos:badland:bl-2", ids)
        self.assertNotIn(CANARY, json.dumps(doc))


if __name__ == "__main__":
    unittest.main()

"""`porchlight view` (view.py), the Item/Action model (ui.py) and the action policy (actions.py).

Offline: a scratch cache, panels registered from the built-ins plus one test panel
that carries items.
"""
import support  # noqa: F401 — first: scratch cache and config

import json
import unittest
from unittest import mock

from porchlight import actions, panels, render, view  # noqa: F401 — panels registers the built-ins
from porchlight.registry import Cache
from porchlight import ui
from porchlight.ui import Action, Item, Panel
from support import clear_cache, host

CANARY = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2  # a token shape, built at runtime


def _viewtest(x):
    items = [Item("viewtest:1", "first item " + CANARY, detail=["detail line"], level="warn",
                  url="https://example.com/1",
                  actions=[Action("viewtest.write", "do it", "d", {"n": 1}, writes=True, confirm="Do it?")])]
    return Panel("viewtest", "VIEWTEST", ["\033[31mred row\033[0m"], "warn", "note", items)


calls = []


def setUpModule():
    # Registered only while this module runs: a global test panel would show up in every
    # other module's auto layout (the demo render, the no-color check).
    ui.BUILDERS["viewtest"], ui.TITLES["viewtest"] = _viewtest, "VIEWTEST"


def tearDownModule():
    ui.BUILDERS.pop("viewtest", None)
    ui.TITLES.pop("viewtest", None)
    actions.HANDLERS.pop("viewtest.write", None)
    actions.HANDLERS.pop("viewtest.boom", None)


@actions.action("viewtest.write", writes=True)
def _write(h, args):
    calls.append(args)
    return actions.Result(True, "done with " + CANARY)


@actions.action("viewtest.boom")
def _boom(h, args):
    raise RuntimeError("exploded " + CANARY)


class View(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def test_document_shape_plain_text_and_masked(self):
        h = host('[layout]\npanels = ["viewtest"]\n')
        doc = view.build(h, Cache(), only=["viewtest"])
        self.assertEqual((doc["schema"], doc["v"]), ("porchlight-view", 1))
        self.assertEqual(doc["fleet"]["role"], "solo")
        p = doc["panels"][0]
        self.assertEqual(p["key"], "viewtest")
        self.assertEqual(p["lines"], ["red row"])  # ANSI removed
        it = p["items"][0]
        self.assertEqual(it["id"], "viewtest:1")
        self.assertEqual(it["actions"][0]["name"], "viewtest.write")
        self.assertTrue(it["actions"][0]["writes"])
        self.assertNotIn(CANARY, json.dumps(doc))

    def test_view_never_moves_the_last_visit_bookmark(self):
        h = host('[layout]\npanels = ["viewtest"]\n')
        with mock.patch.object(render, "visit", wraps=render.visit) as v:
            view.build(h, Cache(), only=["viewtest"])
        self.assertFalse(v.call_args[0][1])  # login=False

    def test_peer_reports_the_hub_and_bundle_age(self):
        h = host("")
        c = Cache()
        c.bundle = {"from": "atlas", "ts": 1000.0}
        with mock.patch("porchlight.core.now", return_value=1300.0):
            info = view.fleet_info(h, c)
        self.assertEqual(info, {"role": "peer", "hub": "atlas", "bundle_age": 300.0})


class Policy(unittest.TestCase):
    def setUp(self):
        calls.clear()

    def act(self, **kw):
        return Action("viewtest.write", "do it", "d", {"n": 1}, **kw)

    def test_writes_are_refused_by_default(self):
        r = actions.run(host(""), self.act(writes=True), confirmed=True)
        self.assertFalse(r.ok)
        self.assertIn("allow_writes", r.msg)
        self.assertEqual(calls, [])

    def test_a_handler_that_writes_is_gated_even_if_the_action_says_it_does_not(self):
        r = actions.run(host(""), self.act(writes=False), confirmed=True)
        self.assertFalse(r.ok)
        self.assertEqual(calls, [])

    def test_writes_need_confirmation(self):
        h = host("[interactive]\nallow_writes = true\n")
        self.assertFalse(actions.run(h, self.act(writes=True)).ok)
        r = actions.run(h, self.act(writes=True), confirmed=True)
        self.assertTrue(r.ok)
        self.assertEqual(calls, [{"n": 1}])
        self.assertNotIn(CANARY, r.msg)

    def test_a_raising_handler_is_a_masked_failure(self):
        r = actions.run(host(""), Action("viewtest.boom", "boom", "b"))
        self.assertFalse(r.ok)
        self.assertIn("RuntimeError", r.msg)
        self.assertNotIn(CANARY, r.msg)

    def test_unknown_action(self):
        self.assertFalse(actions.run(host(""), Action("nope", "nope", "n")).ok)

    def test_run_only_starts_allowed_programs(self):
        r = actions.run(host(""), Action("run", "rm", "r", {"argv": ["rm", "-rf", "/nonexistent"]}))
        self.assertFalse(r.ok)
        self.assertIn("run_allow", r.msg)
        r = actions.run(host(""), Action("run", "bad", "r", {"argv": "tmux attach"}))
        self.assertFalse(r.ok)

    def test_open_url_rejects_other_schemes(self):
        self.assertFalse(actions.run(host(""), Action("open_url", "open", "o", {"url": "file:///etc/passwd"})).ok)


if __name__ == "__main__":
    unittest.main()

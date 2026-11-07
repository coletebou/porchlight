"""The ai_chats source (ai-cli's `ai sessions --json`) and the AI CHATS panel."""
import json
import time
import unittest
from unittest import mock

import support  # noqa: F401 — must be first: isolates cache/config
from support import host, put

from porchlight import registry, theme as T
from porchlight.panels import BUILDERS
from porchlight.sources import ai_chats as A
from porchlight.ui import RCtx

ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
DOC = {"version": 1, "dir": "~/.local/share/ai-cli/chats", "skipped": 1, "sessions": [
    {"name": "rust-lifetimes", "updated": ISO, "turns": 6, "providers": ["claude", "gpt"]},
    {"name": "sk-" + "x" * 40, "updated": ISO, "turns": 1, "providers": []},
    {"name": "", "turns": 2}, "junk", {"name": "solo", "model": "grok-4", "turns": True, "providers": 7}]}


class Runner:
    def __init__(self, rc=0, out="", err=""):
        self.ans, self.calls = (rc, out, err), []

    def __call__(self, argv, timeout=4.0, env=None, **kw):
        self.calls.append(list(argv))
        return self.ans


class Source(unittest.TestCase):
    def setUp(self):
        support.clear_cache()

    def run_src(self, runner, toml=""):
        h = host(toml)
        c = support.ctx(h, h.cfg.source("ai_chats", A.DEFAULTS))
        c.run = runner
        with mock.patch("porchlight.core.which", lambda b, *a, **k: "/usr/bin/" + b):
            return A.ai_chats(c)

    def test_reads_the_list_with_a_limit(self):
        r = Runner(out=json.dumps(DOC))
        d = self.run_src(r, "[sources.ai_chats]\nlimit = 3\n")
        self.assertEqual(r.calls, [["/usr/bin/ai", "sessions", "--json", "--limit", "3"]])
        self.assertEqual([s["name"] for s in d["sessions"]], ["rust-lifetimes", "sk-…", "solo"])
        a = d["sessions"][0]
        self.assertEqual((a["providers"], a["turns"]), (["claude", "gpt"], 6))
        self.assertAlmostEqual(a["updated"], time.time() - 3600, delta=5)
        self.assertEqual(d["sessions"][2]["turns"], 0)  # a bool is not a count
        self.assertEqual(d["skipped"], 1)
        self.assertNotIn("x" * 40, json.dumps(d))

    def test_an_unknown_version_or_a_failure_raises(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_src(Runner(out=json.dumps({"version": 2})))
        self.assertIn("unknown output version 2", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            self.run_src(Runner(rc=2, err="error: unrecognized subcommand 'sessions'\n"))
        self.assertIn("unrecognized subcommand", str(cm.exception))

    def test_off_without_the_binary(self):
        h = host("")
        with mock.patch("porchlight.core.which", lambda *a, **k: None):
            self.assertEqual(registry.state(registry.SOURCES["ai_chats"], h), "no ai")


class Panel(unittest.TestCase):
    def build(self, data, cw=70):
        support.clear_cache()
        put("ai_chats", data)
        return BUILDERS["ai_chats"](RCtx(host(""), registry.Cache(), {"vitals": {}}, [], cw))

    def test_rows_items_and_widths(self):
        data = {"sessions": [{"name": "chat-%d" % n, "providers": ["claude", "gpt"], "turns": n,
                              "updated": time.time() - 60 * n} for n in range(7)], "skipped": 2}
        p = self.build(data)
        text = [T.strip(ln) for ln in p.lines]
        self.assertTrue(text[0].startswith("chat-0  claude, gpt"), text[0])
        self.assertIn("+2 more", text[5])
        self.assertIn("2 unreadable chat files skipped", text[6])
        self.assertEqual(len(p.items), 7)
        for cw in (24, 40, 70):
            for ln in self.build(data, cw).render(cw, T.ACCENT):
                self.assertLessEqual(T.width(ln), cw)

    def test_empty_and_malformed(self):
        self.assertIn("no saved chats yet", T.strip(self.build({"sessions": []}).lines[0]))
        for d in ({"sessions": "x"}, {"sessions": [{"name": None, "turns": "x", "updated": "x", "providers": 3}]}, {}):
            self.build(d)  # never raises


if __name__ == "__main__":
    unittest.main()

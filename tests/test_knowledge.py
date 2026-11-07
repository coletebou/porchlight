"""The knowledge source and KNOWLEDGE panel: brain, search index, reflections per repo, note
folders and links through a fake Drover daemon; nothing here touches the network."""
import json
import os
import shutil
import tempfile
import time
import unittest

import support  # noqa: F401 — must be first: isolates cache/config
from support import host, put
from test_todos import MAIN, SKTOK, Daemon, q

from porchlight import registry, theme as T
from porchlight.panels import BUILDERS
from porchlight.sources import knowledge as K
from porchlight.ui import RCtx

NOW_ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
OLD_ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400))

STATUS = {"url": "https://brain.example.com/api/mcp", "keyConfigured": True, "keySource": "env",
          "health": {"status": 200, "ok": True, "stale": False, "entries": 2129, "chunks": 4194}}
CONTEXT = {"standing": [], "recentDecisions": [
    {"slug": "d-1", "title": "Ship the voice gateway", "validFrom": NOW_ISO},
    {"slug": "d-2", "title": "Rotate %s soon" % SKTOK, "validFrom": OLD_ISO},
    {"slug": "d-3", "title": "Third", "validFrom": OLD_ISO}, {"title": ""}, "junk"]}
SEARCH = {"host": "box", "enabled": True, "index": {"chunks": 73911, "sources": 1164, "newest": NOW_ISO, "vectors": 0,
                                                    "kinds": {"claude": {"chunks": 5526}, "doc": {"chunks": 435}}}}
REPOS = [{"repo": "dotfiles", "open": 107, "stale": 28, "done": 65, "notes": 451, "last": NOW_ISO},
         {"repo": "workrepo", "open": 300, "stale": 1, "done": 2, "notes": 3, "last": NOW_ISO},
         {"repo": "scratch-77", "open": 5, "stale": 0, "done": 0, "notes": 0, "last": NOW_ISO},
         {"repo": "quiet", "open": 0, "stale": 3, "done": 9, "notes": 1, "last": OLD_ISO}, "junk"]
OPEN_LIST = {"items": [
    {"id": "a", "repo": "workrepo", "path": "/h/df/identities/work/claude/memory/workrepo/reflections/x.jsonl"},
    {"id": "b", "repo": "workrepo", "path": "/h/df/identities/work/claude/memory/workrepo/reflections/x.jsonl"},
    {"id": "c", "repo": "dotfiles", "path": "/h/df/identities/personal/claude/memory/dotfiles/reflections/x.jsonl"},
    {"id": "d", "repo": "mixed", "path": "/h/df/identities/work/m"}, {"id": "e", "repo": "mixed", "path": "/h/df/x"}]}


def daemon(**over):
    res = {"brain.status": STATUS, "brain.context": CONTEXT, "search.status": SEARCH, "reflections.repos": REPOS,
           "reflections.list": OPEN_LIST}
    res.update(over)
    return Daemon(**res)


class Base(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.dir = tempfile.mkdtemp(prefix="knowledge-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.state)
        with open(os.path.join(self.state, "daemon.json"), "w") as f:
            json.dump({"url": "http://127.0.0.1:7337/", "pid": 4242}, f)
        with open(os.path.join(self.state, "token"), "w") as f:
            f.write(MAIN + "\n")

    def host(self, extra=""):
        return host("[sources.knowledge]\nenabled = true\nstate_dir = %s\n%s\n" % (q(self.state), extra))

    def run_src(self, extra="", http=None, env=None):
        h = self.host(extra)
        return K.knowledge(support.ctx(h, h.cfg.source("knowledge", K.DEFAULTS), env or {}, None, http or daemon()))


class Source(Base):
    def test_everything_through_the_daemon_with_the_main_token(self):
        d = daemon()
        data = self.run_src(http=d)
        self.assertEqual(sorted(d.ops()), ["brain.context", "brain.status", "reflections.repos", "search.status"])
        self.assertTrue(all(r["token"] == MAIN for r in d.reqs))
        b = data["brain"]
        self.assertEqual((b["configured"], b["ok"], b["stale"], b["entries"]), (True, True, False, 2129))
        self.assertEqual([x["slug"] for x in b["decisions"]], ["d-1", "d-2", "d-3"])
        self.assertEqual(data["search"]["chunks"], 73911)
        self.assertEqual(data["search"]["kinds"], {"claude": 5526, "doc": 435})
        self.assertEqual([r["repo"] for r in data["reflections"]], ["workrepo", "dotfiles", "scratch-77", "quiet"])
        self.assertEqual(data["links"], [{"name": "drover", "url": "http://127.0.0.1:7337/"}])
        self.assertNotIn("alerts", data)

    def test_the_record_holds_no_token_and_masks_token_shapes(self):
        blob = json.dumps(self.run_src())
        self.assertNotIn(MAIN, blob)
        self.assertNotIn(SKTOK, blob)
        self.assertIn("Rotate sk-", blob)

    def test_decisions_setting(self):
        self.assertEqual(len(self.run_src("decisions = 1")["brain"]["decisions"]), 1)
        d = daemon()
        self.assertEqual(self.run_src("decisions = 0", http=d)["brain"]["decisions"], [])
        self.assertNotIn("brain.context", d.ops())

    def test_exclude_repos_takes_globs(self):
        got = self.run_src('exclude_repos = ["WorkRepo", "scratch-*"]')
        self.assertEqual([r["repo"] for r in got["reflections"]], ["dotfiles", "quiet"])

    def test_exclude_paths_drops_repos_whose_open_items_are_all_outside(self):
        d = daemon()
        got = self.run_src('exclude_paths = ["/identities/work/"]', http=d)
        self.assertIn("reflections.list", d.ops())
        lst = [r for r in d.reqs if r["op"] == "reflections.list"][0]["body"]
        self.assertEqual(lst, {"all": True, "status": "open", "limit": 1000})
        # workrepo: every open item is a work path → out; quiet: nothing open says whose it is → out
        self.assertEqual([r["repo"] for r in got["reflections"]], ["dotfiles", "scratch-77"])
        self.assertNotIn("identities", json.dumps(got))

    def test_parts_can_be_turned_off(self):
        d = daemon()
        data = self.run_src("brain = false\nsearch = false\nreflections = false\ndrover_link = false", http=d)
        self.assertEqual(d.reqs, [])
        self.assertEqual(data, {"error": "nothing configured", "errors": {}})

    def test_one_failing_part_is_its_own_error(self):
        d = Daemon(**{"brain.status": STATUS, "brain.context": CONTEXT, "reflections.repos": REPOS})
        data = self.run_src(http=d)
        self.assertIn("search", data["errors"])
        self.assertIn("HTTP 404", data["errors"]["search"])
        self.assertEqual(data["brain"]["entries"], 2129)

    def test_no_daemon(self):
        os.unlink(os.path.join(self.state, "daemon.json"))
        data = self.run_src()
        self.assertEqual(data, {"error": "no Drover daemon here", "errors": {"drover": "no Drover daemon here"}})

    def test_alerts(self):
        bad = dict(STATUS, health={"status": 503, "ok": False})
        self.assertEqual(self.run_src(http=daemon(**{"brain.status": bad}))["alerts"][0]["sev"], "warn")
        empty = dict(STATUS, health={"status": 200, "ok": True, "entries": 0})
        self.assertIn("empty", self.run_src(http=daemon(**{"brain.status": empty}))["alerts"][0]["msg"])
        old = {"enabled": True, "index": {"chunks": 5, "newest": OLD_ISO}}
        got = self.run_src(http=daemon(**{"search.status": old}))["alerts"]
        self.assertEqual([(a["sev"], a["fix"]) for a in got], [("info", "drover op search.reindex")])
        unconfigured = dict(STATUS, keyConfigured=False, health={"ok": False})
        self.assertNotIn("alerts", self.run_src(http=daemon(**{"brain.status": unconfigured})))

    def test_links_and_notes(self):
        notes = os.path.join(self.dir, "wiki")
        os.makedirs(os.path.join(notes, "sub"))
        os.makedirs(os.path.join(notes, ".obsidian"))
        for n, name in enumerate(("a.md", "sub/b.md", "c.txt", ".obsidian/hidden.md")):
            p = os.path.join(notes, name)
            with open(p, "w") as f:
                f.write("secret body %s" % SKTOK)
            os.utime(p, (time.time() - n * 60, time.time() - n * 60))
        data = self.run_src('notes = [%s, "/nope/missing"]\nlinks = [{name = "wiki", url = "https://wiki.example.com"},'
                            ' {name = "bad", url = "javascript:alert(1)"}]' % q(notes))
        wiki, missing = data["notes"]
        self.assertEqual((wiki["name"], wiki["notes"]), ("wiki", 2))
        self.assertEqual([r["title"] for r in wiki["recent"]], ["a", "b"])
        self.assertEqual(missing["error"], "not found")
        self.assertNotIn("secret body", json.dumps(data))
        self.assertEqual([ln["name"] for ln in data["links"]], ["wiki", "drover"])


class Panel(Base):
    def build(self, data, cw=70):
        support.clear_cache()
        put("knowledge", data)
        return BUILDERS["knowledge"](RCtx(self.host(), registry.Cache(), {"vitals": {}}, [], cw))

    def test_rows_and_items(self):
        p = self.build(self.run_src('links = [{name = "wiki", url = "https://wiki.example.com"}]'))
        text = [T.strip(ln) for ln in p.lines]
        self.assertTrue(text[0].startswith("brain    2129 entries · fresh"), text)
        self.assertIn("Ship the voice gateway", text[1])
        self.assertIn("+1 decisions", "\n".join(text))
        self.assertTrue(any(ln.startswith("index    73.9k chunks") for ln in text), text)
        self.assertTrue(any(ln.startswith("refl     412 open workrepo 300") for ln in text), text)
        self.assertTrue(any(ln.startswith("links    wiki · drover") for ln in text), text)
        keys = [i["key"] for i in p.items]
        self.assertIn("decision:d-1", keys)
        self.assertIn("refl-repo:dotfiles", keys)
        link = [i for i in p.items if i["key"] == "link:wiki"][0]
        self.assertEqual((link["url"], link["actions"]), ("https://wiki.example.com", ["open"]))
        self.assertEqual(p.level, "ok")

    def test_every_width_fits(self):
        data = self.run_src()
        for cw in (24, 40, 58, 90):
            p = self.build(data, cw)
            for ln in p.render(cw, T.ACCENT):
                self.assertLessEqual(T.width(ln), cw, "%d: %r" % (cw, T.strip(ln)))

    def test_unhealthy_brain_warns(self):
        bad = dict(STATUS, health={"status": 503, "ok": False})
        p = self.build(self.run_src(http=daemon(**{"brain.status": bad})))
        self.assertEqual(p.level, "warn")
        self.assertIn("unreachable", T.strip(p.lines[0]))

    def test_malformed_records_never_break_the_panel(self):
        for d in ({"brain": "x"}, {"brain": {"decisions": "x", "entries": "many"}}, {"search": {"chunks": None}},
                  {"reflections": [1, {"repo": None, "open": "x"}]}, {"notes": [{"recent": "x"}]}, {"links": [{"url": 3}]},
                  {"errors": {"a": {"b": 1}}}, {"error": "no Drover daemon here"}):
            try:
                self.build(d)
            except Exception as e:  # noqa: BLE001
                self.fail("%r raised %r" % (d, e))

    def test_no_record_no_panel(self):
        support.clear_cache()
        self.assertIsNone(BUILDERS["knowledge"](RCtx(self.host(), registry.Cache(), {"vitals": {}}, [], 70)))


if __name__ == "__main__":
    unittest.main()

"""The Drover source (sources/drover.py) and the DROVER panel (panels/drover.py).

Offline: a fake daemon answers ops through a patched Ctx.http and records the token
every request carried; the daemon's state directory (daemon.json, token,
widget-token) is a temporary one. Every name is synthetic.
"""
import support  # noqa: F401 — first: scratch cache and config

import json
import os
import re
import shutil
import tempfile
import time
import unittest
from unittest import mock

from porchlight import collect, core, registry, render, secrets, theme as T
from porchlight.panels import drover as pdrover  # noqa: F401 — registers the panel
from porchlight.sources import drover
from porchlight.ui import BUILDERS, RCtx
from support import host, put

NOW = time.time()
HUB = '[general]\nlabel = "atlas"\n[fleet]\npeers = ["web-1", "gpu-box"]\n'
PEER = '[general]\nlabel = "web-1"\n'
MAIN, WIDGET = "main-fixture-0f3c9a2b", "widget-fixture-77d1e4c0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "porchlight", "data", "demo", "drover.json")


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def summary():
    return {"generatedAt": iso(NOW), "owner": "owner",
            "hosts": [{"name": "local", "up": True, "waiting": 0, "working": 2, "idle": 3},
                      {"name": "web-1", "up": True, "waiting": 1, "working": 1, "idle": 1}],
            "waiting": [{"id": "00000000-demo-4000-8000-000000000001", "name": "demo-review", "host": "web-1",
                         "engine": "claude", "since": iso(NOW - 180), "kind": "question",
                         "headline": "Push the branch now, or wait for the second review?"}],
            "working": [{"id": "s-1", "name": "demo-build", "host": "local", "engine": "claude", "headline": "Running tests"},
                        {"id": "5ffd86aa-1111-4000-8000-000000000002", "name": "[redacted]", "host": "local",
                         "engine": "codex", "headline": ""},
                        {"id": "s-3", "name": "deploy web-1", "host": "web-1", "engine": "claude", "headline": ""}],
            "totals": {"waiting": 1, "working": 3, "idle": 4, "hosts": 2}, "manager": {"up": True, "lastNoteAt": None}}


def note(nid, at, kind, title, body="", sid="s-x", **kw):
    return dict({"id": nid, "at": iso(at), "kind": kind, "category": kind, "title": title, "body": body,
                 "sessionId": sid, "host": "local"}, **kw)


VERSIONS = {"ts": iso(NOW), "hosts": [
    {"host": "local", "ok": True, "state": "live", "loaded": "4f2a9c1e0b", "checkout": {"path": "/srv/drover", "commit": "4f2a9c1e0b"}},
    {"host": "gpu-box", "ok": True, "state": "restart", "loaded": "9b1e0d7aa1",
     "checkout": {"path": "/srv/drover", "commit": "4f2a9c1e0b"}},
    {"host": "web-1", "ok": False, "error": "ssh: timed out"}]}
POOL = {"recommendation": "main", "stateFile": {"path": "/private/state/credentials.json", "error": None}, "pool": [
    {"id": "main", "label": "Main", "env": {"API_TOKEN": "$API_TOKEN"}, "available": True, "active": 2, "maxConcurrent": 12,
     "recentThrottles": 1, "missing": []},
    {"id": "backup", "available": False, "active": 0, "maxConcurrent": 12, "recentThrottles": 0, "missing": ["BACKUP_API_TOKEN"]}]}


class Daemon:
    """A Drover daemon: op → result, with the real token rules: the widget token answers
    native.summary and native.notifications and nothing else (403). Every request is
    recorded with the token it carried."""

    def __init__(self, items=(), cursor="c1.next"):
        self.items, self.cursor, self.reqs = list(items), cursor, []
        self.down = self.summary_fails = False

    def __call__(self, url, method="GET", headers=None, json_body=None, timeout=6.0, **kw):
        op = url.rsplit("/", 1)[-1]
        auth = (headers or {}).get("Authorization", "")
        tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        self.reqs.append({"op": op, "token": tok, "body": json_body, "method": method, "url": url, "timeout": timeout})
        if self.down:
            raise ConnectionRefusedError("refused")
        if tok not in (MAIN, WIDGET):
            return core.Resp(401, {"ok": False, "error": "unauthorized"}, 1)
        if tok == WIDGET and op not in ("native.summary", "native.notifications"):
            return core.Resp(403, {"ok": False, "error": "widget token: read-only scope"}, 1)
        if op == "native.summary" and self.summary_fails:
            return core.Resp(502, {"ok": False, "error": "bad gateway"}, 1)
        result = {"native.summary": summary(), "native.notifications": {"cursor": self.cursor, "items": self.items},
                  "fleet.versions": VERSIONS, "credentials.status": POOL}.get(op)
        if result is None:
            return core.Resp(404, {"ok": False, "error": "unknown op %s" % op}, 1)
        return core.Resp(200, {"ok": True, "result": result, "ms": 3}, 3)

    def ops(self):
        return [r["op"] for r in self.reqs]

    def tokens(self):
        return {r["op"]: r["token"] for r in self.reqs}


class Base(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.state = tempfile.mkdtemp(prefix="drover-state-")
        self.addCleanup(shutil.rmtree, self.state, True)
        with open(os.path.join(self.state, "daemon.json"), "w") as f:
            json.dump({"url": "http://127.0.0.1:7337/", "pid": 4242}, f)
        for name, tok in (("token", MAIN), ("widget-token", WIDGET)):
            with open(os.path.join(self.state, name), "w") as f:
                f.write(tok + "\n")

    def daemon(self, **kw):
        d = Daemon(**kw)
        p = mock.patch.object(registry.Ctx, "http", staticmethod(d))
        p.start()
        self.addCleanup(p.stop)
        return d

    def toml(self, base=HUB, extra=""):
        return base + '[sources.drover]\nstate_dir = "%s"\n%s' % (self.state, extra)

    def run_src(self, base=HUB, prev=None, env=None, extra=""):
        h = host(self.toml(base, extra))
        src = registry.SOURCES["drover"]
        return drover.drover(registry.Ctx(h, dict(env or {}), prev, src.config(h)))


class Source(Base):
    def test_the_widget_token_reads_the_board_and_only_the_hub_ops_use_the_main_token(self):
        d = self.daemon()
        data = self.run_src()
        self.assertEqual(d.tokens(), {"native.summary": WIDGET, "native.notifications": WIDGET,
                                      "fleet.versions": MAIN, "credentials.status": MAIN})
        for r in d.reqs:
            self.assertEqual((r["method"], r["url"]), ("POST", "http://127.0.0.1:7337/api/op/" + r["op"]))
            self.assertNotIn(MAIN, r["url"])
            self.assertNotIn(WIDGET, r["url"])
        det = data["details"]
        self.assertEqual(det["versions"][1], {"host": "gpu-box", "ok": True, "state": "restart", "loaded": "9b1e0d7",
                                              "checkout": "4f2a9c1"})
        self.assertEqual(det["versions"][2]["state"], "unreachable")
        self.assertEqual(det["pool"][1], {"id": "backup", "available": False, "active": 0, "max": 12, "throttles": 0,
                                          "missing": ["BACKUP_API_TOKEN"]})
        blob = json.dumps(data)
        for secret_or_path in (MAIN, WIDGET, "/private/state", "/srv/drover", "$API_TOKEN"):
            self.assertNotIn(secret_or_path, blob)  # tokens, paths and the env map are never kept
        d.reqs[:] = []
        self.run_src(prev=data)  # inside recheck_minutes: the hub ops wait
        self.assertEqual(sorted(d.ops()), ["native.notifications", "native.summary"])
        d.reqs[:] = []
        self.run_src(prev=dict(data, details=dict(det, at=NOW - 3600)))  # ten minutes on: asked again
        self.assertIn("fleet.versions", d.ops())

    def test_hub_ops_follow_their_settings(self):
        d = self.daemon()
        self.run_src(PEER)  # a peer never asks by default
        self.assertEqual(sorted(d.ops()), ["native.notifications", "native.summary"])
        d.reqs[:] = []
        data = self.run_src(PEER, extra="versions = true\npool = false\n")
        self.assertEqual(sorted(d.ops()), ["fleet.versions", "native.notifications", "native.summary"])
        self.assertEqual(sorted(data["details"]), ["at", "versions"])
        d.reqs[:] = []
        self.run_src(HUB, extra="versions = false\npool = false\n")
        self.assertEqual(sorted(d.ops()), ["native.notifications", "native.summary"])

    def test_without_a_widget_token_the_main_token_reads(self):
        os.remove(os.path.join(self.state, "widget-token"))
        d = self.daemon()
        self.run_src(PEER)
        self.assertEqual({r["token"] for r in d.reqs}, {MAIN})

    def test_a_widget_token_alone_never_reaches_the_hub_ops(self):
        os.remove(os.path.join(self.state, "token"))
        d = self.daemon()
        data = self.run_src()
        self.assertEqual(sorted(d.ops()), ["native.notifications", "native.summary"])
        self.assertNotIn("details", data)

    def test_names_asks_and_the_waiting_alert(self):
        self.daemon()
        data = self.run_src()
        self.assertEqual([w["name"] for w in data["working"]], ["demo-build", "#5ffd86aa", "deploy web-1"])
        self.assertEqual(data["waiting"][0]["ask"], "Push the branch now, or wait for the second review?")
        self.assertEqual(int(data["waiting"][0]["since"]), int(NOW - 180))
        self.assertEqual(data["alerts"], [{"sev": "warn", "msg": "demo-review@web-1 is waiting on you: "
                                                                 "Push the branch now, or wait for the second review?"}])
        self.assertEqual(data["totals"], {"waiting": 1, "working": 3, "idle": 4, "hosts": 2})
        self.assertEqual(data["hosts"][0], {"name": "local", "up": True, "waiting": 0, "working": 2, "idle": 3})

    def test_an_ask_drops_the_name_drover_puts_first(self):
        self.assertEqual(drover.ask_of({"name": "demo-api", "headline": "demo-api needs you: status: waiting"}), "status: waiting")
        self.assertEqual(drover.ask_of({"name": "demo-api", "headline": "demo-api needs a decision"}), "")
        self.assertEqual(drover.ask_of({"name": "demo-api", "headline": "Proceed? demo-api needs you"}), "Proceed? demo-api needs you")

    def test_several_waiting_name_the_local_host_by_its_label(self):
        waiting = [{"name": "a%d" % i, "host": "local" if i == 0 else "web-1", "ask": ""} for i in range(4)]
        self.assertEqual(drover.waiting_alert(waiting, 5, "atlas"),
                         "5 agents waiting on you (a0@atlas, a1@web-1, a2@web-1, …)")
        long_ask = [{"name": "b", "host": "web-1", "ask": "word " * 40}]
        msg = drover.waiting_alert(long_ask, 1, "atlas")
        self.assertTrue(msg.endswith("word…"), msg)  # cut between words, never inside one

    def test_drover_text_is_masked_by_pattern_only(self):
        """Regression: a secret whose value equals a word on the screen must not be masked
        there; masking it would tell the reader which secret that word is. Token shapes
        and URI credentials are masked whatever the environment holds."""
        env = {"DEMO_DB_PASSWORD": "web-1", "DEMO_API_TOKEN": "deploy-runner-0123", "PATH": os.environ.get("PATH", "")}
        self.assertTrue(secrets.masks("DEMO_DB_PASSWORD", "web-1"))  # an error string would have it redacted
        d = self.daemon()
        summ = summary()
        summ["working"].append({"id": "s-4", "name": "deploy-runner-0123", "host": "web-1"})
        summ["waiting"][0]["headline"] = ("Use sk-ant-" + "a1b2c3d4e5" * 3 + " from https://bob:hunter2@example.com/x "
                                          "for web-1?")

        def answer(url, **kw):
            r = d(url, **kw)
            return core.Resp(200, {"ok": True, "result": summ}, 1) if url.endswith("native.summary") else r
        with mock.patch.object(registry.Ctx, "http", staticmethod(answer)), mock.patch.object(secrets, "_ENV", env):
            data = self.run_src(env=env)  # the loaded secrets hold the values too, as in a real collector run
        names = [w["name"] for w in data["working"]]
        self.assertIn("deploy web-1", names)          # the password's value, left alone
        self.assertIn("deploy-runner-0123", names)     # a token-named variable's value, left alone
        self.assertEqual(data["waiting"][0]["ask"], "Use sk-ant-… from https://<redacted>@example.com/x for web-1?")
        self.assertEqual(drover.text("see https://bob:hunter2@example.com/x"), "see https://<redacted>@example.com/x")
        self.assertEqual(drover.text("a\x1b[31mred\x07 line\nbreak"), "a [31mred line break")  # no escape reaches the terminal

    def test_a_daemon_going_down_replaces_the_board_and_its_alert(self):
        d = self.daemon()
        h = host(self.toml())
        first = collect.run_one(registry.SOURCES["drover"], h, {})
        self.assertTrue(first["data"]["alerts"])
        d.down = True
        rec = collect.run_one(registry.SOURCES["drover"], h, {})
        self.assertTrue(rec["ok"])
        self.assertEqual((rec["data"]["daemon"], rec["data"].get("alerts")), ("down", None))
        self.assertEqual(rec["data"]["feed_cursor"], first["data"]["feed_cursor"])

    def test_the_collector_stores_drover_text_as_drover_wrote_it(self):
        """The whole path, through the collector: records are not value-scrubbed either."""
        self.daemon()
        h = host(self.toml())
        env = {"DEMO_DB_PASSWORD": "web-1"}
        with mock.patch.object(secrets, "_ENV", env):
            rec = collect.run_one(registry.SOURCES["drover"], h, env)
        self.assertTrue(rec["ok"], rec)
        self.assertIn("deploy web-1", [w["name"] for w in rec["data"]["working"]])
        stored = core.read_text(registry.src_path("drover"))
        self.assertNotIn(MAIN, stored)
        self.assertNotIn(WIDGET, stored)

    def test_a_daemon_that_does_not_answer_is_down_and_keeps_its_feed(self):
        d = self.daemon()
        prev = {"feed": [{"at": NOW - 60, "kind": "finished", "host": "local", "name": "a", "what": "finished", "text": "t",
                          "sid": "s-a"}], "feed_cursor": "c1.kept"}
        d.down = True
        data = self.run_src(prev=prev)
        self.assertEqual((data["daemon"], data["why"]), ("down", "refused"))
        self.assertFalse(collect._soft_failure(data))  # state, like an engine that is not running: not a kept old board
        self.assertEqual((data["feed"], data["feed_cursor"]), (prev["feed"], "c1.kept"))
        self.assertNotIn("alerts", data)  # a daemon that is down is state, not an alarm
        d.down, d.summary_fails = False, True
        self.assertEqual(self.run_src()["why"], "HTTP 502: bad gateway")
        os.remove(os.path.join(self.state, "token"))
        os.remove(os.path.join(self.state, "widget-token"))
        self.assertEqual(self.run_src(), {"daemon": "down", "why": "no token file"})
        os.remove(os.path.join(self.state, "daemon.json"))
        self.assertEqual(self.run_src(), {"daemon": "none"})

    def test_the_state_dir_comes_from_the_setting_then_drover_state(self):
        d = self.daemon()
        h = host(HUB)
        data = drover.drover(registry.Ctx(h, {"DROVER_STATE": self.state}, None, registry.SOURCES["drover"].config(h)))
        self.assertEqual(data["daemon"], "up")
        self.assertTrue(d.reqs)
        with mock.patch.object(drover, "DEFAULT_STATE", os.path.join(self.state, "nowhere")):
            self.assertEqual(drover.drover(registry.Ctx(h, {}, None, registry.SOURCES["drover"].config(h))), {"daemon": "none"})

    def test_malformed_replies_never_raise(self):
        for junk in (None, [], "x", 5, {"waiting": "x", "working": {"a": 1}, "hosts": [1, None, "x"], "totals": [],
                                        "manager": "up"}, {"waiting": [{"since": "nonsense", "name": {"x": 1}}]}):
            def answer(url, j=junk, **kw):
                op = url.rsplit("/", 1)[-1]
                return core.Resp(200, {"ok": True, "result": j if op != "native.notifications" else {"items": j, "cursor": j}}, 1)
            with mock.patch.object(registry.Ctx, "http", staticmethod(answer)):
                data = self.run_src()
            self.assertIn(data["daemon"], ("up", "down"), junk)
            json.dumps(data)


class Feed(Base):
    def test_one_entry_per_session_and_kind_kept_across_runs_with_the_cursor(self):
        d = self.daemon(items=[
            note("1", NOW - 3600, "finished", "demo-docs finished", "first pass", sid="s-docs"),
            note("2", NOW - 600, "finished", "demo-docs finished", "the second pass", sid="s-docs"),
            note("3", NOW - 300, "error", "demo-bench exited unexpectedly", "~/code/bench", sid="s-bench"),
            note("4", NOW - 200, "decision", "demo-api needs a decision", sid="s-api"),
            note("5", NOW - 100, "finished", "probe finished", "x", sid="s-t", test=True),
            note("6", NOW - 9 * 86400, "finished", "old finished", "x", sid="s-old"),
            note("7", NOW - 250, "error", "web-1 stopped answering", "ssh rpc failed", sid=None, host="web-1"),
            note("8", NOW - 240, "error", "demo-ci exited with code 2", "~/code/ci", sid="s-ci")])
        data = self.run_src(PEER)
        self.assertEqual([(f["name"], f["what"], f["text"]) for f in data["feed"]],
                         [("demo-ci", "exited with code 2", ""), ("web-1", "stopped answering", ""),
                          ("demo-bench", "exited unexpectedly", ""), ("demo-docs", "finished", "the second pass")])
        self.assertEqual(data["feed"][1]["sid"], "web-1")  # a host-level error has no session: its name is the key
        self.assertEqual(data["feed_cursor"], "c1.next")
        self.assertEqual(next(r["body"] for r in d.reqs if r["op"] == "native.notifications"), {"limit": 100})
        d.items = [note("9", NOW - 50, "finished", "demo-lint finished", "clean", sid="s-lint")]
        d.cursor = "c1.later"
        d.reqs[:] = []
        data2 = self.run_src(PEER, prev=data)
        feed_req = next(r for r in d.reqs if r["op"] == "native.notifications")
        self.assertEqual(feed_req["body"], {"limit": 100, "since": "c1.next"})  # only what is new since the last poll
        self.assertEqual([f["name"] for f in data2["feed"]], ["demo-lint", "demo-ci", "web-1", "demo-bench", "demo-docs"])
        self.assertEqual(data2["feed_cursor"], "c1.later")

    def test_a_silent_feed_keeps_what_it_had(self):
        self.daemon()
        prev = {"feed": [{"at": NOW - 60, "kind": "finished", "host": "local", "name": "a", "what": "finished", "text": "t",
                          "sid": "s-a"}], "feed_cursor": "c1.x"}
        real = Daemon()

        def no_feed(url, **kw):
            if url.endswith("native.notifications"):
                return core.Resp(500, {"ok": False}, 1)
            return real(url, **kw)
        with mock.patch.object(registry.Ctx, "http", staticmethod(no_feed)):
            data = self.run_src(PEER, prev=prev)
        self.assertEqual((data["feed"], data["feed_cursor"], data["feed_error"]), (prev["feed"], "c1.x", "HTTP 500"))

    def test_the_feed_is_bounded(self):
        self.daemon(items=[note(str(i), NOW - i * 60, "finished", "job-%d finished" % i, sid="s-%d" % i) for i in range(60)],
                    cursor="c1." + "x" * (drover.CURSOR_MAX + 1))
        data = self.run_src(PEER, prev={"feed_cursor": "c1.short"})
        self.assertEqual(len(data["feed"]), drover.FEED_KEEP)
        self.assertEqual(data["feed"][0]["name"], "job-0")
        self.assertEqual(data["feed_cursor"], "c1.short")  # an oversized cursor is not stored


# ── The panel ───────────────────────────────────────────────────────────────
def demo():
    with open(DEMO, encoding="utf-8") as f:
        raw = json.load(f)
    from porchlight import demo as pdemo
    return pdemo._relative(raw["drover"], time.time())


def words_of(obj) -> set:
    out = set()
    if isinstance(obj, dict):
        for v in obj.values():
            out |= words_of(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= words_of(v)
    elif isinstance(obj, str):
        out |= set(re.split(r"[\s,]+", obj))
    return out


class Panel(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(T.set_mode, "truecolor")
        self.addCleanup(T.set_ascii, False)

    def build(self, data=None, cw=58, last_seen=None, toml=HUB):
        put("drover", demo() if data is None else data)
        return BUILDERS["drover"](RCtx(host(toml), registry.Cache(), {"vitals": {}}, [], cw, last_seen))

    def test_rows_name_who_waits_what_finished_crashed_and_works(self):
        p = self.build(last_seen=time.time() - 3 * 3600)
        rows = [T.strip(ln) for ln in p.lines]
        self.assertTrue(rows[0].startswith("waiting  ⏸ demo-review atlas 7m · Push the branch now, or"), rows[0])
        self.assertEqual(rows[1].strip(), "wait for the second review?")  # the question wraps between words
        self.assertTrue(any(r.startswith("done     ✔ demo-docs web-1 12m · Done:") for r in rows), rows)
        self.assertFalse(any("demo-refactor" in r for r in rows))  # older than the last visit: not news
        self.assertTrue(any(r.startswith("crashed  ✖ demo-bench gpu-box 1h · exited unexpectedly") for r in rows), rows)
        self.assertTrue(any(r.startswith("working  ▶ atlas 3  demo-tests, demo-api, #5ffd86aa") for r in rows), rows)
        self.assertTrue(any(r.startswith("daemons  ● 2/3 live · gpu-box needs a restart · manager ●") for r in rows), rows)
        self.assertTrue(any(r.startswith("pool     main 3/12 · backup ✖ BACKUP_API_TOKEN unset") for r in rows), rows)
        self.assertEqual((p.level, p.note), ("warn", "3 hosts · 5 working · 4 idle"))

    def test_nothing_new_since_the_last_visit_shows_the_last_day_dimmed(self):
        rows = [T.strip(ln) for ln in self.build(last_seen=time.time() - 60).lines]
        self.assertTrue(any("demo-docs" in r for r in rows))
        self.assertTrue(any("demo-refactor" in r for r in rows))  # the last day, when nothing is newer than the visit
        self.assertTrue(any(r.startswith("crashed") for r in rows))  # a crash in the last six hours still shows

    def test_more_than_fits_is_counted(self):
        d = demo()
        d["feed"] = [{"at": time.time() - 60 * i, "kind": "finished", "host": "local", "name": "job-%d" % i, "what": "finished",
                      "text": "", "sid": "s-%d" % i} for i in range(1, 7)]
        d["waiting"] = [dict(d["waiting"][0], name="ask-%d" % i) for i in range(5)]
        d["totals"]["waiting"] = 7
        rows = [T.strip(ln) for ln in self.build(d, last_seen=time.time() - 3600).lines]
        self.assertIn("+3 more since your last visit", "\n".join(rows))
        self.assertTrue(any(r.strip().startswith("+4 more waiting: ask-3, ask-4") for r in rows), rows)

    def test_fits_every_width_without_cutting_a_word(self):
        vocab = words_of(demo())
        for mode, ascii_ in (("truecolor", False), ("256", False), ("none", True)):
            T.set_mode(mode)
            T.set_ascii(ascii_)
            for cw in (58, 64, 78, 90, 130):
                p = self.build(cw=cw, last_seen=time.time() - 3 * 3600)
                for ln in p.lines:
                    self.assertLessEqual(T.width(ln), cw - 1, "%d (%s): %r" % (cw, mode, T.strip(ln)))  # no fit() cut needed
                    plain = T.strip(ln)
                    for m in re.finditer(r"(\S*)(…|\.\.\.)", plain):
                        self.assertIn(m.group(1), vocab, "a word cut at %d: %r" % (cw, plain))
                for ln in p.render(cw, T.ACCENT):
                    self.assertLessEqual(T.width(ln), cw)
                    if ascii_:
                        self.assertTrue(all(ord(ch) < 128 for ch in T.asciify(ln)), ln)

    def test_a_long_url_is_never_cut_inside(self):
        d = demo()
        url = "https://example.com/" + "a" * 70
        d["waiting"][0]["ask"] = url + " is that the right page?"
        d["feed"][0]["text"] = "see " + url
        for cw in (58, 90):
            rows = [T.strip(ln) for ln in self.build(d, cw=cw, last_seen=time.time() - 3 * 3600).lines]
            text = "\n".join(rows)
            self.assertNotRegex(text, r"https://example\.com/a+…")  # dropped, or shown whole: never cut
            self.assertTrue(any(r.startswith("done     ✔ demo-docs web-1 12m · see…") for r in rows), rows)
            for ln in rows:
                self.assertLessEqual(len(ln), cw - 1)

    def test_down_none_and_no_sessions(self):
        p = self.build({"daemon": "down", "why": "refused"})
        self.assertEqual(p.level, "unknown")
        self.assertIn("daemon not answering (refused)", T.strip(p.lines[0]))
        self.assertIsNone(self.build({"daemon": "none"}))
        support.clear_cache()
        self.assertIsNone(BUILDERS["drover"](RCtx(host(HUB), registry.Cache(), {"vitals": {}}, [], 58)))
        p = self.build({"daemon": "up", "totals": {"hosts": 1, "idle": 2}})
        self.assertEqual(T.strip(p.lines[0]), "○ no agent sessions working or waiting")
        self.assertEqual((p.level, p.note), ("ok", "1 host · 2 idle"))

    def test_a_record_from_the_hub_names_the_hubs_machine_as_local(self):
        """With scope = "global" a peer renders the hub's record: Drover's "local" is the hub."""
        p = self.build(toml=PEER, last_seen=time.time() - 3 * 3600)
        text = "\n".join(T.strip(ln) for ln in p.lines)
        self.assertIn("demo-review atlas 7m", text)
        self.assertTrue(any(T.strip(ln).startswith("working  ▶ atlas 3") for ln in p.lines), text)
        self.assertEqual(self.run_src_here(), "atlas")

    def run_src_here(self):
        self.daemon()
        return self.run_src()["here"]

    def test_fleet_labels_shorten_drover_host_names(self):
        p = self.build(toml=HUB + '[fleet.labels]\n"gpu-box" = "gpu"\n', last_seen=time.time() - 3 * 3600)
        self.assertIn("demo-migrate gpu 1m", "\n".join(T.strip(ln) for ln in p.lines))

    def test_malformed_records_never_break_the_panel(self):
        base = demo()

        def paths(obj, path=()):
            items = obj.items() if isinstance(obj, dict) else enumerate(obj[:2]) if isinstance(obj, list) else ()
            for k, v in items:
                yield path + (k,)
                yield from paths(v, path + (k,))
        h, n = host(HUB), 0

        class Mem:
            def __init__(self, d):
                self.d = d

            def rec(self, name):
                return {"ok": True, "ts": time.time(), "data": self.d} if name == "drover" else None

            def data(self, name, default=None):
                return self.d if name == "drover" else default
        for path in list(paths(base)) + [()]:
            for junk in (5, "x", None, [1, "a", None, {"k": 5}], {"k": 5}, True, -1e300):
                d = json.loads(json.dumps(base))
                if path:
                    node = d
                    for part in path[:-1]:
                        node = node[part]
                    node[path[-1]] = junk
                else:
                    d = junk
                p = BUILDERS["drover"](RCtx(h, Mem(d), {"vitals": {}}, [], 58, time.time() - 3600))
                if p:
                    p.render(58, T.ACCENT)
                n += 1
        self.assertGreater(n, 500)

    def test_the_dashboard_puts_the_waiting_alert_in_the_attention_strip(self):
        put("drover", demo())
        out = T.strip(render.render(host(HUB), registry.Cache(), width=130))
        self.assertIn("2 agents waiting on you (demo-review@atlas, demo-migrate@gpu-box)", out)
        self.assertIn("DROVER", out)


class DemoData(unittest.TestCase):
    def test_the_demo_record_has_the_source_shape(self):
        d = demo()
        live = None
        state = tempfile.mkdtemp(prefix="drover-state-")
        self.addCleanup(shutil.rmtree, state, True)
        with open(os.path.join(state, "daemon.json"), "w") as f:
            json.dump({"url": "http://127.0.0.1:7337/"}, f)
        for name, tok in (("token", MAIN), ("widget-token", WIDGET)):
            with open(os.path.join(state, name), "w") as f:
                f.write(tok)
        fake = Daemon(items=[note("1", NOW - 60, "finished", "demo-docs finished", "done", sid="s-docs")])
        with mock.patch.object(registry.Ctx, "http", staticmethod(fake)):
            h = host(HUB + '[sources.drover]\nstate_dir = "%s"\n' % state)
            live = drover.drover(registry.Ctx(h, {}, None, registry.SOURCES["drover"].config(h)))
        self.assertEqual(set(d) - set(live), set())
        for key in ("waiting", "working", "hosts", "feed"):
            self.assertEqual({k for it in d[key] for k in it} - {k for it in live[key] for k in it}, set(), key)
        self.assertEqual(set(d["details"]) - set(live["details"]), set())
        text = json.dumps(d)
        self.assertNotRegex(text, r"/Users/|/home/")


if __name__ == "__main__":
    unittest.main()

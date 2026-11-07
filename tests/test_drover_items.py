"""Items and actions on the DROVER panel: jump, read the transcript, answer a waiting
session. The source side (porchlight/sources/drover.py: tmux_target(), the new `id`
and `tmux` fields) and the panel side (porchlight/panels/drover.py: argv building,
Items) plus the `drover.answer` write handler, all offline.

Fixtures and the `Daemon` fake mirror test_drover.py's; see that file for the native
ops this source reads. Nothing here is a shell string: every whitelist exists because
`ssh host cmd...` has the remote shell re-join `cmd...` into one command line, so a
target or host reaching the ssh+tmux branch must be as clean as text inside an actual
shell command, not merely "not a shell string on this end".
"""
import support  # noqa: F401 — first: scratch cache and config

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from porchlight import actions, core, registry, theme as T
from porchlight.panels import drover as pdrover
from porchlight.sources import drover
from porchlight.ui import Action, BUILDERS, RCtx
from support import host, put

NOW = time.time()
MAIN, WIDGET = "main-fixture-0f3c9a2b", "widget-fixture-77d1e4c0"


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


# ── tmux_target(): the source's own whitelist ────────────────────────────────
class TmuxTarget(unittest.TestCase):
    def test_well_formed_targets_pass(self):
        for t in ("demo:3", "demo:@12.%4", "=session:1", "a.b_c-d:2", "session:@1"):
            self.assertEqual(drover.tmux_target(t), t)

    def test_hostile_targets_are_dropped(self):
        for t in ("evil; rm -rf /", "a$(whoami)", "a b", "`id`", "'; DROP", '"x"', "a|b", "a&b", "a(b)"):
            self.assertEqual(drover.tmux_target(t), "")

    def test_missing_or_wrong_type_is_empty(self):
        self.assertEqual(drover.tmux_target(None), "")
        self.assertEqual(drover.tmux_target(5), "")
        self.assertEqual(drover.tmux_target(""), "")


# ── summary()/feed(): the new id and tmux fields, backward compatible ───────
class Fields(unittest.TestCase):
    def test_summary_adds_id_and_tmux_and_cleans_them(self):
        res = {"waiting": [{"id": "sess-0000000000001", "name": "a", "host": "atlas", "since": iso(NOW),
                            "tmux": "work:@1.%1"}],
               "working": [{"id": "sess-working", "name": "b", "host": "atlas", "tmux": "evil; rm -rf /"}],
               "hosts": [], "totals": {}, "manager": {}}
        out = drover.summary(res)
        self.assertEqual(out["waiting"][0]["id"], "sess-0000000")   # short_id: 12 chars
        self.assertEqual(out["waiting"][0]["tmux"], "work:@1.%1")
        self.assertEqual(out["working"][0]["tmux"], "")             # hostile: dropped, not passed through

    def test_missing_id_and_tmux_mean_no_jump_or_copy_target_not_a_crash(self):
        res = {"waiting": [{"name": "a", "host": "atlas", "since": iso(NOW)}],
               "working": [{"name": "b", "host": "atlas"}], "hosts": [], "totals": {}, "manager": {}}
        out = drover.summary(res)
        self.assertEqual(out["waiting"][0]["id"], "")
        self.assertEqual(out["waiting"][0]["tmux"], "")
        self.assertEqual(out["working"][0]["id"], "")


# ── Panel argv: local, remote, inside tmux, and every hostile case refused ──
class Argv(unittest.TestCase):
    def test_local_outside_tmux_attaches(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            self.assertEqual(pdrover._jump_argv("local", "work:@1.%1"), ["tmux", "attach", "-t", "work:@1.%1"])

    def test_local_inside_tmux_switches_client(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,1234,0"}):
            self.assertEqual(pdrover._jump_argv("local", "work:@1.%1"), ["tmux", "switch-client", "-t", "work:@1.%1"])

    def test_remote_jumps_over_ssh(self):
        self.assertEqual(pdrover._jump_argv("gpu-box", "work:@1.%1"),
                         ["ssh", "-t", "gpu-box", "tmux", "attach", "-t", "work:@1.%1"])

    def test_no_target_means_no_jump_action(self):
        self.assertIsNone(pdrover._jump_argv("local", ""))
        self.assertIsNone(pdrover._jump_argv("gpu-box", ""))

    def test_a_hostile_target_produces_no_jump_action_local_or_remote(self):
        for target in ("evil; rm -rf /", "a$(whoami)", "a b", "`id`", "a|b", "'; DROP TABLE x", '"q"'):
            self.assertIsNone(pdrover._jump_argv("local", target), target)
            self.assertIsNone(pdrover._jump_argv("gpu-box", target), target)

    def test_a_hostile_host_produces_no_jump_or_transcript_host_flag(self):
        for bad_host in ("gpu-box; rm -rf /", "gpu box", "-oProxyCommand=evil", "host$(x)", "host`x`"):
            self.assertIsNone(pdrover._jump_argv(bad_host, "work:@1.%1"), bad_host)
            self.assertNotIn("--host", pdrover._transcript_argv("sid1", bad_host) or [])

    def test_transcript_argv_local_and_remote(self):
        self.assertEqual(pdrover._transcript_argv("sid1", "local"), ["drover", "transcript", "sid1", "--brief"])
        self.assertEqual(pdrover._transcript_argv("sid1", ""), ["drover", "transcript", "sid1", "--brief"])
        self.assertEqual(pdrover._transcript_argv("sid1", "gpu-box"),
                         ["drover", "transcript", "sid1", "--brief", "--host", "gpu-box"])

    def test_no_session_id_means_no_transcript_action(self):
        self.assertIsNone(pdrover._transcript_argv("", "local"))

    def test_a_remote_target_is_quoted_for_the_remote_shell(self):
        # ssh hands the remote argv to a shell as one string: a "$3" session id must arrive
        # as "$3", not as an empty positional parameter.
        self.assertEqual(pdrover._jump_argv("gpu-box", "$3"), ["ssh", "-t", "gpu-box", "tmux", "attach", "-t", "'$3'"])
        self.assertEqual(pdrover._jump_argv("gpu-box", "demo:@4.%1"),
                         ["ssh", "-t", "gpu-box", "tmux", "attach", "-t", "demo:@4.%1"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            self.assertEqual(pdrover._jump_argv("local", "$3")[-1], "$3")  # no shell on the local branch


class Items(unittest.TestCase):
    def setUp(self):
        # Deterministic jump argv whatever tmux session (if any) happens to run these tests.
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("TMUX", None)

    def build(self, data, cw=90, last_seen=None, toml='[general]\nlabel = "atlas"\n'):
        support.clear_cache()
        put("drover", data)
        return BUILDERS["drover"](RCtx(host(toml), registry.Cache(), {"vitals": {}}, [], cw, last_seen))

    def record(self, **over):
        base = {"daemon": "up", "here": "atlas",
                "waiting": [{"id": "wr-review001", "name": "demo-review", "host": "local", "engine": "claude",
                             "kind": "question", "since": NOW - 420, "ask": "Push now?", "tmux": "demo:@1.%1"}],
                "working": [{"id": "wk-api00001", "name": "demo-api", "host": "gpu-box", "engine": "claude",
                             "tmux": "demo:@4.%1"}],
                "hosts": [], "totals": {"waiting": 1, "working": 1, "idle": 0, "hosts": 1}, "manager": {"up": True},
                "feed": []}
        base.update(over)
        return base

    def test_waiting_item_carries_jump_transcript_answer_and_copy(self):
        p = self.build(self.record())
        it = next(i for i in p.items if i.id == "drover:local:wr-review001")
        names = [a.name for a in it.actions]
        self.assertEqual(names, ["run", "run", "drover.answer", "copy"])
        self.assertEqual(it.actions[0].args["argv"][:2], ["tmux", "attach"])
        self.assertEqual(it.actions[1].args["argv"], ["drover", "transcript", "wr-review001", "--brief"])
        self.assertEqual(it.actions[2].args["id"], "wr-review001")
        self.assertTrue(it.actions[2].writes)
        self.assertEqual(it.actions[3].args["text"], "wr-review001")
        self.assertIn("Push now?", "\n".join(it.detail))

    def test_working_item_has_no_answer_action(self):
        p = self.build(self.record())
        it = next(i for i in p.items if i.id == "drover:gpu-box:wk-api00001")
        self.assertNotIn("drover.answer", [a.name for a in it.actions])
        self.assertEqual(it.actions[0].args["argv"][0], "ssh")  # gpu-box is remote

    def test_an_old_record_without_id_or_tmux_has_no_actions_but_still_renders(self):
        rec = self.record()
        del rec["waiting"][0]["id"]
        del rec["waiting"][0]["tmux"]
        p = self.build(rec)
        it = next(i for i in p.items if i.id == "drover:local:-")
        self.assertEqual(it.actions, [])

    def test_item_ids_are_stable_and_unique(self):
        p = self.build(self.record(feed=[{"at": NOW - 60, "kind": "finished", "host": "local", "name": "demo-build",
                                          "what": "finished", "text": "done", "sid": "s-build", "tmux": ""}]))
        ids = [i.id for i in p.items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("drover:local:s-build", ids)

    def test_login_render_text_rows_are_unchanged_by_the_new_fields(self):
        """Items are additive: the same record, with and without id/tmux, renders the
        exact same text rows."""
        with_fields = self.record()
        without_fields = json.loads(json.dumps(with_fields))
        for w in without_fields["waiting"] + without_fields["working"]:
            w.pop("id", None)
            w.pop("tmux", None)
        p1 = self.build(with_fields, last_seen=NOW - 3600)
        rows1 = [T.strip(ln) for ln in p1.lines]
        p2 = self.build(without_fields, last_seen=NOW - 3600)
        rows2 = [T.strip(ln) for ln in p2.lines]
        self.assertEqual(rows1, rows2)
        self.assertGreater(len(p1.items), 0)


# ── The answer handler: policy, the exact op, the main token only ──────────
class Answer(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.state = tempfile.mkdtemp(prefix="drover-answer-state-")
        self.addCleanup(shutil.rmtree, self.state, True)
        with open(os.path.join(self.state, "daemon.json"), "w") as f:
            json.dump({"url": "http://127.0.0.1:7337/"}, f)
        for name, tok in (("token", MAIN), ("widget-token", WIDGET)):
            with open(os.path.join(self.state, name), "w") as f:
                f.write(tok + "\n")

    def toml(self, allow_writes=True):
        return ('[sources.drover]\nstate_dir = "%s"\n' % self.state) + \
               ("[interactive]\nallow_writes = true\n" if allow_writes else "")

    def daemon(self, **kw):
        d = DaemonFake(**kw)
        p = mock.patch.object(registry.Ctx, "http", staticmethod(d))
        p.start()
        self.addCleanup(p.stop)
        return d

    def act(self, **args):
        return Action("drover.answer", "answer", "m", dict({"id": "sess-12345", "text": "go ahead"}, **args),
                      writes=True, confirm="Send this reply to demo-review?")

    def test_refused_without_allow_writes(self):
        self.daemon()
        h = host(self.toml(allow_writes=False))
        res = actions.run(h, self.act(), confirmed=True)
        self.assertFalse(res.ok)
        self.assertIn("allow_writes", res.msg)

    def test_refused_without_confirmation_even_with_allow_writes(self):
        self.daemon()
        h = host(self.toml())
        res = actions.run(h, self.act(), confirmed=False)
        self.assertFalse(res.ok)

    def test_confirmed_sends_exactly_sessions_message_with_the_main_token_only(self):
        d = self.daemon()
        h = host(self.toml())
        res = actions.run(h, self.act(), confirmed=True)
        self.assertTrue(res.ok, res.msg)
        self.assertEqual(d.ops(), ["sessions.message"])
        req = d.reqs[0]
        self.assertEqual(req["body"], {"id": "sess-12345", "text": "go ahead"})
        self.assertEqual(req["token"], MAIN)
        self.assertEqual(res.refresh, ("drover",))

    def test_a_remote_host_is_routed_through_the_op_not_local_only(self):
        d = self.daemon()
        h = host(self.toml())
        res = actions.run(h, self.act(host="gpu-box"), confirmed=True)
        self.assertTrue(res.ok, res.msg)
        self.assertEqual(d.reqs[0]["body"], {"id": "sess-12345", "text": "go ahead", "host": "gpu-box"})

    def test_a_local_host_sends_no_host_param(self):
        d = self.daemon()
        h = host(self.toml())
        actions.run(h, self.act(host="local"), confirmed=True)
        self.assertNotIn("host", d.reqs[0]["body"])

    def test_no_id_or_text_is_refused_before_any_network_call(self):
        d = self.daemon()
        h = host(self.toml())
        self.assertFalse(actions.run(h, self.act(id=""), confirmed=True).ok)
        self.assertFalse(actions.run(h, self.act(text="  "), confirmed=True).ok)
        self.assertEqual(d.reqs, [])

    def test_a_daemon_error_is_a_short_message_never_raised(self):
        self.daemon(fail=True)
        h = host(self.toml())
        res = actions.run(h, self.act(), confirmed=True)
        self.assertFalse(res.ok)
        self.assertTrue(res.msg)

    def test_the_result_message_masks_token_shapes(self):
        """A daemon error that happens to echo something token-shaped never reaches the
        screen whole: actions.run() masks every Result.msg before it is shown."""
        canary = "sk-ant-" + "a1b2c3d4e5" * 3  # a token shape, built at runtime: never scanner bait
        self.daemon(deny="rejected near " + canary)
        h = host(self.toml())
        res = actions.run(h, self.act(), confirmed=True)
        self.assertFalse(res.ok)
        self.assertNotIn(canary, res.msg)
        self.assertIn("sk-ant-", res.msg)  # the shape's prefix survives; the secret body does not


class DaemonFake:
    """A fake Drover daemon for sessions.message: records every request, the main
    token only (per the WIDGET_PATHS scope, sessions.message is not on it)."""

    def __init__(self, fail=False, deny=""):
        self.fail, self.deny, self.reqs = fail, deny, []

    def __call__(self, url, method="GET", headers=None, json_body=None, timeout=6.0, **kw):
        op = url.rsplit("/", 1)[-1]
        auth = (headers or {}).get("Authorization", "")
        tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        self.reqs.append({"op": op, "token": tok, "body": json_body})
        if tok != MAIN:
            return core.Resp(403, {"ok": False, "error": "widget token: read-only scope"}, 1)
        if self.fail:
            return core.Resp(502, {"ok": False, "error": "bad gateway"}, 1)
        if self.deny:
            return core.Resp(200, {"ok": False, "error": self.deny}, 1)
        if op != "sessions.message":
            return core.Resp(404, {"ok": False, "error": "unknown op %s" % op}, 1)
        return core.Resp(200, {"ok": True, "result": {"delivered": True}}, 3)

    def ops(self):
        return [r["op"] for r in self.reqs]


if __name__ == "__main__":
    unittest.main()

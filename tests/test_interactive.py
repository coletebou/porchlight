"""`porchlight -i` (porchlight/interactive.py), offline.

No real terminal: `App` is built with `term=None` and a fake opener/runner, and driven
by calling `reload()`, `handle(key)` and `frame()` directly. Live probes are faked
(`support.calm_probes()`); Drover ops go through a fake HTTP, never the network.
"""
import support  # noqa: F401 — first: scratch cache and config

import io
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from porchlight import collect, core, interactive, registry, render, theme as T
from porchlight import todo_cmd
from porchlight.sources import todos as ST
from porchlight.ui import Panel
from support import FakeHTTP, calm_probes, clear_cache, host, put, resp

GHTOK = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5" + "xx"  # 36 chars after the prefix
TODAY = time.strftime("%Y-%m-%d")


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        clear_cache()
        self.addCleanup(clear_cache)


# ── parse_key ────────────────────────────────────────────────────────────────
class ParseKey(unittest.TestCase):
    def test_every_named_key(self):
        for b, name in interactive.KEYS.items():
            self.assertEqual(interactive.parse_key(b), name, b)

    def test_arrows_enter_esc_tab_btab_backspace_ctrlc(self):
        self.assertEqual(interactive.parse_key(b"\x1b[A"), "up")
        self.assertEqual(interactive.parse_key(b"\x1bOB"), "down")
        self.assertEqual(interactive.parse_key(b"\x1b[C"), "right")
        self.assertEqual(interactive.parse_key(b"\x1b[D"), "left")
        self.assertEqual(interactive.parse_key(b"\r"), "enter")
        self.assertEqual(interactive.parse_key(b"\n"), "enter")
        self.assertEqual(interactive.parse_key(b"\x1b"), "esc")
        self.assertEqual(interactive.parse_key(b"\t"), "tab")
        self.assertEqual(interactive.parse_key(b"\x1b[Z"), "btab")
        self.assertEqual(interactive.parse_key(b"\x7f"), "backspace")
        self.assertEqual(interactive.parse_key(b"\x08"), "backspace")
        self.assertEqual(interactive.parse_key(b"\x03"), "ctrl-c")

    def test_plain_ascii_passes_through(self):
        self.assertEqual(interactive.parse_key(b"q"), "q")
        self.assertEqual(interactive.parse_key(b"/"), "/")

    def test_utf8_multibyte_chars(self):
        self.assertEqual(interactive.parse_key("é".encode()), "é")
        self.assertEqual(interactive.parse_key("🎉".encode()), "🎉")

    def test_unknown_or_incomplete_bytes_decode_to_empty(self):
        self.assertEqual(interactive.parse_key(b"\xff"), "")
        self.assertEqual(interactive.parse_key(b"\xc3"), "")  # half of a 2-byte utf-8 char


# ── load() ───────────────────────────────────────────────────────────────────
class Load(Base):
    def test_alerts_first_then_panel_keys_order_with_cached_data(self):
        h = host()
        todo = ST.item("files", "a1", "Buy milk")
        put("todos", {"items": [todo], "providers": {"files": {"ok": True, "n": 1}}, "total": 1, "overdue": 0})
        put("drover", {"daemon": "up", "here": "testbox",
                       "waiting": [{"name": "worker1", "host": "local", "id": "sess1", "ask": "approve?",
                                    "engine": "claude", "kind": "question", "since": time.time() - 60}],
                       "working": [], "feed": [], "totals": {"waiting": 1, "working": 0, "idle": 0, "hosts": 1},
                       "details": {}})
        with calm_probes():
            panels, al, cache = interactive.load(h, 100)
        self.assertEqual(panels[0].key, "alerts")
        keys = [p.key for p in panels[1:]]
        self.assertIn("todos", keys)
        self.assertIn("drover", keys)
        # the built-in panels follow render.panel_keys (AUTO_PANELS without a fleet view); a
        # panel from a [[custom]] source elsewhere in the suite, if any, only ever rides after
        # them (load() appends builders panel_keys does not already name), never between.
        expected_order = render.panel_keys(h, registry.Cache())
        builtin = [k for k in keys if k in expected_order]
        self.assertEqual(builtin, [k for k in expected_order if k in keys])
        self.assertLess(keys.index("todos"), keys.index("drover"))
        todos_panel = panels[1 + keys.index("todos")]
        self.assertEqual(T.strip(todos_panel.items[0]["text"]), "Buy milk")
        drover_panel = panels[1 + keys.index("drover")]
        self.assertTrue(any("worker1" in T.strip(it["text"]) for it in drover_panel.items))

    def test_no_cached_data_still_has_the_alerts_panel(self):
        h = host()
        with calm_probes():
            panels, al, _cache = interactive.load(h, 100)
        self.assertEqual(panels[0].key, "alerts")
        self.assertEqual(al, [])


# ── navigation ───────────────────────────────────────────────────────────────
def _app(h=None):
    return interactive.App(h or host(), term=None)


def _three_panels():
    return [
        Panel("alerts", "ALERTS", ["ok no alerts"], "ok", "", []),
        Panel("todos", "TODOS", ["3 open"], "warn", "", [{"text": "a"}, {"text": "b"}, {"text": "c"}]),
        Panel("drover", "DROVER", ["1 waiting"], "warn", "", [{"text": "d1"}]),
    ]


class Navigation(unittest.TestCase):
    def test_move_in_panels_clamps(self):
        app = _app()
        app.panels = _three_panels()
        app.focus, app.pi = "panels", 0
        app.ii, app.scroll, app.filter = 5, 3, "x"
        app.move(1)
        self.assertEqual((app.pi, app.ii, app.scroll, app.filter), (1, 0, 0, ""))
        app.move(5)
        self.assertEqual(app.pi, 2)  # clamped at the last panel
        app.move(-10)
        self.assertEqual(app.pi, 0)  # clamped at the first

    def test_move_in_items_clamps(self):
        app = _app()
        app.panels = _three_panels()
        app.pi, app.focus = 1, "items"
        app.ii = 0
        app.move(10)
        self.assertEqual(app.ii, 2)  # 3 items: 0,1,2
        app.move(-10)
        self.assertEqual(app.ii, 0)

    def test_move_elsewhere_scrolls_without_an_upper_clamp(self):
        app = _app()
        app.focus, app.scroll = "detail", 2
        app.move(-10)
        self.assertEqual(app.scroll, 0)
        app.move(7)
        self.assertEqual(app.scroll, 7)

    def test_forward_into_items_then_detail(self):
        app = _app()
        app.panels = _three_panels()
        app.pi, app.focus, app.ii = 1, "panels", 0
        app.forward()
        self.assertEqual((app.focus, app.ii), ("items", 0))
        app.forward()
        self.assertEqual(app.focus, "detail")

    def test_forward_on_a_panel_without_items_resets_scroll_only(self):
        app = _app()
        app.panels = _three_panels()
        app.pi, app.focus, app.scroll = 0, "panels", 9  # "alerts" has no items here
        app.forward()
        self.assertEqual((app.focus, app.scroll), ("panels", 0))

    def test_back_from_every_focus(self):
        app = _app()
        app.panels = _three_panels()
        app.focus, app.scroll = "detail", 9
        app.back()
        self.assertEqual((app.focus, app.scroll), ("items", 0))
        for start in ("items", "help", "search"):
            app.focus, app.scroll = start, 9
            app.back()
            self.assertEqual((app.focus, app.scroll), ("panels", 0))
        app.focus, app.filter = "panels", "keep"
        app.back()
        self.assertEqual(app.filter, "")

    def test_tab_and_btab_wrap_and_reset(self):
        app = _app()
        app.panels = _three_panels()
        app.pi, app.ii, app.scroll, app.filter = 2, 2, 5, "x"
        app.handle("tab")
        self.assertEqual((app.pi, app.ii, app.scroll, app.filter, app.focus), (0, 0, 0, "", "panels"))
        app.handle("btab")
        self.assertEqual(app.pi, 2)  # wraps backward from 0

    def test_help_toggle_always_returns_to_panels(self):
        app = _app()
        app.focus = "items"
        app.handle("?")
        self.assertEqual(app.focus, "help")
        app.handle("?")
        self.assertEqual(app.focus, "panels")  # not back to "items"

    def test_quit_keys(self):
        for k in ("q", "ctrl-c", "ctrl-d"):
            app = _app()
            app.handle(k)
            self.assertTrue(app.quit, k)


# ── frame() ──────────────────────────────────────────────────────────────────
def _frame_fixture_panels():
    return [
        Panel("alerts", "ALERTS", ["ok no alerts"], "ok", "", []),
        Panel("todos", "TODOS", ["3 open"], "warn", "", [
            {"text": "Buy milk", "detail": ["Buy milk"], "actions": ["done"], "url": ""},
            {"text": "x" * 300, "detail": ["long"], "actions": ["done"], "url": "http://x"},
            {"text": "🎉" * 20 + " party", "detail": ["emoji"], "actions": []},
        ]),
        Panel("huge", "X" * 1000, ["one line"], "ok", "", []),
    ]


class Frame(unittest.TestCase):
    def _check(self, app, w, h):
        with mock.patch.object(interactive.App, "size", lambda self: (w, h)):
            out = app.frame()
        stripped = T.strip(out)
        lines = stripped.split("\n")
        self.assertEqual(len(lines), h, (w, h))
        for line in lines:
            self.assertLessEqual(T.width(line), w, (w, h, line))

    def test_no_panels_never_raises(self):
        app = _app()
        app.al, app.cache, app.panels = [], None, []
        self._check(app, 80, 10)

    def test_empty_items_long_titles_and_emoji_at_every_size(self):
        app = _app()
        app.al, app.cache = [], None
        app.panels = _frame_fixture_panels()
        app.pi, app.focus, app.ii = 1, "items", 0
        for w in (40, 80, 160):
            for h in (10, 30):
                self._check(app, w, h)

    def test_detail_help_and_search_focus_never_raise(self):
        app = _app()
        app.al, app.cache = [], None
        app.panels = _frame_fixture_panels()
        app.pi, app.ii = 1, 0
        for focus in ("detail", "help", "search"):
            app.focus = focus
            app.search_rows = ["a search row"]
            self._check(app, 100, 24)


# ── filter ───────────────────────────────────────────────────────────────────
class Filter(unittest.TestCase):
    def _app_with_items(self):
        app = _app()
        app.panels = [Panel("todos", "TODOS", ["x"], "ok", "", [
            {"text": T.c("Alpha Project", "green")},
            {"text": "beta task"},
            {"text": "gamma ALPHA thing"},
        ])]
        app.pi, app.focus = 0, "panels"
        return app

    def test_typing_backspace_enter_apply_case_insensitively(self):
        app = self._app_with_items()
        app.handle("/")
        self.assertEqual(app.typing, ("filter", ""))
        for ch in "alphax":
            app.handle(ch)
        app.handle("backspace")
        self.assertEqual(app.typing, ("filter", "alpha"))
        app.handle("enter")
        self.assertIsNone(app.typing)
        self.assertEqual(app.filter, "alpha")
        texts = [T.strip(i["text"]) for i in app.items()]
        self.assertEqual(texts, ["Alpha Project", "gamma ALPHA thing"])

    def test_esc_and_ctrlc_cancel_without_changing_the_filter(self):
        app = self._app_with_items()
        app.filter = "beta"
        app.handle("/")
        app.handle("z")
        app.handle("esc")
        self.assertIsNone(app.typing)
        self.assertEqual(app.filter, "beta")
        app.handle("/")
        app.handle("z")
        app.handle("ctrl-c")
        self.assertIsNone(app.typing)
        self.assertEqual(app.filter, "beta")

    def test_non_printable_keys_are_ignored_while_typing(self):
        app = self._app_with_items()
        app.typing = ("filter", "ab")
        app.handle("up")
        self.assertEqual(app.typing, ("filter", "ab"))


# ── d: mark a todo done ───────────────────────────────────────────────────────
class TodoDoneLocal(Base):
    def _setup_file_todo(self, dir_, actions):
        path = os.path.join(dir_, "todo.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Buy milk\n")
        it = ST.p_files(None, {"files": [path]})[0]
        put("todos", {"items": [it], "providers": {"files": {"ok": True, "n": 1}}, "total": 1, "overdue": 0})
        extra = "actions = true\n" if actions else ""
        h = host("[sources.todos]\n%sfiles = [%s]\n" % (extra, repr(path)))
        app = interactive.App(h, term=None)
        with calm_probes():
            app.reload()
        ti = next(i for i, p in enumerate(app.panels) if p.key == "todos")
        app.pi, app.focus, app.ii = ti, "items", 0
        return path, app

    def test_refused_unless_actions_is_true(self):
        with tempfile.TemporaryDirectory() as d:
            path, app = self._setup_file_todo(d, actions=False)
            app.handle("d")
            self.assertIsNotNone(app.confirm)
            app.handle("y")
            self.assertIn("actions are off", T.strip(app.msg))
            self.assertEqual(slurp(path), "Buy milk\n")
            self.assertEqual(app.done, set())

    def test_n_leaves_it_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path, app = self._setup_file_todo(d, actions=True)
            app.handle("d")
            self.assertIsNotNone(app.confirm)
            app.handle("n")
            self.assertEqual(T.strip(app.msg), "unchanged")
            self.assertEqual(slurp(path), "Buy milk\n")
            self.assertEqual(app.done, set())

    def test_yes_marks_done_hides_it_and_spawns_a_collector(self):
        with tempfile.TemporaryDirectory() as d:
            path, app = self._setup_file_todo(d, actions=True)
            before = app.item()
            with mock.patch.object(collect, "spawn_background", return_value=True) as spawn:
                app.handle("d")
                self.assertIn("Buy milk", T.strip(app.confirm[0]))
                app.handle("y")
            self.assertIn("marked done in", T.strip(app.msg))
            self.assertEqual(slurp(path), "x %s Buy milk\n" % TODAY)
            self.assertIn(before["key"], app.done)
            self.assertEqual(app.items(), [])
            spawn.assert_called_once_with(app.host, only=["todos"])

    def test_nothing_to_mark_done_without_the_done_action(self):
        app = _app()
        app.panels = [Panel("drover", "DROVER", ["x"], "ok", "", [{"text": "t", "actions": ["attach"]}])]
        app.pi, app.focus, app.ii = 0, "items", 0
        app.handle("d")
        self.assertIsNone(app.confirm)
        self.assertIn("nothing to mark done here", T.strip(app.msg))


class TodoDoneViaHub(Base):
    def _bundle_with_item(self, h, item, extra_sources=None):
        now = time.time()
        core.ensure_dirs()
        sources = {"todos": {"data": {"items": [item], "providers": {"files": {"ok": True, "n": 1}}, "total": 1,
                                       "overdue": 0}, "ok": True, "ts": now}}
        if extra_sources:
            sources.update(extra_sources)
        core.write_json_atomic(core.BUNDLE_PATH, {"from": "hubbox", "label": "hub", "ts": now, "identity": h.identity,
                                                   "sources": sources, "fleet": {"hosts": {}}})

    def _app_on_item(self, item):
        h = host()
        self._bundle_with_item(h, item)
        app = interactive.App(h, term=None)
        with calm_probes():
            app.reload()
        ti = next(i for i, p in enumerate(app.panels) if p.key == "todos")
        app.pi, app.focus, app.ii = ti, "items", 0
        return app

    def test_confirm_names_the_hub_and_quotes_the_key_on_success(self):
        item = ST.item("files", "o'brien 1", "Weird key")
        app = self._app_on_item(item)
        app.handle("d")
        self.assertIn("via hubbox", T.strip(app.confirm[0]))
        raw_key = app.item()["raw"]["key"]
        with mock.patch.object(core, "run", return_value=(0, "", "")) as run, \
                mock.patch.object(collect, "spawn_background", return_value=True) as spawn:
            app.handle("y")
        self.assertEqual(run.call_args[0][0], todo_cmd.hub_argv("hubbox", raw_key))
        self.assertIn(shlex.quote(raw_key), run.call_args[0][0][-1])
        self.assertEqual(run.call_args[1].get("timeout"), 40)
        self.assertIn("done on hubbox", T.strip(app.msg))
        spawn.assert_called_once_with(app.host, only=["todos"])

    def test_nonzero_rc_shows_the_last_stderr_line_masked(self):
        item = ST.item("files", "k2", "Another key")
        app = self._app_on_item(item)
        app.handle("d")
        err = "line one\nerror: token %s invalid\n" % GHTOK
        with mock.patch.object(core, "run", return_value=(1, "stdout", err)), \
                mock.patch.object(collect, "spawn_background", return_value=True):
            app.handle("y")
        msg = T.strip(app.msg)
        self.assertNotIn(GHTOK, msg)
        self.assertIn("hubbox", msg)
        self.assertIn("invalid", msg)


# ── o: open ──────────────────────────────────────────────────────────────────
class ActOpen(unittest.TestCase):
    def test_with_a_url_calls_the_opener(self):
        calls = []
        app = interactive.App(host(), term=None, opener=lambda u: calls.append(u) or ("opened " + u))
        app.panels = [Panel("todos", "TODOS", ["x"], "ok", "", [{"text": "t", "url": "http://example.com"}])]
        app.pi, app.focus, app.ii = 0, "items", 0
        app.handle("o")
        self.assertEqual(calls, ["http://example.com"])
        self.assertEqual(T.strip(app.msg), "opened http://example.com")

    def test_without_a_url_says_so(self):
        app = _app()
        app.panels = [Panel("todos", "TODOS", ["x"], "ok", "", [{"text": "t"}])]
        app.pi, app.focus, app.ii = 0, "items", 0
        app.handle("o")
        self.assertEqual(T.strip(app.msg), "no link on this item")


class OpenUrl(unittest.TestCase):
    def test_over_ssh_without_display_returns_the_link(self):
        env = dict(os.environ)
        env["SSH_CONNECTION"] = "1.2.3.4 1 5.6.7.8 22"
        env.pop("DISPLAY", None)
        with mock.patch.dict(os.environ, env, clear=True):
            got = interactive.open_url("http://example.com/x")
        self.assertEqual(T.strip(got), "link: http://example.com/x")


# ── a: attach ────────────────────────────────────────────────────────────────
class AttachArgv(unittest.TestCase):
    def _state(self, dir_, url="http://127.0.0.1:7337/", token="maintok"):
        with open(os.path.join(dir_, "daemon.json"), "w") as f:
            import json
            json.dump({"url": url, "pid": 1}, f)
        with open(os.path.join(dir_, "token"), "w") as f:
            f.write(token + "\n")

    def _host(self, dir_):
        return host("[sources.drover]\nstate_dir = %s\n" % repr(dir_))

    def test_inside_tmux_switches_the_client(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d)
            h = self._host(d)
            fake = FakeHTTP({"op/sessions.list": resp(200, {"ok": True, "result": [{"id": "sess1", "tmux": "mysess:1.0"}]})})
            with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,1,0"}):
                argv, why = interactive.attach_argv(h, {"id": "sess1", "host": "local"}, http=fake)
            self.assertIsNone(why)
            self.assertEqual(argv, ["tmux", "switch-client", "-t", "mysess:1"])

    def test_outside_tmux_attaches(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d)
            h = self._host(d)
            fake = FakeHTTP({"op/sessions.list": resp(200, {"ok": True, "result": [{"id": "sess1", "tmux": "mysess:1.0"}]})})
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TMUX", None)
                argv, why = interactive.attach_argv(h, {"id": "sess1", "host": "local"}, http=fake)
            self.assertIsNone(why)
            self.assertEqual(argv, ["tmux", "attach", "-t", "mysess:1"])

    def test_remote_host_uses_ssh(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d)
            h = self._host(d)
            fake = FakeHTTP({"op/sessions.list": resp(200, {"ok": True, "result": [{"id": "sess1", "tmux": "mysess:1.0"}]})})
            argv, why = interactive.attach_argv(h, {"id": "sess1", "host": "remotebox"}, http=fake)
            self.assertIsNone(why)
            # one remote command string, the target shell-quoted, "--" before the host
            self.assertEqual(argv, ["ssh", "-t", "--", "remotebox", "tmux attach -t mysess:1"])

    def test_remote_target_is_quoted_and_odd_hosts_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d)
            h = self._host(d)
            fake = FakeHTTP({"op/sessions.list": resp(200, {"ok": True, "result": [{"id": "s", "tmux": "my proj;rm -rf x:1.0"}]})})
            argv, why = interactive.attach_argv(h, {"id": "s", "host": "remotebox"}, http=fake)
            self.assertEqual(argv[-1], "tmux attach -t 'my proj;rm -rf x:1'")
            for bad in ("-oProxyCommand=touch /tmp/x", "box name", "box;id"):
                argv, why = interactive.attach_argv(h, {"id": "s", "host": bad}, http=fake)
                self.assertIsNone(argv, bad)
                self.assertIn("odd host name", why)

    def test_no_tmux_pane_gives_a_reason(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d)
            h = self._host(d)
            fake = FakeHTTP({"op/sessions.list": resp(200, {"ok": True, "result": [{"id": "sess1", "tmux": ""}]})})
            argv, why = interactive.attach_argv(h, {"id": "sess1", "host": "local"}, http=fake)
            self.assertIsNone(argv)
            self.assertIn("no tmux pane", why)

    def test_no_daemon_is_a_reason_string(self):
        with tempfile.TemporaryDirectory() as d:
            h = self._host(d)  # no daemon.json written
            argv, why = interactive.attach_argv(h, {"id": "sess1", "host": "local"})
            self.assertIsNone(argv)
            self.assertIn("no Drover daemon", why)


class AttachGlue(unittest.TestCase):
    def test_not_a_drover_session_without_an_id(self):
        app = _app()
        app.panels = [Panel("drover", "DROVER", ["x"], "ok", "", [{"text": "t", "raw": {}}])]
        app.pi, app.focus, app.ii = 0, "items", 0
        app.handle("a")
        self.assertEqual(T.strip(app.msg), "not a Drover session")

    def test_a_reason_shows_when_attach_argv_fails(self):
        app = _app()
        app.panels = [Panel("drover", "DROVER", ["x"], "ok", "", [{"text": "t", "raw": {"id": "s1"}}])]
        app.pi, app.focus, app.ii = 0, "items", 0
        with mock.patch.object(interactive, "attach_argv", return_value=(None, "headless, no tmux")):
            app.handle("a")
        self.assertEqual(T.strip(app.msg), "headless, no tmux")

    def test_an_argv_runs_through_external(self):
        runner = mock.Mock(return_value=0)
        term = mock.Mock()
        app = interactive.App(host(), term=term, runner=runner)
        app.panels = [Panel("drover", "DROVER", ["x"], "ok", "", [{"text": "t", "raw": {"id": "s1"}}])]
        app.pi, app.focus, app.ii = 0, "items", 0
        with mock.patch.object(interactive, "attach_argv", return_value=(["tmux", "attach", "-t", "s"], None)), \
                mock.patch.object(core, "which", lambda name, extra=(): "/usr/bin/" + name), calm_probes():
            app.handle("a")
        runner.assert_called_once_with(["tmux", "attach", "-t", "s"])
        term.restore.assert_called_once()
        term.resume.assert_called_once()


# ── programs() and external() ────────────────────────────────────────────────
class Programs(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(interactive.programs(host()), dict(interactive.PROGRAMS))

    def test_adds_overrides_and_removes(self):
        h = host('[interactive.programs]\nX = ["htop"]\nA = []\n')
        got = interactive.programs(h)
        self.assertEqual(got["D"], interactive.PROGRAMS["D"])
        self.assertEqual(got["X"], ["htop"])
        self.assertNotIn("A", got)

    def test_reserved_and_multichar_keys_are_ignored(self):
        h = host('[interactive.programs]\nd = ["rm"]\nq = ["y"]\nab = ["z"]\n"/" = ["w"]\n')
        got = interactive.programs(h)
        self.assertEqual(got, dict(interactive.PROGRAMS))

    def test_string_values_are_split(self):
        h = host('[interactive.programs]\nY = "top -n 1"\n')
        self.assertEqual(interactive.programs(h)["Y"], ["top", "-n", "1"])


class External(unittest.TestCase):
    def test_missing_binary(self):
        app = _app()
        with mock.patch.object(core, "which", return_value=None):
            app.external(["nope"], "nope")
        self.assertIn("not installed here", T.strip(app.msg))

    def test_present_binary_restores_runs_resumes_and_reloads(self):
        runner = mock.Mock(return_value=0)
        term = mock.Mock()
        app = interactive.App(host(), term=term, runner=runner)
        with mock.patch.object(core, "which", return_value="/usr/bin/prog"), calm_probes():
            app.external(["prog", "x"], "prog")
        term.restore.assert_called_once()
        runner.assert_called_once_with(["prog", "x"])
        term.resume.assert_called_once()
        self.assertGreater(app.loaded, 0)  # reload() ran

    def test_nonzero_exit_is_reported(self):
        term = mock.Mock()
        app = interactive.App(host(), term=term, runner=lambda argv: 3)
        with mock.patch.object(core, "which", return_value="/usr/bin/prog"), calm_probes():
            app.external(["prog"], "prog")
        self.assertIn("exited 3", T.strip(app.msg))

    def test_oserror_from_the_runner_restores_the_terminal(self):
        term = mock.Mock()

        def boom(argv):
            raise OSError("no exec")
        app = interactive.App(host(), term=term, runner=boom)
        with mock.patch.object(core, "which", return_value="/usr/bin/prog"), calm_probes():
            app.external(["prog"], "prog")
        term.restore.assert_called_once()
        term.resume.assert_called_once()
        self.assertIn("no exec", T.strip(app.msg))


# ── search() ─────────────────────────────────────────────────────────────────
class Search(unittest.TestCase):
    def _state(self, dir_):
        import json
        with open(os.path.join(dir_, "daemon.json"), "w") as f:
            json.dump({"url": "http://127.0.0.1:7337/", "pid": 1}, f)
        with open(os.path.join(dir_, "token"), "w") as f:
            f.write("maintok\n")

    def test_hits_are_titled_masked_and_filtered_by_exclude_settings(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d)
            h = host("[sources.drover]\nstate_dir = %s\n[sources.todos]\nexclude_repos = [\"excluded/repo\"]\n"
                      "exclude_paths = [\"/identities/work/\"]\n" % repr(d))
            fake = FakeHTTP({
                "op/brain.search": resp(200, {"ok": True, "result": {"hits": [
                    {"title": "Rotate " + GHTOK, "repo": "keepme/repo", "snippet": "use " + GHTOK},
                    {"title": "Drop this one", "repo": "excluded/repo", "snippet": "irrelevant"},
                ]}}),
                "op/reflections.search": resp(200, {"ok": True, "result": {"items": [
                    {"title": "keep reflection", "path": "/home/x/other.md"},
                    {"title": "work reflection", "path": "/home/x/identities/work/secret.md"},
                ]}}),
            })
            rows = interactive.search(h, "query", http=fake)
        text = "\n".join(T.strip(r) for r in rows)
        self.assertIn("Rotate ghp_", text)
        self.assertNotIn(GHTOK, text)
        self.assertIn("keepme/repo", text)
        self.assertNotIn("Drop this one", text)
        self.assertIn("keep reflection", text)
        self.assertNotIn("work reflection", text)

    def test_no_daemon_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            h = host("[sources.drover]\nstate_dir = %s\n" % repr(d))  # no daemon.json
            rows = interactive.search(h, "q")
        self.assertEqual(len(rows), 1)
        self.assertIn("no Drover daemon", T.strip(rows[0]))

    def test_no_hits_says_nothing_found(self):
        with tempfile.TemporaryDirectory() as d:
            self._state(d)
            h = host("[sources.drover]\nstate_dir = %s\n" % repr(d))
            fake = FakeHTTP({"op/brain.search": resp(200, {"ok": True, "result": {"hits": []}}),
                            "op/reflections.search": resp(200, {"ok": True, "result": {"items": []}})})
            rows = interactive.search(h, "q", http=fake)
        text = "\n".join(T.strip(r) for r in rows)
        self.assertEqual(text.count("nothing found"), 2)


class RunSearchGlue(unittest.TestCase):
    def test_sets_focus_and_rows(self):
        app = _app()
        with mock.patch.object(interactive, "search", return_value=["a row"]):
            app.run_search("hello")
        self.assertEqual(app.focus, "search")
        self.assertEqual(app.scroll, 0)
        self.assertIn("a row", app.search_rows)

    def test_blank_query_does_nothing(self):
        app = _app()
        app.focus = "panels"
        with mock.patch.object(interactive, "search") as fn:
            app.run_search("   ")
        fn.assert_not_called()
        self.assertEqual(app.focus, "panels")


# ── the run loop ─────────────────────────────────────────────────────────────
class FakeTerm:
    def __init__(self, keys):
        self.keys = list(keys)

    def key(self, timeout):
        return self.keys.pop(0) if self.keys else "q"


class FakeStdout:
    def __init__(self):
        self.frames = 0

    def write(self, s):
        if s.startswith("\x1b[H"):
            self.frames += 1

    def flush(self):
        pass


def _fast_reload(self):
    """A stand-in for App.reload that skips real panel-building: the run-loop tests are
    about when frame() gets called, not about what load() builds."""
    self.panels, self.al, self.cache = [], [], None
    self.pi = 0
    self.loaded = time.monotonic()


class RunLoop(unittest.TestCase):
    def test_idle_timeouts_do_not_redraw(self):
        h = host("[interactive]\nrefresh = 1000\n")
        app = interactive.App(h, term=FakeTerm([None, None, None, "q"]))
        out = FakeStdout()
        with mock.patch.object(interactive.App, "reload", _fast_reload), \
                mock.patch.object(interactive.App, "size", lambda self: (80, 24)), \
                mock.patch("sys.stdout", out):
            app.run()
        self.assertEqual(out.frames, 1)

    def test_a_reload_past_refresh_redraws_once_more(self):
        h = host("[interactive]\nrefresh = 1e-9\n")
        app = interactive.App(h, term=FakeTerm([None, "q"]))
        out = FakeStdout()
        with mock.patch.object(interactive.App, "reload", _fast_reload), \
                mock.patch.object(interactive.App, "size", lambda self: (80, 24)), \
                mock.patch("sys.stdout", out):
            app.run()
        self.assertEqual(out.frames, 2)


class RunNoTTY(unittest.TestCase):
    def test_returns_2_without_a_terminal(self):
        buf = io.StringIO()
        with mock.patch("sys.stdin", mock.Mock(isatty=lambda: False)), mock.patch("sys.stderr", buf):
            rc = interactive.run(host())
        self.assertEqual(rc, 2)
        self.assertIn("needs a terminal", buf.getvalue())


# ── the plain dashboard never imports this module ─────────────────────────────
class PlainDashboard(unittest.TestCase):
    def test_show_does_not_import_interactive(self):
        with tempfile.TemporaryDirectory() as d:
            cache_dir, cfg_dir = os.path.join(d, "cache"), os.path.join(d, "config")
            code = ("import sys; sys.path.insert(0, %r)\n"
                    "from porchlight.cli import main\n"
                    "rc = main(['--width', '100', '--no-refresh'])\n"
                    "print('rc', rc)\n"
                    "print('clean', 'porchlight.interactive' not in sys.modules)\n" % str(support.ROOT))
            env = dict(os.environ, PORCHLIGHT_CACHE_DIR=cache_dir, PORCHLIGHT_CONFIG=os.path.join(cfg_dir, "config.toml"),
                      PORCHLIGHT_CONFIG_DIR=cfg_dir)
            p = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("rc 0", p.stdout)
        self.assertIn("clean True", p.stdout)



class Foreground(unittest.TestCase):
    def test_ctrl_c_while_a_program_runs_does_not_kill_it_or_porchlight(self):
        calls = {"n": 0}

        class P:
            def wait(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise KeyboardInterrupt  # the terminal's SIGINT reached porchlight too
                return 0

        with mock.patch.object(interactive.subprocess, "Popen", lambda argv: P()):
            self.assertEqual(interactive.run_foreground(["prog"]), 0)
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()

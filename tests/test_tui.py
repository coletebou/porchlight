"""porchlight -i (tui.py): the pure Model (navigation, selection, filtering,
modals, the write/run policy) plus two process-level checks (non-TTY, a real
pty session). The curses View itself is only exercised by the pty test.
"""
import support  # noqa: F401 — first: scratch cache and config

import os
import subprocess
import sys
import unittest

from porchlight import actions, tui
from porchlight.ui import Action, Item, Panel
from support import host

try:
    import pty
except ImportError:  # pragma: no cover — no pty on this platform
    pty = None

CANARY = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0" * 2  # a token shape, built at runtime


class FakeFrame:
    """Just enough of a render.Frame for tui.build_panel_views: .built (Panel
    list, in layout order) and .al (ranked alert dicts)."""

    def __init__(self, built=None, al=None):
        self.built = list(built or [])
        self.al = list(al or [])


def model_for(built=None, al=None, toml=""):
    return tui.Model(host(toml), FakeFrame(built, al))


calls = []


def setUpModule():
    pass


def tearDownModule():
    actions.HANDLERS.pop("tuitest.write", None)


@actions.action("tuitest.write", writes=True)
def _write(h, args):
    calls.append(args)
    return actions.Result(True, "done")


class Navigation(unittest.TestCase):
    def setUp(self):
        self.items = [Item("a", "alpha"), Item("b", "beta"), Item("c", "gamma")]
        self.p1 = Panel("p1", "P1", [], items=self.items)
        self.p2 = Panel("p2", "P2", [], items=[Item("x", "ex")])
        self.m = model_for([self.p1, self.p2])

    def test_rows_move_and_clamp(self):
        self.m.focus = "rows"
        self.assertEqual(self.m.current_row().id, "a")
        self.m.handle_key("UP")  # clamp at the top
        self.assertEqual(self.m.current_row().id, "a")
        self.m.handle_key("j")
        self.m.handle_key("j")
        self.assertEqual(self.m.current_row().id, "c")
        self.m.handle_key("DOWN")  # clamp at the bottom
        self.assertEqual(self.m.current_row().id, "c")
        self.m.handle_key("g")
        self.assertEqual(self.m.current_row().id, "a")
        self.m.handle_key("G")
        self.assertEqual(self.m.current_row().id, "c")

    def test_panels_move_and_clamp(self):
        self.assertEqual(self.m.focus, "panels")
        self.assertEqual(self.m.panel.key, "p1")
        self.m.handle_key("k")  # clamp at the top
        self.assertEqual(self.m.panel.key, "p1")
        self.m.handle_key("j")
        self.assertEqual(self.m.panel.key, "p2")
        self.m.handle_key("PGDN")
        self.assertEqual(self.m.panel.key, "p2")

    def test_tab_and_left_right_switch_panes(self):
        self.assertEqual(self.m.focus, "panels")
        self.m.handle_key("TAB")
        self.assertEqual(self.m.focus, "rows")
        self.m.handle_key("TAB")
        self.assertEqual(self.m.focus, "panels")
        self.m.handle_key("l")
        self.assertEqual(self.m.focus, "rows")
        self.m.handle_key("h")
        self.assertEqual(self.m.focus, "panels")


class AttentionAndRows(unittest.TestCase):
    def test_attention_panel_comes_first_and_only_crit_warn(self):
        al = [{"sev": "crit", "msg": "disk full", "src": "system", "fix": "clear /tmp"},
              {"sev": "info", "msg": "fyi only"},
              {"sev": "warn", "msg": "cert expiring"}]
        p = Panel("system", "SYSTEM", ["load 0.4"])
        m = model_for([p], al)
        self.assertEqual(m.panels[0].key, tui.ATTENTION_KEY)
        self.assertEqual(len(m.panels[0].rows), 2)  # info is not attention
        self.assertEqual(m.panels[1].key, "system")

    def test_attention_items_carry_source_and_fix(self):
        al = [{"sev": "crit", "msg": "disk full", "src": "system", "fix": "clear /tmp"}]
        m = model_for([], al)
        row = m.panels[0].rows[0]
        self.assertIn("source: system", row.detail)
        self.assertIn("fix: clear /tmp", row.detail)
        self.assertEqual(row.actions[0].name, "copy")
        self.assertEqual(row.actions[0].args["text"], "clear /tmp")

    def test_item_less_panel_becomes_read_only_line_rows(self):
        p = Panel("system", "SYSTEM", ["load 0.4", "mem 61%"], level="ok")
        m = model_for([p])
        rows = m.visible_rows(m.panel)
        self.assertEqual([r.text for r in rows], ["load 0.4", "mem 61%"])
        self.assertEqual(rows[0].actions, [])

    def test_no_attention_panel_when_nothing_crit_or_warn(self):
        m = model_for([Panel("p", "P", ["ok"])], al=[{"sev": "info", "msg": "fyi"}])
        self.assertEqual(m.panels[0].key, "p")


class Detail(unittest.TestCase):
    def test_enter_opens_and_esc_or_backspace_close(self):
        m = model_for([Panel("p", "P", [], items=[Item("a", "alpha")])])
        self.assertEqual(m.mode, "normal")
        m.handle_key("ENTER")
        self.assertEqual(m.mode, "detail")
        m.handle_key("ESC")
        self.assertEqual(m.mode, "normal")
        m.handle_key("ENTER")
        m.handle_key("BACKSPACE")
        self.assertEqual(m.mode, "normal")

    def test_enter_does_nothing_without_a_row(self):
        m = model_for([])
        m.handle_key("ENTER")
        self.assertEqual(m.mode, "normal")


class Filter(unittest.TestCase):
    def setUp(self):
        self.m = model_for([Panel("p", "P", [], items=[Item("a", "alpha"), Item("b", "beta"), Item("c", "gamma")])])

    def test_filter_narrows_the_current_panel(self):
        self.m.handle_key("/")
        self.assertEqual(self.m.mode, "filter")
        for ch in "eta":
            self.m.handle_key(ch)
        self.m.handle_key("ENTER")
        self.assertEqual(self.m.mode, "normal")
        self.assertEqual(self.m.filter_text, "eta")
        self.assertEqual([r.id for r in self.m.visible_rows(self.m.panel)], ["b"])

    def test_esc_cancels_without_changing_the_filter(self):
        self.m.handle_key("/")
        self.m.handle_key("x")
        self.m.handle_key("ESC")
        self.assertEqual(self.m.mode, "normal")
        self.assertEqual(self.m.filter_text, "")

    def test_backspace_edits_the_buffer(self):
        self.m.handle_key("/")
        self.m.handle_key("a")
        self.m.handle_key("b")
        self.m.handle_key("BACKSPACE")
        self.assertEqual(self.m.filter_buf, "a")


class Reload(unittest.TestCase):
    def test_selection_kept_by_id_across_reload(self):
        items1 = [Item("a", "alpha"), Item("b", "beta"), Item("c", "gamma")]
        m = model_for([Panel("p", "P", [], items=items1)])
        m.focus = "rows"
        m.handle_key("j")  # select "b"
        self.assertEqual(m.current_row().id, "b")
        items2 = [Item("a", "alpha"), Item("c", "gamma"), Item("b", "beta")]  # reordered
        m.reload(FakeFrame([Panel("p", "P", [], items=items2)]))
        self.assertEqual(m.current_row().id, "b")
        self.assertEqual(m.row_index(m.panel), 2)

    def test_panel_selection_kept_by_key_across_reload(self):
        m = model_for([Panel("p1", "P1", ["x"]), Panel("p2", "P2", ["y"])])
        m.handle_key("j")
        self.assertEqual(m.panel.key, "p2")
        m.reload(FakeFrame([Panel("p2", "P2", ["y"]), Panel("p1", "P1", ["x"])]))
        self.assertEqual(m.panel.key, "p2")


class AskAI(unittest.TestCase):
    def test_builds_argv_and_question_and_foregrounds_when_runnable(self):
        toml = '[interactive]\nai_command = ["true"]\nrun_allow = ["true"]\n'
        p = Panel("drover", "DROVER", [], items=[Item("w", "waiting agent", detail=["started 2h ago"])])
        m = model_for([p], toml=toml)
        effects = m.handle_key("a")
        self.assertEqual(len(effects), 1)
        tag, act = effects[0]
        self.assertEqual(tag, "run_foreground")
        self.assertEqual(act.name, "run")
        self.assertTrue(act.args["pause"])
        self.assertEqual(act.args["argv"][0], "true")
        self.assertEqual(act.args["argv"][-1],
                          "DROVER · waiting agent · started 2h ago. Explain this and tell me what to do first.")

    def test_shows_the_result_message_when_not_runnable(self):
        toml = '[interactive]\nai_command = ["definitely-not-a-real-binary-xyz"]\n'
        p = Panel("drover", "DROVER", [], items=[Item("w", "waiting agent")])
        m = model_for([p], toml=toml)
        effects = m.handle_key("a")
        self.assertEqual(effects, [])
        self.assertIn("run_allow", m.status)

    def test_nothing_selected(self):
        m = model_for([])
        self.assertEqual(m.handle_key("a"), [])
        self.assertIn("nothing selected", m.status)


class WritePolicy(unittest.TestCase):
    def setUp(self):
        calls.clear()
        self.act = Action("tuitest.write", "do it", "d", {"n": 1}, writes=True, confirm="Do it?")
        self.item = Item("i", "an item", actions=[self.act])

    def test_refused_when_read_only_with_no_modal(self):
        m = model_for([Panel("p", "P", [], items=[self.item])])
        effects = m.handle_key("d")
        self.assertEqual(m.mode, "normal")
        self.assertEqual(len(effects), 1)
        tag, act, confirmed = effects[0]
        self.assertEqual((tag, confirmed), ("action", False))
        res = actions.run(m.host, act, confirmed=confirmed)
        self.assertFalse(res.ok)
        self.assertIn("allow_writes", res.msg)
        self.assertEqual(calls, [])

    def test_y_confirms_with_allow_writes(self):
        m = model_for([Panel("p", "P", [], items=[self.item])], toml="[interactive]\nallow_writes = true\n")
        self.assertEqual(m.handle_key("d"), [])
        self.assertEqual(m.mode, "modal")
        self.assertEqual(m.modal_kind, "confirm")
        effects = m.handle_key("y")
        self.assertEqual(m.mode, "normal")
        self.assertEqual(effects, [("action", self.act, True)])
        res = actions.run(m.host, self.act, confirmed=True)
        self.assertTrue(res.ok)
        self.assertEqual(calls, [{"n": 1}])

    def test_n_cancels_with_allow_writes(self):
        m = model_for([Panel("p", "P", [], items=[self.item])], toml="[interactive]\nallow_writes = true\n")
        m.handle_key("d")
        effects = m.handle_key("n")
        self.assertEqual(m.mode, "normal")
        self.assertEqual(effects, [])
        self.assertEqual(calls, [])


class InputConvention(unittest.TestCase):
    def test_empty_answer_cancels(self):
        act = Action("tuitest.write", "ask", "i", {"input": "Name?"})
        m = model_for([Panel("p", "P", [], items=[Item("i", "row", actions=[act])])])
        self.assertEqual(m.handle_key("i"), [])
        self.assertEqual(m.mode, "modal")
        self.assertEqual(m.modal_kind, "input")
        self.assertEqual(m.input_prompt, "Name?")
        effects = m.handle_key("ENTER")
        self.assertEqual(effects, [])
        self.assertEqual(m.mode, "normal")

    def test_text_is_passed_as_args_text(self):
        act = Action("tuitest.write", "ask", "i", {"input": "Name?"}, writes=True)
        m = model_for([Panel("p", "P", [], items=[Item("i", "row", actions=[act])])],
                      toml="[interactive]\nallow_writes = true\n")
        m.handle_key("i")
        for ch in "ok":
            m.handle_key(ch)
        effects = m.handle_key("ENTER")
        # writes=True still asks y/N after the text is collected
        self.assertEqual(effects, [])
        self.assertEqual(m.mode, "modal")
        self.assertEqual(m.modal_kind, "confirm")
        effects = m.handle_key("y")
        self.assertEqual(len(effects), 1)
        act2 = effects[0][1]
        self.assertEqual(act2.args["text"], "ok")
        self.assertNotIn("input", act2.args)

    def test_backspace_edits_the_input_buffer(self):
        act = Action("tuitest.write", "ask", "i", {"input": "Name?"})
        m = model_for([Panel("p", "P", [], items=[Item("i", "row", actions=[act])])])
        m.handle_key("i")
        m.handle_key("o")
        m.handle_key("k")
        m.handle_key("BACKSPACE")
        self.assertEqual(m.input_buffer, "o")


class Masking(unittest.TestCase):
    def test_canary_never_reaches_a_row(self):
        item = Item("i", "leak " + CANARY, detail=["more " + CANARY])
        p = Panel("p", "P", ["line " + CANARY], items=[item])
        al = [{"sev": "crit", "msg": "alert " + CANARY, "src": "s", "fix": "fix " + CANARY}]
        m = model_for([p], al)
        blob = repr([(pv.title, pv.note, [(r.text, r.detail, [a.args for a in r.actions]) for r in pv.rows])
                     for pv in m.panels])
        self.assertNotIn(CANARY, blob)

    def test_canary_never_reaches_an_item_less_line_row(self):
        p = Panel("p", "P", ["\x1b[31mred " + CANARY + "\x1b[0m"])
        m = model_for([p])
        self.assertNotIn(CANARY, m.panels[0].rows[0].text)
        self.assertNotIn("\x1b[", m.panels[0].rows[0].text)


class Help(unittest.TestCase):
    def test_help_opens_and_closes_on_any_key(self):
        m = model_for([])
        m.handle_key("?")
        self.assertEqual((m.mode, m.modal_kind), ("modal", "help"))
        m.handle_key("x")
        self.assertEqual(m.mode, "normal")


class Refresh(unittest.TestCase):
    def test_r_requests_a_reread_and_a_background_collect(self):
        m = model_for([])
        self.assertEqual(m.handle_key("r"), [("refresh",), ("spawn_collect", None)])


class Quit(unittest.TestCase):
    def test_q_and_ctrl_c_quit_from_normal_mode(self):
        self.assertEqual(model_for([]).handle_key("q"), [("quit",)])
        self.assertEqual(model_for([]).handle_key("CTRL_C"), [("quit",)])

    def test_q_quits_from_detail_mode_too(self):
        m = model_for([Panel("p", "P", [], items=[Item("a", "alpha")])])
        m.handle_key("ENTER")
        self.assertEqual(m.mode, "detail")
        self.assertEqual(m.handle_key("q"), [("quit",)])


class OpenAndCopy(unittest.TestCase):
    def test_o_without_a_url_sets_a_status_message(self):
        m = model_for([Panel("p", "P", [], items=[Item("a", "alpha")])])
        self.assertEqual(m.handle_key("o"), [])
        self.assertIn("URL", m.status)

    def test_o_with_a_url_runs_open_url(self):
        m = model_for([Panel("p", "P", [], items=[Item("a", "alpha", url="https://example.com")])])
        effects = m.handle_key("o")
        self.assertEqual(len(effects), 1)
        tag, act, confirmed = effects[0]
        self.assertEqual((tag, act.name, confirmed), ("action", "open_url", False))
        self.assertEqual(act.args["url"], "https://example.com")

    def test_y_copies_the_item_text_by_default(self):
        m = model_for([Panel("p", "P", [], items=[Item("a", "alpha")])])
        effects = m.handle_key("y")
        tag, act, confirmed = effects[0]
        self.assertEqual((tag, act.name, confirmed), ("action", "copy", False))
        self.assertEqual(act.args["text"], "alpha")

    def test_y_prefers_the_items_own_copy_action(self):
        copy_act = Action("copy", "copy fix", "y", {"text": "the fix"})
        m = model_for([Panel("p", "P", [], items=[Item("a", "alpha", actions=[copy_act])])])
        effects = m.handle_key("y")
        self.assertEqual(effects[0][1].args["text"], "the fix")


class OnResult(unittest.TestCase):
    def test_a_refresh_result_asks_for_a_background_collect(self):
        m = model_for([])
        act = Action("tuitest.write", "do it", "d", {})
        res = actions.Result(True, "done", refresh=("todos",))
        effects = m.on_result(act, res)
        self.assertEqual(m.status, "done")
        self.assertEqual(effects, [("spawn_collect", ("todos",))])

    def test_a_result_with_no_refresh_asks_for_nothing(self):
        m = model_for([])
        self.assertEqual(m.on_result(Action("x", "x", "x"), actions.Result(True, "ok")), [])


@unittest.skipUnless(pty, "no pty on this platform")
class NonTty(unittest.TestCase):
    def test_piped_stdout_returns_2_with_a_hint(self):
        exe = str(support.ROOT / "bin" / "porchlight")
        p = subprocess.run([sys.executable, exe, "-i"], capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
        self.assertEqual(p.returncode, 2)
        self.assertIn(b"porchlight view", p.stderr)


@unittest.skipUnless(pty, "no pty on this platform")
class EndToEnd(unittest.TestCase):
    """A real porchlight -i session in a pseudo-terminal: start it on the demo
    cache, wait for a known panel, move around, then quit."""

    def _winsize(self, fd, rows, cols):
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _wait_for(self, master_fd, needle: bytes, timeout=15.0) -> bytes:
        import select
        deadline = __import__("time").monotonic() + timeout
        buf = b""
        while __import__("time").monotonic() < deadline:
            r, _, _ = select.select([master_fd], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if needle in buf:
                    return buf
        return buf

    def test_demo_session_navigates_and_quits_cleanly(self):
        import time
        exe = str(support.ROOT / "bin" / "porchlight")
        master_fd, slave_fd = os.openpty()
        self._winsize(slave_fd, 48, 160)
        env = dict(os.environ, TERM="xterm-256color")
        proc = subprocess.Popen([sys.executable, exe, "demo", "-i"], stdin=slave_fd, stdout=slave_fd,
                                 stderr=slave_fd, env=env, start_new_session=True, close_fds=True)
        os.close(slave_fd)
        try:
            seen = self._wait_for(master_fd, b"DROVER", timeout=15.0)
            self.assertIn(b"DROVER", seen, "the demo's DROVER panel never showed up:\n%r" % seen[-2000:])
            for ch in (b"j", b"j", b"\r", b"\x1b", b"?", b"x", b"/", b"\x1b"):
                os.write(master_fd, ch)
                time.sleep(0.1)
            os.write(master_fd, b"q")
            rc = proc.wait(timeout=20)
            self.assertEqual(rc, 0)
        finally:
            proc.poll()
            if proc.returncode is None:
                proc.kill()
                proc.wait(timeout=5)
            os.close(master_fd)


if __name__ == "__main__":
    unittest.main()

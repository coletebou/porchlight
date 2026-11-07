"""porchlight -i: the interactive dashboard (docs/interactive.md).

A pure, curses-free Model (navigation, selection, filtering, modals, the action
policy) plus a thin curses View that draws it and executes the effects it
returns. Tests drive the Model directly; only the pty end-to-end test and a
manual run touch curses. Imported only for -i: nothing here runs on the login
path, so a slow import here never slows a login.

Keys: see docs/interactive.md. Effects handle_key() can return:
  ("action", Action, confirmed)   the View calls actions.run(host, act, confirmed)
  ("run_foreground", Action)      the View leaves the alternate screen, runs it,
                                   honours args["pause"], then comes back
  ("refresh",)                    reread the cache (render.prepare) into model.reload()
  ("spawn_collect", names|None)   start `porchlight collect` in the background
  ("quit",)                       leave the main loop
"""
from __future__ import annotations

import os
import sys
import time

from . import actions as A
from .ui import Action

HELP_TEXT = (
    "porchlight -i",
    "",
    "  up/down, j/k       move in the focused pane      PgUp/PgDn, g/G   page, top/bottom",
    "  Tab, left/right, h/l   switch between the panel list and its rows",
    "  Enter              open the selected row          Esc, Backspace   back",
    "  o                  open its URL                   y                copy",
    "  a                  ask the AI CLI about it         r                refresh",
    "  /                  filter the current panel        ?                this help",
    "  q, Ctrl-C          quit",
    "",
    "Rows may carry their own keys too (shown in the detail view).",
    "Press any key to close this help.",
)


# ── helpers shared by the Model and the View ─────────────────────────────────
def _clean(s) -> str:
    """Every string the TUI draws goes through this: ANSI stripped, secrets masked."""
    from . import secrets, theme as T
    return secrets.mask_shapes(T.strip(str(s or "")))


class Row:
    """One selectable line in the rows pane: either a real ui.Item, or a plain
    line from an item-less panel (then actions is empty and the row is read-only
    besides copy/open)."""

    __slots__ = ("id", "text", "detail", "level", "url", "actions")

    def __init__(self, id, text, detail=None, level="ok", url="", actions=None):
        self.id, self.text = id, text
        self.detail = list(detail or [])
        self.level, self.url = level, url
        self.actions = list(actions or [])


class PanelView:
    __slots__ = ("key", "title", "level", "note", "rows")

    def __init__(self, key, title, level, note, rows):
        self.key, self.title, self.level, self.note, self.rows = key, title, level, note, rows


ATTENTION_KEY = "attention"


def _attention_rows(al):
    import hashlib
    rows = []
    for a in al:
        if a.get("sev") not in ("crit", "warn"):
            continue
        msg = _clean(a.get("msg"))
        src = _clean(a.get("src"))
        fix = _clean(a.get("fix"))
        rid = "attn:%s:%s" % (src or "-", hashlib.sha1(msg.encode("utf-8", "ignore")).hexdigest()[:10])
        detail = ([("source: " + src)] if src else []) + ([("fix: " + fix)] if fix else [])
        acts = [Action("copy", "copy fix", "y", {"text": fix})] if fix else []
        rows.append(Row(rid, msg, detail, a.get("sev") or "warn", "", acts))
    return rows


def _panel_rows(p):
    if getattr(p, "items", None):
        return [Row(it.id, _clean(it.text), [_clean(d) for d in it.detail], it.level or "ok",
                     str(it.url or ""), list(it.actions)) for it in p.items]
    return [Row("%s:L%d" % (p.key, i), _clean(ln), [], p.level or "ok") for i, ln in enumerate(p.lines)]


def build_panel_views(frame) -> list:
    """The ATTENTION pseudo-panel (when there is anything in it) followed by every
    built panel, each turned into rows: Items when the panel has them, else one
    read-only row per plain line."""
    from . import theme as T
    views = []
    attn = _attention_rows(frame.al)
    if attn:
        level = T.worst(*(r.level for r in attn))
        views.append(PanelView(ATTENTION_KEY, "ATTENTION", level, "", attn))
    for p in frame.built:
        views.append(PanelView(p.key, _clean(p.title), p.level or "ok", _clean(p.note), _panel_rows(p)))
    return views


# ── Model ─────────────────────────────────────────────────────────────────
class Model:
    """Everything porchlight -i needs to decide what a keypress does. No curses,
    no network, no subprocess: tests construct one from a host and a
    render.prepare() Frame and drive it with handle_key()."""

    def __init__(self, host, frame, fleet=None):
        self.host = host
        self.frame = None
        self.fleet = {}
        self.panels = []
        self.panel_idx = 0
        self.focus = "panels"          # "panels" | "rows"
        self.mode = "normal"           # "normal" | "detail" | "filter" | "modal"
        self.modal_kind = None         # "confirm" | "input" | "help"
        self.pending_action = None
        self.input_prompt = ""
        self.input_buffer = ""
        self.filter_text = ""
        self.filter_buf = ""
        self.sel = {}                  # panel key -> selected row id
        self.status = ""
        self.detail_return_focus = "rows"
        self.reload(frame, fleet)

    # ── reload / lookups ──
    def reload(self, frame, fleet=None) -> None:
        """Rebuild the panels from a fresh Frame, keeping the selected panel and
        row by id wherever they still exist."""
        self.frame = frame
        if fleet is not None:
            self.fleet = fleet
        prev_key = self.panels[self.panel_idx].key if self.panels else None
        self.panels = build_panel_views(frame)
        if not self.panels:
            self.panel_idx = 0
            return
        if prev_key is not None:
            for i, p in enumerate(self.panels):
                if p.key == prev_key:
                    self.panel_idx = i
                    break
            else:
                self.panel_idx = min(self.panel_idx, len(self.panels) - 1)
        else:
            self.panel_idx = 0
        for p in self.panels:
            if p.rows and p.key not in self.sel:
                self.sel[p.key] = p.rows[0].id

    @property
    def panel(self):
        return self.panels[self.panel_idx] if self.panels else None

    def visible_rows(self, panel):
        if not panel:
            return []
        if not self.filter_text:
            return panel.rows
        needle = self.filter_text.lower()
        return [r for r in panel.rows if needle in r.text.lower()]

    def row_index(self, panel) -> int:
        rows = self.visible_rows(panel)
        if not rows:
            return -1
        rid = self.sel.get(panel.key)
        for i, r in enumerate(rows):
            if r.id == rid:
                return i
        return 0

    def current_row(self):
        p = self.panel
        if not p:
            return None
        rows = self.visible_rows(p)
        i = self.row_index(p)
        return rows[i] if 0 <= i < len(rows) else None

    # ── movement ──
    def _move_panel(self, delta) -> None:
        if not self.panels:
            return
        self.panel_idx = max(0, min(len(self.panels) - 1, self.panel_idx + delta))

    def _move_row(self, delta) -> None:
        p = self.panel
        rows = self.visible_rows(p) if p else []
        if not rows:
            return
        i = max(0, min(len(rows) - 1, self.row_index(p) + delta))
        self.sel[p.key] = rows[i].id

    def _move(self, delta) -> None:
        self._move_row(delta) if self.focus == "rows" else self._move_panel(delta)

    # ── key entry point ──
    def handle_key(self, key) -> list:
        if self.mode == "modal":
            if self.modal_kind == "confirm":
                return self._key_confirm(key)
            if self.modal_kind == "input":
                return self._key_input(key)
            # "help": any key closes it
            self.mode, self.modal_kind = "normal", None
            return []
        if self.mode == "filter":
            return self._key_filter(key)
        if self.mode == "detail":
            return self._key_detail(key)
        return self._key_normal(key)

    def _key_normal(self, key) -> list:
        if key in ("q", "CTRL_C"):
            return [("quit",)]
        if key in ("UP", "k"):
            self._move(-1)
        elif key in ("DOWN", "j"):
            self._move(1)
        elif key == "PGUP":
            self._move(-10)
        elif key == "PGDN":
            self._move(10)
        elif key == "g":
            self._move(-1_000_000)
        elif key == "G":
            self._move(1_000_000)
        elif key == "TAB":
            self.focus = "rows" if self.focus == "panels" else "panels"
        elif key in ("RIGHT", "l"):
            self.focus = "rows"
        elif key in ("LEFT", "h"):
            self.focus = "panels"
        elif key == "ENTER":
            if self.current_row() is not None:
                self.detail_return_focus = self.focus
                self.mode = "detail"
        elif key == "/":
            self.filter_buf = self.filter_text
            self.mode = "filter"
        elif key == "?":
            self.mode, self.modal_kind = "modal", "help"
        elif key == "r":
            return [("refresh",), ("spawn_collect", None)]
        elif key == "o":
            return self._open_url()
        elif key == "y":
            return self._copy()
        elif key == "a":
            return self._ask_ai()
        else:
            return self._item_key(key)
        return []

    def _key_detail(self, key) -> list:
        if key in ("ESC", "BACKSPACE"):
            self.mode = "normal"
            self.focus = self.detail_return_focus
        elif key in ("q", "CTRL_C"):
            return [("quit",)]
        elif key == "o":
            return self._open_url()
        elif key == "y":
            return self._copy()
        elif key == "a":
            return self._ask_ai()
        else:
            return self._item_key(key)
        return []

    def _item_key(self, key) -> list:
        row = self.current_row()
        if row:
            for act in row.actions:
                if act.key == key:
                    return self._invoke(act)
        return []

    def _key_filter(self, key) -> list:
        if key == "ENTER":
            self.filter_text = self.filter_buf
            self.mode = "normal"
        elif key == "ESC":
            self.mode = "normal"
        elif key == "BACKSPACE":
            self.filter_buf = self.filter_buf[:-1]
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.filter_buf += key
        return []

    def _key_confirm(self, key) -> list:
        act, self.pending_action = self.pending_action, None
        self.mode, self.modal_kind = "normal", None
        return [("action", act, True)] if key == "y" else []

    def _key_input(self, key) -> list:
        if key == "ENTER":
            act, self.pending_action = self.pending_action, None
            text, self.input_buffer = self.input_buffer, ""
            self.mode, self.modal_kind = "normal", None
            if not text:
                return []
            args = dict(act.args)
            args.pop("input", None)
            args["text"] = text
            return self._invoke(Action(act.name, act.label, act.key, args, writes=act.writes, confirm=act.confirm))
        if key == "ESC":
            self.pending_action, self.input_buffer = None, ""
            self.mode, self.modal_kind = "normal", None
        elif key == "BACKSPACE":
            self.input_buffer = self.input_buffer[:-1]
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.input_buffer += key
        return []

    # ── built-in item actions ──
    def _open_url(self) -> list:
        row = self.current_row()
        if not row or not row.url:
            self.status = "no URL for this item"
            return []
        return self._invoke(Action("open_url", "open", "o", {"url": row.url}))

    def _copy(self) -> list:
        row = self.current_row()
        if not row:
            return []
        for act in row.actions:
            if act.name == "copy":
                return self._invoke(act)
        return self._invoke(Action("copy", "copy", "y", {"text": row.text}))

    def _ask_ai(self) -> list:
        row, panel = self.current_row(), self.panel
        if not row or not panel:
            self.status = "nothing selected to ask about"
            return []
        bits = [panel.title, row.text] + row.detail
        question = " · ".join(b for b in bits if b) + ". Explain this and tell me what to do first."
        cmd = self.host.cfg.get("interactive.ai_command", ["ai"])
        argv = [str(a) for a in cmd] if isinstance(cmd, list) else [str(cmd)]
        return self._invoke_run(Action("run", "ask ai", "a", {"argv": argv + [question], "pause": True}))

    # ── dispatch ──
    def _invoke(self, act) -> list:
        if "input" in act.args:
            self.pending_action = act
            self.input_prompt = str(act.args.get("input") or "")
            self.input_buffer = ""
            self.mode, self.modal_kind = "modal", "input"
            return []
        if act.name == "run":
            return self._invoke_run(act)
        h = A.HANDLERS.get(act.name)
        writes = bool(act.writes or (h and h.writes))
        if writes and not A.writes_allowed(self.host):
            return [("action", act, False)]  # the View shows actions.run's refusal; no modal
        if writes:
            self.pending_action = act
            self.mode, self.modal_kind = "modal", "confirm"
            return []
        return [("action", act, False)]

    def _invoke_run(self, act) -> list:
        argv = act.args.get("argv")
        if self._runnable(argv):
            return [("run_foreground", act)]
        res = A.run(self.host, act, confirmed=True)  # never runs: run_command fails the same checks first
        self.status = res.msg
        return []

    def _runnable(self, argv) -> bool:
        import shutil
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            return False
        allow = self.host.cfg.get("interactive.run_allow", list(A.RUN_ALLOW))
        allow = allow if isinstance(allow, list) else list(A.RUN_ALLOW)
        return os.path.basename(argv[0]) in allow and bool(shutil.which(argv[0]))

    def on_result(self, act, res) -> list:
        """Feed an executed Action's Result back in: updates the status line and,
        when it asks for a refresh, returns a spawn_collect effect."""
        self.status = res.msg
        return [("spawn_collect", tuple(res.refresh))] if res.ok and res.refresh else []


# ── entry point ───────────────────────────────────────────────────────────
def run(host, flags, opts) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("TERM", "") == "dumb":
        print("porchlight -i needs an interactive terminal; try `porchlight view` instead.", file=sys.stderr)
        return 2
    try:
        import curses
    except ImportError:
        print("porchlight -i needs curses, which is not available here; try `porchlight view` instead.",
              file=sys.stderr)
        return 2
    from . import registry, render, view as V
    cache = registry.Cache()
    frame = render.prepare(host, cache, width=render.term_width(), only=None, login=False, compact=False)
    model = Model(host, frame, V.fleet_info(host, cache))
    try:
        return curses.wrapper(_curses_main, model, host)
    except curses.error as e:
        print("porchlight -i: terminal error (%s); try `porchlight view` instead." % e, file=sys.stderr)
        return 2


def _reload(model, host) -> None:
    from . import registry, render, view as V
    cache = registry.Cache()
    frame = render.prepare(host, cache, width=render.term_width(), only=None, login=False, compact=False)
    model.reload(frame, V.fleet_info(host, cache))


def _spawn_collect(host, names=None) -> None:
    """A detached, quiet `porchlight collect`, optionally scoped to `names`."""
    import subprocess
    from . import core
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    args = ["collect", "--quiet"] + (["--only", ",".join(names)] if names else [])
    code = "import sys; sys.path.insert(0, %r); from porchlight.cli import main; sys.exit(main(%r))" % (root, args)
    env = dict(os.environ, PORCHLIGHT_HOST=host.name, PORCHLIGHT_CACHE_DIR=core.CACHE_DIR)
    if getattr(host.cfg, "path", None) and host.cfg.path != "<string>":
        env["PORCHLIGHT_CONFIG"] = host.cfg.path
    try:
        subprocess.Popen([sys.executable, "-I", "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, env=env)
    except OSError:
        pass


def _run_foreground(stdscr, host, act):
    import curses
    curses.def_prog_mode()
    curses.endwin()
    try:
        res = A.run(host, act, confirmed=True)
        if act.args.get("pause"):
            try:
                print("\n[%s] press Enter to continue..." % ("ok" if res.ok else "failed"))
                input()
            except (EOFError, KeyboardInterrupt):
                pass
    finally:
        curses.reset_prog_mode()
        stdscr.clear()
        stdscr.refresh()
    return res


def _key_name(ch):
    """A curses getch() code -> the Model's logical key names, or a one-char
    string for an ordinary key, or None for anything the TUI does not use."""
    import curses
    names = {curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_LEFT: "LEFT", curses.KEY_RIGHT: "RIGHT",
             curses.KEY_NPAGE: "PGDN", curses.KEY_PPAGE: "PGUP", curses.KEY_HOME: "g", curses.KEY_END: "G",
             curses.KEY_BACKSPACE: "BACKSPACE"}
    if ch in names:
        return names[ch]
    if ch in (10, 13, curses.KEY_ENTER):
        return "ENTER"
    if ch == 27:
        return "ESC"
    if ch == 9:
        return "TAB"
    if ch in (127, 8):
        return "BACKSPACE"
    if ch == 3:
        return "CTRL_C"
    if 0 <= ch < 256:
        c = chr(ch)
        return c if c.isprintable() else None
    return None


def _init_colors() -> dict:
    import curses
    if not curses.has_colors():
        return {}
    from . import theme as T
    if T.MODE == "none":
        return {}
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    mapping = {"crit": curses.COLOR_RED, "warn": curses.COLOR_YELLOW, "ok": curses.COLOR_GREEN,
               "info": curses.COLOR_CYAN, "unknown": curses.COLOR_WHITE, "accent": curses.COLOR_MAGENTA}
    pairs = {}
    for i, (name, col) in enumerate(mapping.items(), start=1):
        try:
            curses.init_pair(i, col, bg)
            pairs[name] = curses.color_pair(i)
        except curses.error:
            pairs[name] = 0
    return pairs


def _glyph(level):
    from . import theme as T
    key = {"unknown": "hollow", "off": "hollow", "stale": "stale"}.get(level, "dot")
    return T.g(key)


def _safe_addstr(win, y, x, s, attr=0) -> None:
    try:
        h, w = win.getmaxyx()
    except Exception:  # noqa: BLE001 — a torn-down window must never crash the draw
        return
    if y < 0 or y >= h or x < 0 or x >= w - 1:
        return
    try:
        win.addstr(y, x, s[: w - x - 1], attr)
    except Exception:  # noqa: BLE001 — curses raises on the bottom-right cell
        pass


def _hline(win, y, x, w, attr=0) -> None:
    from . import theme as T
    _safe_addstr(win, y, x, T.g("rule") * max(0, w), attr)


def _curses_main(stdscr, model, host) -> int:
    import curses
    curses.curs_set(0)
    pairs = _init_colors()
    refresh_secs = float(host.cfg.get("interactive.refresh", 30) or 30)
    stdscr.timeout(int(max(1, refresh_secs) * 1000))
    last_reload = time.monotonic()
    last_spawn = 0.0

    def dispatch(effects):
        nonlocal last_reload, last_spawn
        for eff in effects:
            if eff[0] == "quit":
                return True
            if eff[0] == "refresh":
                _reload(model, host)
                last_reload = time.monotonic()
            elif eff[0] == "spawn_collect":
                now = time.monotonic()
                if now - last_spawn >= 30:
                    _spawn_collect(host, eff[1] if len(eff) > 1 and eff[1] else None)
                    last_spawn = now
            elif eff[0] == "action":
                _, act, confirmed = eff
                res = A.run(host, act, confirmed=confirmed)
                if dispatch(model.on_result(act, res)):
                    return True
            elif eff[0] == "run_foreground":
                res = _run_foreground(stdscr, host, eff[1])
                if dispatch(model.on_result(eff[1], res)):
                    return True
        return False

    while True:
        _draw(stdscr, model, host, pairs)
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            return 0
        now = time.monotonic()
        if ch == -1:
            if refresh_secs > 0 and now - last_reload >= refresh_secs:
                _reload(model, host)
                last_reload = now
            continue
        if ch == curses.KEY_RESIZE:
            curses.update_lines_cols()
            continue
        key = _key_name(ch)
        if key is None:
            continue
        if dispatch(model.handle_key(key)):
            return 0


def _draw(stdscr, model, host, pairs) -> None:
    import curses

    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 3 or w < 10:
        stdscr.refresh()
        return
    _safe_addstr(stdscr, 0, 0, _header_text(model, host)[: w - 1], pairs.get("accent", 0) | curses.A_BOLD)
    _hline(stdscr, 1, 0, w, 0)

    content_top, content_h = 2, max(1, h - 4)
    single = w < 80
    if single:
        _draw_rows(stdscr, model, pairs, 0, content_top, w, content_h) if model.focus == "rows" else \
            _draw_panels(stdscr, model, pairs, 0, content_top, w, content_h)
    else:
        left_w = min(30, w // 3)
        _draw_panels(stdscr, model, pairs, 0, content_top, left_w, content_h)
        for y in range(content_h):
            _safe_addstr(stdscr, content_top + y, left_w, "│", 0)
        _draw_rows(stdscr, model, pairs, left_w + 1, content_top, w - left_w - 1, content_h)

    if model.mode == "detail":
        _draw_detail(stdscr, model, pairs, h, w)
    elif model.mode == "modal" and model.modal_kind == "help":
        _draw_modal(stdscr, HELP_TEXT, h, w)
    elif model.mode == "modal" and model.modal_kind == "confirm":
        act = model.pending_action
        q = act.confirm or ("Run %r? [y/N]" % act.label)
        _draw_modal(stdscr, (q, "", "y confirms; anything else cancels."), h, w)

    _hline(stdscr, h - 2, 0, w, 0)
    bottom = h - 1
    if model.mode == "filter":
        _safe_addstr(stdscr, bottom, 0, "/ filter: " + model.filter_buf + "_", pairs.get("info", 0))
    elif model.mode == "modal" and model.modal_kind == "input":
        _safe_addstr(stdscr, bottom, 0, model.input_prompt + ": " + model.input_buffer + "_", pairs.get("info", 0))
    elif model.status:
        _safe_addstr(stdscr, bottom, 0, model.status, pairs.get("accent", 0))
    else:
        hint = "j/k move · Enter open · o open · y copy · a ask · r refresh · / filter · ? help · q quit"
        if model.filter_text:
            hint = ("filter: %s (Esc clears) · " % model.filter_text) + hint
        _safe_addstr(stdscr, bottom, 0, hint, 0)
    stdscr.refresh()


def _header_text(model, host) -> str:
    from . import core
    al = model.frame.al if model.frame else []
    nc = sum(1 for a in al if a.get("sev") == "crit")
    nw = sum(1 for a in al if a.get("sev") == "warn")
    fi = model.fleet or {}
    bits = [host.label, host.identity]
    role = fi.get("role", "solo")
    if role == "peer":
        age = fi.get("bundle_age")
        bits.append("peer of %s (%s old)" % (fi.get("hub") or "?", core.human_age(age) if age is not None else "?"))
    elif role == "hub":
        bits.append("hub")
    bits.append("writes: on" if A.writes_allowed(host) else "read-only")
    bits.append("%d crit · %d warn" % (nc, nw))
    return "porchlight -i  ·  " + "  ·  ".join(bits)


def _draw_panels(stdscr, model, pairs, x, y, w, h) -> None:
    import curses
    focused = model.focus == "panels"
    for i, p in enumerate(model.panels[: max(0, h)]):
        sel = focused and i == model.panel_idx
        attr = (curses.A_REVERSE if sel else 0) | pairs.get(p.level, 0)
        line = "%s %s %s(%d)" % (_glyph(p.level), p.title, (p.note + " ") if p.note else "", len(p.rows))
        _safe_addstr(stdscr, y + i, x, _clip(line, w), attr)


def _clip(s: str, w: int) -> str:
    """s, padded/cut to exactly w cells: a pane must never bleed into its
    neighbour just because one row's text ran long."""
    return (s[: max(0, w - 1)]).ljust(max(0, w - 1))


def _draw_rows(stdscr, model, pairs, x, y, w, h) -> None:
    import curses
    p = model.panel
    if not p:
        _safe_addstr(stdscr, y, x, "(no panels)", 0)
        return
    rows = model.visible_rows(p)
    idx = model.row_index(p)
    focused = model.focus == "rows"
    top = max(0, idx - h + 1) if idx >= h else 0
    for i, r in enumerate(rows[top:top + h]):
        sel = focused and (top + i) == idx
        attr = (curses.A_REVERSE if sel else 0) | pairs.get(r.level, 0)
        _safe_addstr(stdscr, y + i, x, _clip("%s %s" % (_glyph(r.level), r.text), w), attr)
    if not rows:
        _safe_addstr(stdscr, y, x, "(nothing here)" if not model.filter_text else "(no match)", 0)


def _draw_detail(stdscr, model, pairs, h, w) -> None:
    import curses
    row = model.current_row()
    if not row:
        model.mode = "normal"
        return
    bw, bh = max(20, min(w - 4, 80)), min(h - 4, 6 + len(row.detail) + len(row.actions))
    by, bx = max(1, (h - bh) // 2), max(1, (w - bw) // 2)
    for dy in range(bh):
        _safe_addstr(stdscr, by + dy, bx, " " * bw, curses.A_REVERSE)
    _safe_addstr(stdscr, by, bx + 1, row.text[: bw - 2], curses.A_REVERSE | curses.A_BOLD)
    line = by + 2
    for d in row.detail:
        _safe_addstr(stdscr, line, bx + 1, d[: bw - 2], curses.A_REVERSE)
        line += 1
    if row.url:
        _safe_addstr(stdscr, line, bx + 1, "url: " + row.url[: bw - 7], curses.A_REVERSE)
        line += 1
    for act in row.actions:
        _safe_addstr(stdscr, line, bx + 1, "[%s] %s" % (act.key, act.label), curses.A_REVERSE)
        line += 1
    _safe_addstr(stdscr, by + bh - 1, bx + 1, "Esc/Backspace: back", curses.A_REVERSE | curses.A_DIM)


def _draw_modal(stdscr, lines, h, w) -> None:
    import curses
    bw = max(20, min(w - 4, max((len(ln) for ln in lines), default=20) + 4))
    bh = min(h - 4, len(lines) + 2)
    by, bx = max(1, (h - bh) // 2), max(1, (w - bw) // 2)
    for dy in range(bh):
        _safe_addstr(stdscr, by + dy, bx, " " * bw, curses.A_REVERSE)
    for i, ln in enumerate(lines[: bh - 2]):
        _safe_addstr(stdscr, by + 1 + i, bx + 2, ln[: bw - 4], curses.A_REVERSE)

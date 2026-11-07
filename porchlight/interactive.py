"""`porchlight -i`: the dashboard you can move around in and act from.

Left: the panels (alerts first), each with its level. Right: the selected panel at the
pane's full width, then its items, the rows you can act on. Enter opens an item's
detail; keys act on it: `d` marks a todo done (after y/N), `o` opens its link, `a`
jumps to a Drover session's tmux pane, `D` opens Drover's own TUI, `A` ai-cli's TUI
(both configurable under [interactive] programs), `s` searches the brain and
reflections through Drover, `R` refreshes the panel's source now.

Nothing here runs at login: the plain dashboard never imports this module. It reads
the same cache, with the same live probes, so a frame costs what a login render does;
it redraws on a key, on a resize and every few seconds. Stdlib only: termios for raw
keys, ANSI for drawing (the panels are already ANSI strings, which curses cannot take).
"""
from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import time

from . import alerts as A, core, registry, theme as T
from .ui import Panel, kv

REFRESH = 5.0
# Full-screen programs a key runs (porchlight leaves the screen, runs it, comes back).
# [interactive] programs adds keys or overrides these; an empty list turns one off.
PROGRAMS = {"D": ["drover", "tui", "--view", "board"], "A": ["ai", "-t"]}
HELP = [
    ("↑↓ / j k", "move"),
    ("→ ⏎ / l", "into a panel's items, then open one"),
    ("← ⎋ / h", "back"),
    ("tab", "next panel"),
    ("d", "mark a todo done (asks y/N; needs [sources.todos] actions = true)"),
    ("o", "open the item's link"),
    ("a", "jump to a Drover session's tmux pane"),
    ("D", "Drover's own TUI"),
    ("A", "ai-cli's TUI (ask every model side by side)"),
    ("s", "search the brain and reflections (through Drover)"),
    ("/", "filter the items"),
    ("R", "refresh this panel's source now"),
    ("r", "redraw from the cache"),
    ("q", "quit"),
]


def accent():
    """The theme's accent as one color (T.ACCENT is the gradient the panel titles use)."""
    return T.gradient(T.ACCENT, 0.15)


# ── terminal ──────────────────────────────────────────────────────────────────
class Term:
    """Raw keys on the alternate screen; everything restored on exit, even on an exception."""

    def __init__(self, fd_in=None, out=None):
        self.fd = sys.stdin.fileno() if fd_in is None else fd_in
        self.out = out or sys.stdout
        self.saved = None

    def __enter__(self):
        import termios
        import tty
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self.out.write("\033[?1049h\033[?25l")
        self.out.flush()
        return self

    def __exit__(self, *exc):
        self.restore()

    def restore(self):
        import termios
        self.out.write("\033[?25h\033[?1049l")
        self.out.flush()
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def resume(self):
        import tty
        tty.setcbreak(self.fd)
        self.out.write("\033[?1049h\033[?25l")
        self.out.flush()

    def key(self, timeout: float):
        """One key name ('up', 'enter', 'esc', 'x', ...) or None after `timeout`."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        b = os.read(self.fd, 1)
        if b == b"\x1b":
            seq = b""
            while select.select([self.fd], [], [], 0.03)[0]:
                seq += os.read(self.fd, 1)
                if len(seq) >= 2 and (seq[-1:].isalpha() or seq[-1:] == b"~"):
                    break
            return parse_key(b"\x1b" + seq)
        return parse_key(b)


KEYS = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left", b"\x1bOA": "up",
        b"\x1bOB": "down", b"\x1bOC": "right", b"\x1bOD": "left", b"\x1b[5~": "pgup", b"\x1b[6~": "pgdn",
        b"\x1b[H": "home", b"\x1b[F": "end", b"\x1b": "esc", b"\r": "enter", b"\n": "enter", b"\t": "tab",
        b"\x1b[Z": "btab", b"\x7f": "backspace", b"\x08": "backspace", b"\x03": "ctrl-c", b"\x04": "ctrl-d"}


def parse_key(b: bytes) -> str:
    if b in KEYS:
        return KEYS[b]
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return ""


# ── model ────────────────────────────────────────────────────────────────────
def alerts_panel(al) -> Panel:
    """The ranked alerts as a panel whose items carry each alert's fix."""
    rows, items = [], []
    for a in al:
        gl, col = A.glyph(a["sev"])
        line = T.c(gl, col) + " " + a["msg"]
        rows.append(line)
        det = [line, "", kv("source", a.get("src") or "?", 9)]
        if a.get("fix"):
            det.append(kv("fix", T.c(a["fix"], "slate"), 9))
        items.append({"key": "alert:%d" % len(items), "text": line, "detail": det, "actions": [], "source": a.get("src")})
    if not rows:
        rows = [T.c(T.g("ok") + " no alerts", "green")]
    lvl = "crit" if any(a["sev"] == "crit" for a in al) else ("warn" if any(a["sev"] == "warn" for a in al) else "ok")
    return Panel("alerts", "ALERTS", rows, lvl, "%d" % len(al) if al else "", items)


def load(host, width: int):
    """(panels, alerts) as the dashboard would build them, for a right pane `width` wide."""
    from . import panels as P, render
    from .ui import RCtx
    cache = registry.Cache()
    lv = A.probe_all(host, repos=list(host.repos))
    al = A.gather(host, cache, lv)
    ctx = RCtx(host, cache, lv, al, width, None)
    keys = render.panel_keys(host, cache)
    extra = P.custom_keys(host) + [k for k in P.BUILDERS if k not in keys]
    built = render.build_panels(ctx, keys + [k for k in extra if k not in keys])
    return [alerts_panel(al)] + built, al, cache


class App:
    def __init__(self, host, term=None, opener=None, runner=None):
        self.host = host
        self.term = term
        self.opener = opener or open_url
        self.runner = runner or subprocess.call
        self.panels, self.al, self.cache = [], [], None
        self.pi = 0            # selected panel
        self.ii = 0            # selected item
        self.focus = "panels"  # panels | items | detail | help | search
        self.scroll = 0
        self.msg = ""
        self.confirm = None    # (prompt, callable) waiting for y/N
        self.filter = ""
        self.typing = None     # ("filter" | "search", text so far)
        self.search_rows = []
        self.done = set()      # keys acted on this session (hidden until the cache catches up)
        self.loaded = 0.0
        self.quit = False
        self.dirty = True
        self.refresh = float(host.cfg.get("interactive.refresh", REFRESH) or REFRESH)
        self.programs = programs(host)

    # geometry
    def size(self):
        s = shutil.get_terminal_size((100, 30))
        return max(40, s.columns), max(10, s.lines)

    def widths(self):
        W, _ = self.size()
        left = max(14, min(24, W // 5))
        return left, W - left - 3

    def reload(self):
        _, right = self.widths()
        try:
            self.panels, self.al, self.cache = load(self.host, right)
        except Exception as e:  # noqa: BLE001 — keep the old frame and say why
            self.msg = T.c("reload failed: %s: %s" % (type(e).__name__, e), "red")
        self.pi = min(self.pi, max(0, len(self.panels) - 1))
        self.loaded = time.monotonic()

    # selection
    def panel(self):
        return self.panels[self.pi] if self.panels else None

    def items(self):
        p = self.panel()
        its = [i for i in (p.items if p else []) if i.get("key") not in self.done]
        if self.filter:
            f = self.filter.lower()
            its = [i for i in its if f in T.strip(str(i.get("text", ""))).lower()]
        return its

    def item(self):
        its = self.items()
        return its[self.ii] if 0 <= self.ii < len(its) else None

    # drawing
    def frame(self) -> str:
        W, H = self.size()
        left_w, right_w = self.widths()
        body_h = H - 2
        left = self.left_lines(left_w, body_h)
        right = self.right_lines(right_w, body_h)
        out = [self.top_line(W)]
        for n in range(body_h):
            lt = left[n] if n < len(left) else ""
            rt = right[n] if n < len(right) else ""
            out.append(T.pad(T.fit(lt, left_w), left_w) + " " + T.dim("│") + " " + T.fit(rt, right_w))
        out.append(self.bottom_line(W))
        return "\033[H" + "\033[K\n".join(out) + "\033[K\033[J"

    def top_line(self, W) -> str:
        nc = sum(1 for a in self.al if a["sev"] == "crit")
        nw = sum(1 for a in self.al if a["sev"] == "warn")
        parts = [T.c("porchlight", accent(), bold=True), T.c(self.host.label, "fg", bold=True)]
        if nc:
            parts.append(T.c("%s%d" % (T.g("crit"), nc), "red"))
        if nw:
            parts.append(T.c("%s%d" % (T.g("warn"), nw), "amber"))
        b = (self.cache.bundle if self.cache else None) or {}
        if b.get("ts") and not self.host.hub:
            parts.append(T.dim("fleet view %s · %s" % (b.get("label") or b.get("from"), core.human_age(core.now() - b["ts"]))))
        parts.append(T.dim(time.strftime("%H:%M")))
        return " " + T.fit(T.dim(" · ").join(parts), W - 1)

    def bottom_line(self, W) -> str:
        if self.confirm:
            return " " + T.fit(T.c(self.confirm[0] + " [y/N]", "amber", bold=True), W - 1)
        if self.typing:
            label = "filter" if self.typing[0] == "filter" else "search brain + reflections"
            return " " + T.fit(T.c(label + ": ", "sky") + self.typing[1] + "▏", W - 1)
        if self.msg:
            return " " + T.fit(self.msg, W - 1)
        it = self.item() if self.focus in ("items", "detail") else None
        acts = (it or {}).get("actions") or []
        keys = [k for k, a in (("d", "done"), ("o", "open"), ("a", "attach")) if a in acts]
        hint = "  ".join("%s %s" % (T.c(k, "sky"), {"d": "done", "o": "open", "a": "attach"}[k]) for k in keys)
        base = T.dim("↑↓ move · ⏎ open · ← back · / filter · s search · ? help · q quit")
        return " " + T.fit((hint + "   " if hint else "") + base, W - 1)

    def left_lines(self, w, h) -> list:
        out = []
        for n, p in enumerate(self.panels):
            mark = T.dot(p.level) if p.level in ("warn", "crit") else T.dim(T.g("dot") if p.items else " ")
            name = T.strip(p.title)
            count = len([i for i in p.items if i.get("key") not in self.done])
            label = name + (T.dim(" %d" % count) if count else "")
            line = mark + " " + label
            if n == self.pi:
                line = T.c("▶", accent(), bold=True) + " " + T.c(name, "fg", bold=True) + (T.dim(" %d" % count) if count else "")
            out.append(line)
        start = max(0, self.pi - h + 2) if self.pi >= h - 1 else 0
        return out[start:start + h]

    def right_lines(self, w, h) -> list:
        if self.focus == "help":
            return [T.c("keys", "fg", bold=True), ""] + [kv(k, d, 12) for k, d in HELP]
        if self.focus == "search":
            return self.search_rows or [T.dim("no results")]
        p = self.panel()
        if not p:
            return [T.dim("no panels have data yet: porchlight collect")]
        if self.focus == "detail":
            it = self.item()
            if not it:
                self.focus = "items"
            else:
                det = list(it.get("detail") or [it.get("text", "")])
                acts = it.get("actions") or []
                if acts:
                    det += ["", T.dim("actions: ") + "  ".join(T.c(a, "sky") for a in acts)]
                return det[self.scroll:self.scroll + h]
        lines = p.render(w, T.ACCENT)
        its = self.items()
        if not its:
            return lines[self.scroll:self.scroll + h]
        if p.key == "alerts":
            lines = lines[:1]  # its rows are its items
        head = lines + ["", T.dim("%d item%s%s · ⏎ to open" % (len(its), "" if len(its) == 1 else "s",
                                                                (" matching '%s'" % self.filter) if self.filter else ""))]
        room = max(3, h - len(head))
        if len(head) > h - 3:  # a tall panel: keep its title and give the items the room
            head = head[:1] + ["", head[-1]]
            room = h - len(head)
        start = max(0, min(self.ii - room // 2, len(its) - room))
        rows = []
        for n, it in enumerate(its[start:start + room], start):
            text = str(it.get("text") or "")
            if n == self.ii and self.focus == "items":
                rows.append(T.c("▶ ", accent(), bold=True) + T.fit(text, w - 2))
            else:
                rows.append("  " + T.fit(text, w - 2))
        return head + rows

    # input
    def handle(self, k):
        if k is None:
            return
        if self.confirm:
            prompt, fn = self.confirm
            self.confirm = None
            self.msg = fn() if k in ("y", "Y") else T.dim("unchanged")
            return
        if self.typing:
            kind, text = self.typing
            if k == "enter":
                self.typing = None
                if kind == "filter":
                    self.filter, self.ii = text, 0
                else:
                    self.run_search(text)
            elif k in ("esc", "ctrl-c"):
                self.typing = None
            elif k == "backspace":
                self.typing = (kind, text[:-1])
            elif len(k) == 1 and k.isprintable():
                self.typing = (kind, text + k)
            return
        self.msg = ""
        if k in ("q", "ctrl-c", "ctrl-d"):
            self.quit = True
        elif k == "?":
            self.focus = "panels" if self.focus == "help" else "help"
        elif k in ("up", "k"):
            self.move(-1)
        elif k in ("down", "j"):
            self.move(1)
        elif k in ("pgup",):
            self.move(-10)
        elif k in ("pgdn", " "):
            self.move(10)
        elif k in ("tab", "btab"):
            self.pi = (self.pi + (1 if k == "tab" else -1)) % max(1, len(self.panels))
            self.focus, self.ii, self.scroll, self.filter = "panels", 0, 0, ""
        elif k in ("right", "l", "enter"):
            self.forward()
        elif k in ("left", "h", "esc", "backspace"):
            self.back()
        elif k == "/":
            self.typing = ("filter", self.filter)
        elif k == "s":
            self.typing = ("search", "")
        elif k == "r":
            self.reload()
            self.msg = T.dim("redrawn from the cache")
        elif k == "R":
            self.refresh_source()
        elif k in self.programs:
            argv = self.programs[k]
            self.external(argv, os.path.basename(argv[0]))
        elif k == "d":
            self.act_done()
        elif k == "o":
            self.act_open()
        elif k == "a":
            self.act_attach()

    def move(self, n):
        if self.focus == "panels":
            self.pi = max(0, min(len(self.panels) - 1, self.pi + n))
            self.ii, self.scroll, self.filter = 0, 0, ""
        elif self.focus == "items":
            self.ii = max(0, min(len(self.items()) - 1, self.ii + n))
        else:
            self.scroll = max(0, self.scroll + n)

    def forward(self):
        if self.focus == "panels":
            if self.items():
                self.focus, self.ii = "items", 0
            else:
                self.scroll = 0
        elif self.focus == "items" and self.item():
            self.focus, self.scroll = "detail", 0

    def back(self):
        if self.focus == "detail":
            self.focus, self.scroll = "items", 0
        elif self.focus in ("items", "help", "search"):
            self.focus, self.scroll = "panels", 0
        elif self.filter:
            self.filter = ""

    # actions
    def current(self):
        return self.item() if self.focus in ("items", "detail") else None

    def act_done(self):
        it = self.current()
        if not it or "done" not in (it.get("actions") or []):
            self.msg = T.dim("nothing to mark done here")
            return
        raw = it.get("raw") or {}
        from .sources import todos as S

        from . import todo_cmd
        via_hub = todo_cmd.via_hub(self.host, self.cache)

        def go():
            if via_hub:  # a peer showing the hub's list: the hub holds the credentials, so it acts
                rc, out, err = core.run(todo_cmd.hub_argv(via_hub, raw.get("key", "")), timeout=40)
                if rc != 0:
                    lines = T.strip(err or out or "").strip().splitlines()
                    why = lines[-1] if lines else "exit %d" % rc
                    return T.c("%s on %s: %s" % (T.g("crit"), via_hub, S.clean(why, 120)), "red")
                what = "done on %s" % via_hub
            else:
                try:
                    what = S.act(self.host, raw, "done")
                except S.ActionError as e:
                    return T.c("%s %s" % (T.g("crit"), e), "red")
            from . import collect
            todo_cmd.forget(raw.get("key", ""))
            self.done.add(it.get("key"))
            self.ii = max(0, min(self.ii, len(self.items()) - 1))
            if self.focus == "detail":
                self.focus = "items"
            collect.spawn_background(self.host, only=["todos"])
            return T.c("%s %s" % (T.g("ok"), what), "green")
        where = raw.get("provider") + (" via %s" % via_hub if via_hub else "")
        self.confirm = ("mark done in %s: %s?" % (where, T.fit(T.strip(str(it.get("text"))), 60)), go)

    def act_open(self):
        it = self.current()
        url = (it or {}).get("url") or ""
        if not url:
            self.msg = T.dim("no link on this item")
            return
        self.msg = self.opener(url)

    def act_attach(self):
        it = self.current()
        raw = (it or {}).get("raw") or {}
        if not raw.get("id"):
            self.msg = T.dim("not a Drover session")
            return
        argv, why = attach_argv(self.host, raw)
        if not argv:
            self.msg = T.c(why, "amber")
            return
        self.external(argv, "tmux")

    def external(self, argv, what):
        """Leave the screen, run a foreground program, come back."""
        if not core.which(argv[0]):
            self.msg = T.c("%s is not installed here" % argv[0], "amber")
            return
        if self.term:
            self.term.restore()
        try:
            rc = self.runner(argv)
        except OSError as e:
            rc = None
            self.msg = T.c("%s: %s" % (what, e), "red")
        finally:
            if self.term:
                self.term.resume()
        if rc not in (0, None):
            self.msg = T.c("%s exited %s" % (what, rc), "amber")
        self.reload()

    def refresh_source(self):
        p = self.panel()
        if not p or p.key == "alerts":
            return
        name = p.key if p.key in registry.SOURCES else None
        if not name:
            self.msg = T.dim("this panel reads several sources: porchlight collect --force")
            return
        from . import collect
        ok = collect.spawn_background(self.host, only=[name])
        self.msg = T.dim("refreshing %s in the background" % name) if ok else T.c("a collector is already running", "amber")

    def run_search(self, query):
        query = query.strip()
        if not query:
            return
        self.search_rows = [T.c("search: " + query, "fg", bold=True), ""] + search(self.host, query)
        self.focus, self.scroll = "search", 0

    # loop
    def run(self) -> int:
        """Draw only when something changed: a key, a resize, a reload (every `refresh` s)."""
        self.reload()
        last = None
        while not self.quit:
            size = self.size()
            if self.dirty or size != last:
                sys.stdout.write(self.frame())
                sys.stdout.flush()
                last, self.dirty = size, False
            k = self.term.key(0.5) if self.term else None
            if k is None:
                if time.monotonic() - self.loaded > self.refresh and not (self.confirm or self.typing):
                    self.reload()
                    self.dirty = True
                continue
            self.handle(k)
            self.dirty = True
        return 0


# ── helpers with side effects ────────────────────────────────────────────────
def programs(host) -> dict:
    """The full-screen programs by key: the built-ins, then [interactive] programs over them.
    Keys the dashboard already uses for itself cannot be taken."""
    out = {k: list(v) for k, v in PROGRAMS.items()}
    own = host.cfg.get("interactive.programs", {}) or {}
    for k, v in (own.items() if isinstance(own, dict) else []):
        if not isinstance(k, str) or len(k) != 1 or k in RESERVED:
            continue
        if isinstance(v, str):
            v = v.split()
        if isinstance(v, list) and all(isinstance(a, str) for a in v):
            if v:
                out[k] = list(v)
            else:
                out.pop(k, None)
    return out


RESERVED = set("qjklhdoasrR/?") | {" "}
def open_url(url: str) -> str:
    """Open a link in the browser where one exists; over ssh, say the link instead."""
    if os.environ.get("SSH_CONNECTION") and not os.environ.get("DISPLAY"):
        return T.c("link: ", "sky") + url
    opener = "open" if core.IS_MAC else ("wslview" if core.IS_WSL and core.which("wslview") else "xdg-open")
    if not core.which(opener):
        return T.c("link: ", "sky") + url
    try:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return T.dim("opened ") + url
    except OSError:
        return T.c("link: ", "sky") + url


def attach_argv(host, raw, http=None):
    """(argv, why): the command that puts you in the session's tmux pane. Inside tmux on the
    same machine, switch the client; otherwise attach (over ssh for another host)."""
    from .registry import Ctx
    from .sources import drover as D
    env = os.environ.copy()
    url, main, _w = D.find_daemon(host.cfg.source("drover", {}), env)
    if not url or not main:
        return None, "no Drover daemon (or token) on this machine"
    ctx = Ctx(host, env, {})
    if http:
        ctx.http = http
    where = str(raw.get("host") or "local")
    args = {} if where == "local" else {"host": where}
    res, why = D.op(ctx, url, main, "sessions.list", args, timeout=8)
    if res is None:
        return None, "sessions.list: %s" % (why or "no answer")
    rows = res if isinstance(res, list) else (res.get("sessions") or res.get("items") or []) if isinstance(res, dict) else []
    hit = next((s for s in rows if isinstance(s, dict) and raw.get("id") in (s.get("id"), s.get("sessionId"))), None)
    target = str((hit or {}).get("tmux") or "")
    if not target:
        return None, "that session has no tmux pane (headless?): D opens Drover"
    win = target.rsplit(".", 1)[0] if "." in target.split(":", 1)[-1] else target
    if where == "local":
        if os.environ.get("TMUX"):
            return ["tmux", "switch-client", "-t", win], None
        return ["tmux", "attach", "-t", win], None
    return ["ssh", "-t", where, "tmux", "attach", "-t", win], None


def search(host, query: str, http=None) -> list:
    """Brain and reflections hits for `query`, through this machine's Drover daemon."""
    from .registry import Ctx
    from .sources import drover as D
    env = os.environ.copy()
    url, main, _w = D.find_daemon(host.cfg.source("drover", {}), env)
    if not url or not main:
        return [T.c("no Drover daemon (or token) here: search goes through Drover", "amber")]
    ctx = Ctx(host, env, {})
    if http:
        ctx.http = http
    from .sources import todos as S
    tcfg = host.cfg.source("todos", S.DEFAULTS)

    def excluded(r):  # the todos source's exclude_repos / exclude_paths hold for search hits too
        from .sources.knowledge import repo_excluded
        return repo_excluded(r.get("repo"), tcfg.get("exclude_repos")) or S._excluded_path(tcfg, r)

    out = []
    for op, args, label in (("brain.search", {"query": query, "limit": 8}, "BRAIN"),
                            ("reflections.search", {"query": query, "limit": 8}, "REFLECTIONS")):
        res, why = D.op(ctx, url, main, op, args, timeout=10)
        out.append(T.c(label, "sky", bold=True))
        rows = res if isinstance(res, list) else (((res or {}).get("hits") or (res or {}).get("items") or
                                                   (res or {}).get("results") or []) if isinstance(res, (dict, type(None))) else [])
        if res is None:
            out.append(T.dim("  %s" % (why or "no answer")))
        for r in rows[:8]:
            if not isinstance(r, dict) or excluded(r):
                continue
            title = D.text(r.get("title") or r.get("name") or r.get("snippet") or r.get("id"), 160)
            meta = " ".join(D.text(r.get(k), 30) for k in ("kind", "repo", "status", "namespace") if r.get(k))
            when = D.text(r.get("validFrom") or r.get("updatedAt") or r.get("ts") or "", 10)
            meta = (meta + " " + when).strip()
            out.append("  " + title + (T.dim("  " + meta) if meta else ""))
            snip = D.text(r.get("snippet") or r.get("body") or r.get("content") or "", 200)
            if snip and snip != title:
                out.append("    " + T.dim(snip))
        if res is not None and not rows:
            out.append(T.dim("  nothing found"))
        out.append("")
    return out


def run(host) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("porchlight: -i needs a terminal (try porchlight, or porchlight watch)", file=sys.stderr)
        return 2
    with Term() as term:
        return App(host, term).run()

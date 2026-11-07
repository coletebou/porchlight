"""Panels and the render context: the API panel builders (and plugins) use.

A builder takes an RCtx and returns a Panel, or None when it has nothing to
show. Builders read caches through x.cache and never touch the network.
"""
from __future__ import annotations

from . import core, registry, theme as T

BUILDERS = {}
TITLES = {}


def panel(key, title=None):
    """Register a panel builder: @panel("weather", title="WEATHER")."""
    def deco(fn):
        BUILDERS[key] = fn
        TITLES[key] = title or key.upper()
        return fn
    return deco


class Action:
    """Something the interactive dashboard (porchlight -i) can do with an item.

    name    a handler registered in porchlight.actions (@action("todos.done")); the
            built-ins are "open_url", "copy" and "run"
    label   what the key hint says ("mark done")
    key     one character, unique within the item ("d")
    args    JSON-able arguments for the handler; never a credential (a handler loads
            its own from the secrets files when it runs)
    writes  True when the handler changes state somewhere (an API write, a message):
            refused unless [interactive] allow_writes = true, and always confirmed
    confirm the y/N question asked before a write ("Mark 'renew cert' done in Badland?")

    Two args the TUI itself interprets before it calls the handler:
    args["input"]  a prompt string: porchlight -i first asks for one line of text and passes
                   it to the handler as args["text"]; an empty answer cancels the action
    args["pause"]  True on a "run" action: after the program exits, wait for Enter before
                   redrawing (for programs that print and exit, like a transcript)
    """

    def __init__(self, name, label, key, args=None, writes=False, confirm=""):
        self.name, self.label, self.key = str(name), str(label), str(key)[:1]
        self.args = dict(args or {})
        self.writes, self.confirm = bool(writes), str(confirm or "")

    def to_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "key": self.key, "args": self.args,
                "writes": self.writes, "confirm": self.confirm}


class Item:
    """One selectable row behind a panel, for porchlight -i and `porchlight view`.

    id      stable across renders ("todos:badland:69bc3f..."), so a selection survives a redraw
    text    one plain line (no ANSI): what the item list shows
    detail  plain text lines for the detail pane (may be empty)
    level   "ok" | "info" | "warn" | "crit"
    url     an optional link (the TUI offers "open" for it)
    actions Action list; "open_url" for url is added by the TUI itself
    """

    def __init__(self, id, text, detail=None, level="ok", url="", actions=None):
        self.id, self.text = str(id), str(text)
        self.detail = [str(d) for d in (detail or [])]
        self.level, self.url = str(level), str(url or "")
        self.actions = list(actions or [])

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "detail": self.detail, "level": self.level, "url": self.url,
                "actions": [a.to_dict() for a in self.actions]}


class Panel:
    def __init__(self, key, title, lines, level="ok", note="", items=None):
        self.key, self.title, self.lines, self.level, self.note = key, title, lines, level, note
        self.items = list(items or [])  # Item list for porchlight -i and `porchlight view`; the login render ignores it

    def render(self, cw: int, accent) -> list:
        title = T.c(self.title, T.gradient(accent, 0.15), bold=True)
        note = T.dim(self.note) if self.note else ""
        mark = (" " + T.dot(self.level)) if self.level in ("warn", "crit") else ""
        rule_w = cw - T.width(title) - T.width(mark) - T.width(note) - (3 if note else 2)
        head = " " + title + mark + " " + T.rule(max(2, rule_w)) + ((" " + note) if note else "")
        return [head] + [" " + T.fit(line, cw - 1) for line in self.lines]


class RCtx:
    """Everything a panel builder needs: host, cache, live probes, alerts, column width,
    and the previous login's time (last_seen; None before the first login) for "since
    your last visit"."""

    def __init__(self, host, cache, lv, alerts, cw, last_seen=None):
        self.host, self.cache, self.lv, self.alerts, self.cw = host, cache, lv, alerts, cw
        self.v = lv["vitals"]
        self.cfg = host.cfg
        self.last_seen = last_seen

    def data(self, name, default=None):
        return self.cache.data(name, default)

    def stale_note(self, name, ttl_mult=4, floor=1800) -> str:
        """'12m' when a source is older than expected, '✖ 3h' when its last run failed."""
        rec = self.cache.rec(name)
        if not rec:
            return "no data"
        age = core.now() - rec.get("ts", 0)
        if not rec.get("ok") and not rec.get("skip"):
            since = core.now() - (rec.get("last_ok") or rec.get("ts", 0))
            return T.g("crit") + " " + core.human_age(since)
        src = registry.SOURCES.get(name)
        ttl = src.ttl if src else 600
        return core.human_age(age) if age > max(ttl * ttl_mult, floor) else ""

    def panel(self, key, rows, level="ok", note="", title=None, items=None):
        return Panel(key, title or TITLES.get(key) or key.upper(), rows, level, note, items) if rows else None


class _Sep:
    """The dim " · " between items, resolved when used (theme, color depth and
    ASCII mode are settled after import, at render start)."""

    def join(self, items):
        return T.sep().join(items)

    def __str__(self):
        return T.sep()

    def __add__(self, other):
        return T.sep() + other

    def __radd__(self, other):
        return other + T.sep()


SEP = _Sep()


def kv(label: str, value: str, w: int = 9) -> str:
    return T.dim(label.ljust(w)) + value


def cells(items, cw, minw=14) -> list:
    """Lay text cells out in as many columns as fit."""
    if not items:
        return []
    w = min(cw, max(minw, max(T.width(i) for i in items) + 2))
    per = max(1, cw // w)
    return ["".join(T.pad(i, w) for i in items[r:r + per]).rstrip() for r in range(0, len(items), per)]


def fitted(items, room: int, sep=None, more=None) -> str:
    """As many items as fit in `room` cells, joined by `sep` (default SEP), then "+N"
    (or more(N)) for the rest: a row that never cuts an item, or a word, in half."""
    sep = SEP if sep is None else sep
    more = more or (lambda n: T.dim("+%d" % n))
    items = [i for i in items if i]
    out = []
    for n, it in enumerate(items):
        rest = len(items) - n - 1
        tail = T.width(str(sep) + more(rest)) if rest else 0
        if T.width(sep.join(out + [it])) + tail > room:
            return sep.join(out + [more(len(items) - n)])
        out.append(it)
    return sep.join(out)


def wrap(text: str, widths) -> list:
    """Words laid onto lines of the given widths, one line per width at most, never
    cutting a word: text that does not fit (a word wider than its line included) ends
    the last line with an ellipsis after the last whole word. [] when not even the
    first word fits with its ellipsis."""
    words, lines, i = str(text or "").split(), [], 0
    widths = list(widths)
    for w in widths:
        if i >= len(words) or T.width(words[i]) > w:
            break
        line = words[i]
        i += 1
        while i < len(words) and T.width(line + " " + words[i]) <= w:
            line += " " + words[i]
            i += 1
        lines.append(line)
    mark = T.ellipsis()
    while i < len(words) and lines:  # more text than room: the last line says so
        cut = lines[-1].split(" ")
        while cut and T.width(" ".join(cut) + mark) > widths[len(lines) - 1]:
            cut.pop()
        if cut:
            lines[-1] = " ".join(cut) + mark
            break
        lines.pop()  # a lone word with no room left for the mark: end on the line before
    return lines


def fnum(x: float) -> str:
    if x < 10:
        return "%.2f" % x
    if x < 100:
        return "%.1f" % x
    return "%.0f" % x


def ago(t, suffix=" ago") -> str:
    return core.human_age(core.now() - t, suffix) if t else "?"


def until(t) -> str:
    """'↻3h06m' for a reset under a day away, else '↻Tue 02:36'."""
    import time
    if not t:
        return ""
    left = t - core.now()
    if left <= 0:
        return "now"
    return (T.g("reset") + core.human_age(left)) if left < 86400 else time.strftime(T.g("reset") + "%a %H:%M", time.localtime(t))

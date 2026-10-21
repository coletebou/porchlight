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


class Panel:
    def __init__(self, key, title, lines, level="ok", note=""):
        self.key, self.title, self.lines, self.level, self.note = key, title, lines, level, note

    def render(self, cw: int, accent) -> list:
        title = T.c(self.title, T.gradient(accent, 0.15), bold=True)
        note = T.dim(self.note) if self.note else ""
        mark = (" " + T.dot(self.level)) if self.level in ("warn", "crit") else ""
        rule_w = cw - T.width(title) - T.width(mark) - T.width(note) - (3 if note else 2)
        head = " " + title + mark + " " + T.rule(max(2, rule_w)) + ((" " + note) if note else "")
        return [head] + [" " + T.fit(line, cw - 1) for line in self.lines]


class RCtx:
    """Everything a panel builder needs: host, cache, live probes, alerts, column width."""

    def __init__(self, host, cache, lv, alerts, cw):
        self.host, self.cache, self.lv, self.alerts, self.cw = host, cache, lv, alerts, cw
        self.v = lv["vitals"]
        self.cfg = host.cfg

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

    def panel(self, key, rows, level="ok", note="", title=None):
        return Panel(key, title or TITLES.get(key) or key.upper(), rows, level, note) if rows else None


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

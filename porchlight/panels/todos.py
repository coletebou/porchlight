"""The TODOS panel: open items from every tracker the `todos` source reads.

One count row (per provider), the items that matter most (overdue first, then by due
date, priority and recency), the ask-coverage row when ask tracking is on, and one row
per provider that failed. The interactive dashboard gets every item (not just the ones
that fit) with its actions.
"""
from __future__ import annotations

from .. import core, theme as T
from ..ui import fitted, kv, panel

W = 9
SHOWN = 6
SHORT = {"files": "file", "github": "gh", "drover": "drover", "reflections": "refl", "badland": "badland",
         "command": "cmd"}


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []


def _s(v, n=160) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _int(v, default=0) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else default


def _due(t) -> tuple:
    """('3d', color) for a due date, ('', '') without one."""
    if not isinstance(t, (int, float)) or not t:
        return "", ""
    s = t - core.now()
    if s < 0:
        return "-" + core.human_age(-s), "red"
    return core.human_age(s), ("amber" if s < 2 * 86400 else "slate")


def where(it) -> str:
    """'gh coletebou/porchlight#12', 'drover dotfiles', 'refl badland'."""
    prov = SHORT.get(_s(it.get("provider")), _s(it.get("provider"), 8))
    if it.get("provider") == "github":
        return "%s %s" % (prov, _s(it.get("id"), 60))
    repo = _s(it.get("repo"), 30)
    return prov + (" " + repo if repo else "")


def row(it, room: int) -> str:
    due, dcol = _due(it.get("due"))
    glyph = T.c("!", "red", bold=True) if dcol == "red" else (T.c(T.g("dot"), "amber") if _int(it.get("prio"), 5) <= 2
                                                              else T.dim(T.g("dot")))
    tail = T.dim(where(it)) + ((" " + T.c(due, dcol)) if due else "")
    left = room - 2 - T.width(tail) - 1
    title = _s(it.get("title"))
    if left < 12:
        return glyph + " " + T.fit(title, room - 2)
    return glyph + " " + T.pad(T.fit(title, left), left) + " " + tail


def detail(it) -> list:
    """Every field worth reading, for the interactive dashboard's detail view."""
    out = [T.c(_s(it.get("title"), 400), "fg", bold=True), ""]
    for label, val in (("where", where(it)), ("key", _s(it.get("key"), 120)), ("status", _s(it.get("status"))),
                       ("kind", _s(it.get("kind"))), ("priority", _s(it.get("prio"))),
                       ("due", (time_str(it.get("due")) + "  (" + _due(it.get("due"))[0] + ")") if it.get("due") else ""),
                       ("updated", time_str(it.get("updated"))), ("asks", " ".join(it.get("asks") or [])),
                       ("labels", " ".join(_s(x, 30) for x in it.get("labels") or [])), ("url", _s(it.get("url"), 300))):
        if val:
            out.append(kv(label, val, W))
    return out


def time_str(t) -> str:
    if not isinstance(t, (int, float)) or not t:
        return ""
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def tui_items(items) -> list:
    out = []
    for it in items:
        # A pull request is never "done" from here: closing one unmerged is not finishing it.
        acts = [] if (it.get("provider") == "github" and it.get("kind") == "pr") else ["done"]
        if it.get("url"):
            acts.append("open")
        out.append({"key": it.get("key"), "text": _s(it.get("title")), "detail": detail(it), "url": it.get("url") or "",
                    "actions": acts, "source": "todos", "raw": it})
    return out


@panel("todos", title="TODOS")
def p_todos(x):
    d = _d(x.data("todos"))
    if not d:
        return None
    items, provs = _l(d.get("items")), _d(d.get("providers"))
    if d.get("error") and not items:
        return x.panel("todos", [T.dim(_s(d.get("error")))], note=x.stale_note("todos"))
    room = x.cw - 1
    counts = [T.c(str(len(items)), "fg", bold=True) + T.dim(" open")]
    counts += ["%s %s" % (T.dim(SHORT.get(n, n)), _s(_d(p).get("n"))) for n, p in provs.items()]
    rows = [fitted(counts, room)]
    overdue = _int(d.get("overdue"))
    shown = int(x.cfg.get("sources.todos.shown", SHOWN) or SHOWN)
    rows += [row(it, room) for it in items[:shown]]
    if len(items) > shown:
        more = "  +%d more" % (len(items) - shown)
        rows.append(T.dim(more + " · porchlight todos" if T.width(more) + 19 <= room else more))
    asks = _d(d.get("asks"))
    if asks:
        gaps = [(a, [str(m) for m in _d(v).get("missing") or []]) for a, v in asks.items()]
        gaps = [(a, m) for a, m in gaps if m]
        full = len(asks) - len(gaps)
        head = [T.c(str(len(asks)), "fg", bold=True) + T.dim(" tracked"), T.c(str(full), "green") + T.dim(" everywhere")]
        if gaps:
            head.append(T.c("%d gap%s" % (len(gaps), "" if len(gaps) == 1 else "s"), "amber"))
        rows.append(kv("asks", fitted(head, room - W), W))
        if gaps:
            cells_ = ["%s %s" % (a, T.dim("-" + ",".join(SHORT.get(m) or m for m in miss))) for a, miss in gaps]
            rows.append(" " * W + fitted(cells_, room - W))
    for n, p in provs.items():
        p = _d(p)
        if not p.get("ok"):
            kept = " (kept)" if p.get("kept") else ""
            why = T.fit("%s: %s" % (SHORT.get(n, n), _s(p.get("error"), 80)), max(4, room - 2 - len(kept)))
            rows.append(T.c(T.g("crit"), "red") + " " + T.dim(why + kept))
    level = "warn" if overdue else "ok"
    note = x.stale_note("todos", ttl_mult=6) or ("%d overdue" % overdue if overdue else "")
    return x.panel("todos", rows, level=level, note=note, items=tui_items(items))

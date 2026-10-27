"""The DROVER panel: the agent sessions this machine's Drover daemon sees, on every host.

Who waits on you and what they ask, what finished since your last visit and what
crashed, who works where, and (on the fleet hub) the commit each daemon runs and the
credential pool. Everything comes from the `drover` record; nothing here touches the
network. Rows are built to fit the column: a list that runs out of room says how many
it left out ("+3"), and a question wraps onto a second line between words.
"""
from __future__ import annotations

from .. import core, theme as T
from ..ui import SEP, Panel, fitted, kv, panel, wrap

W = 9                 # the label column
SHOWN_WAITING = 3
SHOWN_DONE = 3
SHOWN_CRASHED = 2
SHOWN_HOSTS = 3
CRASH_RECENT = 6 * 3600


# ── Typed reads: a record kept from another version may hold anything ───────
def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []


def _s(v, n: int = 40) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _n(v) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _host(x, h) -> str:
    """Drover's host name as this dashboard says it: "local" is the machine whose daemon
    answered (this one, unless the record came from the hub), and [fleet] labels
    shorten the others."""
    h = _s(h) or "?"
    if h == "local":
        return _s(_d(x.data("drover")).get("here")) or x.host.label
    label = x.cfg.table("fleet.labels").get(h)
    return _s(label) or h


def _age(t) -> str:
    if not _n(t):
        return "?"
    s = max(0, core.now() - t)
    if s < 60:
        return "now"
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def _head(x, glyph, color, name, host, at, fresh=True, room=60) -> str:
    """'⏸ name host 3m': the name cut only when it alone would not leave room for the rest."""
    where = _host(x, host)
    tail = "%s %s" % (where, _age(at)) if where != name else _age(at)
    name = name or "?"
    over = 2 + T.width(name) + 1 + T.width(tail) - room
    if over > 0:
        name = T.fit(name, max(6, T.width(name) - over))
    return T.c(glyph, color) + " " + T.c(name, "sky" if fresh else "fg", bold=fresh) + " " + T.dim(tail)


def _with_text(x, label, head, text, room, dim=False) -> list:
    """head, then " · text" on the same row as far as words fit (an ellipsis after the last
    whole word when the rest does not)."""
    left = room - T.width(head) - T.width(str(SEP))
    lines = wrap(text, [left]) if text and left >= 10 else []
    body = head + (SEP + (T.dim(lines[0]) if dim else lines[0]) if lines else "")
    return [kv(label, body, W)]


def _waiting(x, d, room) -> list:
    rows = []
    waiting = _l(d.get("waiting"))
    for i, w in enumerate(waiting[:SHOWN_WAITING]):
        head = _head(x, T.g("wait"), "amber", _s(w.get("name")), w.get("host"), w.get("since"), room=room)
        ask = _s(w.get("ask"), 140) or _s(w.get("kind"), 16)
        first = room - T.width(head) - T.width(str(SEP))
        # The question starts beside the name and may take one more line; when not even its
        # first word fits there, it takes the next two lines instead.
        lines = wrap(ask, [first, room - 2]) if ask and first >= 12 else []
        label = "waiting" if i == 0 else ""
        if lines:
            rows.append(kv(label, head + SEP + lines[0], W))
            rows += [kv("", "  " + ln, W) for ln in lines[1:]]
        else:
            rows.append(kv(label, head, W))
            rows += [kv("", "  " + ln, W) for ln in (wrap(ask, [room - 2, room - 2]) if ask else [])]
    total = max(len(waiting), int(_n(_d(d.get("totals")).get("waiting"))))
    if total > SHOWN_WAITING:
        rest = [_s(w.get("name")) for w in waiting[SHOWN_WAITING:]]
        lead = T.dim("+%d more waiting" % (total - SHOWN_WAITING))
        names = fitted(rest, room - T.width(lead) - 3, ", ") if rest else ""
        rows.append(kv("", lead + (T.dim(": ") + names if names and not T.strip(names).startswith("+") else ""), W))
    return rows


def _finished(x, d, room) -> list:
    rows = []
    seen = x.last_seen or 0
    done = [f for f in _l(d.get("feed")) if f.get("kind") == "finished"]
    fresh = [f for f in done if seen and _n(f.get("at")) > seen]
    shown = (fresh or [f for f in done if core.now() - _n(f.get("at")) < 86400])[:SHOWN_DONE]
    for i, f in enumerate(shown):
        head = _head(x, T.g("ok"), "green", _s(f.get("name")), f.get("host"), f.get("at"), fresh=bool(fresh), room=room)
        rows += _with_text(x, "done" if i == 0 else "", head, _s(f.get("text"), 140), room, dim=not fresh)
    if len(fresh) > SHOWN_DONE:
        rows.append(kv("", T.dim("+%d more since your last visit" % (len(fresh) - SHOWN_DONE)), W))
    return rows


def _crashed(x, d, room) -> list:
    """Sessions that crashed (and hosts that stopped answering) since your last visit,
    or in the last six hours, whichever reaches back further."""
    since = core.now() - CRASH_RECENT
    if x.last_seen:
        since = min(since, x.last_seen)
    errs = [f for f in _l(d.get("feed")) if f.get("kind") == "error" and _n(f.get("at")) > since]
    rows = []
    for i, f in enumerate(errs[:SHOWN_CRASHED]):
        head = _head(x, T.g("crit"), "red", _s(f.get("name")), f.get("host"), f.get("at"), fresh=False, room=room)
        rows += _with_text(x, "crashed" if i == 0 else "", head, _s(f.get("what")), room, dim=True)
    if len(errs) > SHOWN_CRASHED:
        rows.append(kv("", T.dim("+%d more" % (len(errs) - SHOWN_CRASHED)), W))
    return rows


def _working(x, d, room) -> list:
    by = {}
    for w in _l(d.get("working")):
        by.setdefault(_host(x, w.get("host")), []).append(_s(w.get("name")) or "?")
    here = _host(x, "local")
    order = sorted(by, key=lambda h: (h != here, -len(by[h]), h))
    rows = []
    for i, h in enumerate(order[:SHOWN_HOSTS]):
        lead = T.c(T.g("work"), "green") + " " + T.c("%s %d" % (h, len(by[h])), "fg") + "  "
        rows.append(kv("working" if i == 0 else "", lead + fitted(by[h], room - T.width(lead), ", "), W))
    if len(order) > SHOWN_HOSTS:
        more = order[SHOWN_HOSTS:]
        rows.append(kv("", T.dim("+%d more host%s working" % (len(more), "" if len(more) == 1 else "s")), W))
    return rows


def _daemons(x, d, room):
    """(row or None, level): the commit each daemon runs, from fleet.versions (hub)."""
    det = _d(d.get("details"))
    vs = _l(det.get("versions"))
    if not vs:
        return None, "ok"
    live = [v for v in vs if v.get("state") == "live"]
    commits = {_s(v.get("loaded"), 7) for v in live if _s(v.get("loaded"))}
    lvl = "ok" if len(live) == len(vs) else "warn"
    head = T.dot(lvl) + " %d/%d live" % (len(live), len(vs))
    why = {"restart": "needs a restart", "stopped": "daemon stopped", "unreachable": "unreachable"}
    rest = [T.c("%s %s" % (_host(x, v.get("host")), why.get(_s(v.get("state"), 16), _s(v.get("state"), 16) or "?")), "amber")
            for v in vs if v.get("state") != "live"]
    if len(commits) > 1:
        rest.append(T.c("mixed code", "amber"))
    mgr = _d(d.get("manager"))
    if "up" in mgr:
        rest.append("manager " + T.dot("ok" if mgr.get("up") else "unknown"))
    # The commit is the first thing to give up its room: who needs a restart matters more.
    full = [head + (" on %s" % next(iter(commits)) if len(commits) == 1 else "")] + rest
    line = SEP.join(full) if T.width(SEP.join(full)) <= room else fitted([head] + rest, room)
    return kv("daemons", line, W), lvl


def _pool(x, d, room):
    """The credential pool Drover spreads launches over (hub): each entry's sessions out of
    its limit, recent throttles, and what an unavailable one is missing."""
    pool = _l(_d(d.get("details")).get("pool"))
    if not pool:
        return None
    full, short = [], []
    for e in pool:
        name = _s(e.get("id"), 24) or "?"
        if e.get("available"):
            cell = "%s %d/%d" % (name, _n(e.get("active")), _n(e.get("max")))
            if _n(e.get("throttles")):
                cell += T.c(" %d throttled" % _n(e["throttles"]), "amber")
            full.append(cell)
            short.append(cell)
        else:
            miss = [_s(m, 48) for m in e.get("missing") or [] if _s(m)] if isinstance(e.get("missing"), list) else []
            cell = T.c("%s %s" % (name, T.g("crit")), "amber")
            full.append(cell + (T.dim(" %s unset" % miss[0]) if miss else ""))
            short.append(cell)
    line = fitted(full, room)
    if T.width(fitted(full, 10 ** 6)) > room:  # the names of the missing variables do not fit: drop them first
        line = fitted(short, room)
    return kv("pool", line, W)


@panel("drover", title="DROVER")
def p_drover(x):
    d = x.data("drover")
    if not isinstance(d, dict) or d.get("daemon") not in ("up", "down"):
        return None  # no Drover daemon on this machine
    room = x.cw - 1 - W
    stale = x.stale_note("drover", 6, 900)
    if d.get("daemon") == "down":
        why = _s(d.get("why"), 30) or "no answer"
        row = T.dot("unknown") + T.dim(" daemon not answering (%s)" % why)
        hint = SEP + T.c("drover serve", "sky")
        return Panel("drover", "DROVER", [row + (hint if T.width(row + hint) <= x.cw - 1 else "")], "unknown", stale)
    rows, lvl = [], "ok"
    waiting = _waiting(x, d, room)
    if waiting:
        lvl = "warn"
    rows += waiting + _finished(x, d, room) + _crashed(x, d, room) + _working(x, d, room)
    row, l2 = _daemons(x, d, room)
    rows += [r for r in (row, _pool(x, d, room)) if r]
    lvl = T.worst(lvl, l2)
    tot = _d(d.get("totals"))
    if not rows:
        rows = [T.dot("unknown") + T.dim(" no agent sessions working or waiting")]
    hosts, busy, idle = (int(_n(tot.get(k))) for k in ("hosts", "working", "idle"))
    parts = ["%d host%s" % (hosts, "" if hosts == 1 else "s") if hosts else "", "%d working" % busy if busy else "",
             "%d idle" % idle if idle else ""]
    note = stale or fitted([p for p in parts if p], x.cw - 16)  # the title, its mark and a stretch of rule
    return Panel("drover", "DROVER", rows, lvl, T.strip(note))

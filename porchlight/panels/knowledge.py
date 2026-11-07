"""The KNOWLEDGE panel: the brain's health and what was written since your last
visit, open reflections, wikis and links. Everything comes from the
`knowledge` record (porchlight/sources/knowledge.py); nothing here touches the
network. Items (for `porchlight -i` and `porchlight view`): one per recent
brain entry, reflection, wiki page and link.
"""
from __future__ import annotations

from .. import theme as T
from ..ui import Action, Item, SEP, ago, fitted, kv, panel, wrap

W = 9


# ── Typed reads: a record kept from another version may hold anything ───────
def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []


def _s(v, n: int = 160) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _n(v) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _comma(v) -> str:
    try:
        return f"{int(_n(v)):,}"
    except (TypeError, ValueError, OverflowError):
        return "0"


def _plural(word: str, n: float) -> str:
    return word if n == 1 else (word if word.endswith("s") else word + "s")


def _url_ok(v) -> str:
    s = _s(v, 2000)
    return s if s.startswith(("http://", "https://")) else ""


# ── Rows ──────────────────────────────────────────────────────────────────────
def _brain_rows(x, b, room):
    """(rows, level, new_count): new_count is how many recent entries postdate x.last_seen."""
    entries = _n(b.get("entries"))
    lvl = "crit" if b.get("ok") is False else ("warn" if (b.get("stale") or entries == 0) else "ok")
    head = T.dot(lvl) + " %s entries" % _comma(entries)
    recent = _l(b.get("recent"))
    new = [r for r in recent if x.last_seen and _n(r.get("created")) > x.last_seen]
    if b.get("ok") is False and _s(b.get("error")):
        tail = _s(b.get("error"), 200)
    elif new:
        tail = "new: %s%s" % (_s(new[0].get("title"), 160), (" +%d" % (len(new) - 1)) if len(new) > 1 else "")
    elif recent:
        tail = _s(recent[0].get("title"), 160)
    else:
        tail = ""
    first = room - T.width(head) - T.width(str(SEP))
    lines = wrap(tail, [first, room]) if tail and first >= 8 else (wrap(tail, [room, room]) if tail else [])
    rows = []
    if lines:
        rows.append(kv("brain", head + SEP + lines[0], W))
        rows += [kv("", "  " + ln, W) for ln in lines[1:]]
    else:
        rows.append(kv("brain", head, W))
    return rows, lvl, len(new)


def _reflect_rows(r, room):
    open_n = _n(r.get("open"))
    by_kind = {_s(k, 24) or "other": _n(v) for k, v in _d(r.get("by_kind")).items() if _s(k, 24) and _n(v) > 0}
    if r.get("ok") is False and not open_n and not by_kind:
        return [kv("reflect", T.dim(_s(r.get("error"), 120) or "unavailable"), W)], True
    parts = ["%d open" % open_n] if open_n or not by_kind else []
    parts += ["%d %s" % (c, _plural(k, c)) for k, c in sorted(by_kind.items(), key=lambda kv: -kv[1])]
    return [kv("reflect", fitted(parts, room), W)], (r.get("ok") is False)


def _wikis_rows(wikis, room):
    parts, newest, bad = [], None, False
    for w in wikis:
        if w.get("ok") is False:
            parts.append(T.c("%s error" % (_s(w.get("name"), 40) or "wiki"), "amber"))
            bad = True
            continue
        parts.append("%s %d pages" % (_s(w.get("name"), 40) or "wiki", _n(w.get("pages"))))
        for p in _l(w.get("recent")):
            if newest is None or _n(p.get("mtime")) > _n(newest.get("mtime")):
                newest = p
    if newest:
        parts.append("%s %s" % (_s(newest.get("title"), 60), ago(_n(newest.get("mtime")), suffix="")))
    return [kv("wikis", fitted(parts, room), W)], bad


def _links_row(links, room):
    parts = [_s(link.get("label"), 40) or "link" for link in links]
    return kv("links", fitted(parts, room), W)


# ── Items (porchlight -i / `porchlight view`) ────────────────────────────────
def _items(d) -> list:
    items = []
    b = _d(d.get("brain"))
    for i, r in enumerate(_l(b.get("recent"))):
        rid = _s(r.get("id"), 40)
        acts = [Action("copy", "copy id", "c", {"text": rid})] if rid else []
        items.append(Item("knowledge:brain:%s" % (rid or i), _s(r.get("title"), 160) or "(untitled)",
                           detail=["kind: %s" % (_s(r.get("kind"), 24) or "?")], url=_url_ok(r.get("url")), actions=acts))
    refl = _d(d.get("reflections"))
    for i, r in enumerate(_l(refl.get("recent"))):
        rid = _s(r.get("id"), 24)
        acts = [Action("copy", "copy id", "c", {"text": rid})] if rid else []
        items.append(Item("knowledge:reflection:%s" % (rid or i), _s(r.get("title"), 160) or "(untitled)",
                           detail=["kind: %s" % (_s(r.get("kind"), 16) or "?"), "host: %s" % (_s(r.get("host"), 40) or "?")],
                           actions=acts))
    for w in _l(d.get("wikis")):
        wurl, wname = _url_ok(w.get("url")), _s(w.get("name"), 60) or "wiki"
        for p in _l(w.get("recent")):
            path = _s(p.get("path"), 200)
            acts = [Action("copy", "copy path", "c", {"text": path})] if path else []
            items.append(Item("knowledge:wiki:%s:%s" % (wname, path or "?"),
                               "%s: %s" % (wname, _s(p.get("title"), 120) or path or "?"), url=wurl, actions=acts))
    for link in _l(d.get("links")):
        url = _url_ok(link.get("url"))
        if not url:
            continue
        label = _s(link.get("label"), 40) or url
        items.append(Item("knowledge:link:%s" % label, label, url=url))
    return items


@panel("knowledge", title="KNOWLEDGE")
def p_knowledge(x):
    d = x.data("knowledge")
    if not isinstance(d, dict):
        return None
    room = x.cw - 1 - W
    rows, lvl, new_total, bad = [], "ok", 0, False
    b = _d(d.get("brain"))
    if b:
        brows, blvl, bnew = _brain_rows(x, b, room)
        rows += brows
        lvl = T.worst(lvl, blvl)
        new_total += bnew
    r = _d(d.get("reflections"))
    if r:
        rrows, rbad = _reflect_rows(r, room)
        rows += rrows
        bad = bad or rbad
    wikis = _l(d.get("wikis"))
    if wikis:
        wrows, wbad = _wikis_rows(wikis, room)
        rows += wrows
        bad = bad or wbad
    links = _l(d.get("links"))
    if links:
        rows.append(_links_row(links, room))
    if bad:
        lvl = T.worst(lvl, "warn")
    if not rows:
        rows = [T.dot("unknown") + T.dim(" nothing configured")]
    note = "%d new since last visit" % new_total if new_total else x.stale_note("knowledge", 4, 1800)
    return x.panel("knowledge", rows, lvl, T.strip(note), items=_items(d))

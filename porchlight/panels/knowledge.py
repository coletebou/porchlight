"""The KNOWLEDGE panel: the shared brain, the search index, reflections per repo, your note
folders and wiki links, from the `knowledge` record (nothing here touches the network).

In `porchlight -i` every decision, repo, note folder and link is an item: links open
in the browser (`o`), and `s` searches the brain and reflections from any panel.
"""
from __future__ import annotations

import time

from .. import core, theme as T
from ..ui import SEP, fitted, kv, panel

W = 9
SHOWN_DECISIONS = 2


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []


def _s(v, n=120) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _n(v) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _age(t) -> str:
    t = _n(t)
    return core.human_age(max(0, time.time() - t)) if t else ""


def _k(n: int) -> str:
    return "%.1fk" % (n / 1000.0) if n >= 10000 else str(n)


def _when(t) -> str:
    t = _n(t)
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


@panel("knowledge", title="KNOWLEDGE")
def p_knowledge(x):
    d = _d(x.data("knowledge"))
    if not d:
        return None
    room = x.cw - 1
    if d.get("error") and not any(d.get(k) for k in ("brain", "search", "reflections", "notes", "links")):
        return x.panel("knowledge", [T.dim(_s(d.get("error")))], note=x.stale_note("knowledge"))
    rows, items, level = [], [], "ok"
    b = _d(d.get("brain"))
    if b:
        if not b.get("configured"):
            state = T.dim("no key configured in Drover")
        elif not b.get("ok"):
            state, level = T.c("%s unreachable" % T.g("crit"), "red"), "warn"
        else:
            state = T.c(_k(_n(b.get("entries"))), "fg", bold=True) + T.dim(" entries") + SEP + (
                T.c("stale", "amber") if b.get("stale") else T.c("fresh", "green"))
        rows.append(kv("brain", state, W))
        decs = _l(b.get("decisions"))
        for n, dec in enumerate(decs):
            title, age = _s(dec.get("title"), 200), _age(dec.get("at"))
            if n < SHOWN_DECISIONS:
                left = room - W - (len(age) + 1 if age else 0)
                rows.append(" " * W + T.pad(T.fit(title, left), left) + ((" " + T.dim(age)) if age else ""))
            items.append({"key": "decision:%s" % (_s(dec.get("slug"), 100) or n), "source": "knowledge",
                          "text": T.c("◆", "gold_h") + " " + title + (T.dim("  " + age) if age else ""),
                          "detail": [T.c(title, "fg", bold=True), "", kv("kind", "brain decision", W),
                                     kv("slug", _s(dec.get("slug"), 100), W), kv("recorded", _when(dec.get("at")), W),
                                     "", T.dim("s searches the brain for more")], "actions": []})
        if len(decs) > SHOWN_DECISIONS:
            more = len(decs) - SHOWN_DECISIONS
            rows.append(" " * W + T.dim("+%d decision%s · porchlight -i" % (more, "" if more == 1 else "s")))
    s = _d(d.get("search"))
    if s:
        if not s.get("enabled", True):
            rows.append(kv("index", T.dim("search is off in Drover"), W))
        else:
            cells = [T.c(_k(_n(s.get("chunks"))), "fg", bold=True) + T.dim(" chunks"),
                     T.dim("%s sources" % _k(_n(s.get("sources"))))]
            if s.get("newest"):
                cells.append(T.dim("newest ") + _age(s.get("newest")))
            rows.append(kv("index", fitted(cells, room - W), W))
    refl = _l(d.get("reflections"))
    if refl:
        total = sum(_n(r.get("open")) for r in refl)
        cells = ["%s %s" % (_s(r.get("repo"), 30), T.c(str(_n(r.get("open"))), "fg")) for r in refl if _n(r.get("open"))]
        rows.append(kv("refl", T.dim("%d open " % total) + fitted(cells, room - W - len("%d open " % total)), W))
        for r in refl:
            items.append({"key": "refl-repo:%s" % _s(r.get("repo"), 40), "source": "knowledge",
                          "text": T.c(_s(r.get("repo"), 40), "fg") + T.dim("  %d open · %d stale · %d done" % (
                              _n(r.get("open")), _n(r.get("stale")), _n(r.get("done")))),
                          "detail": [T.c(_s(r.get("repo"), 40), "fg", bold=True), "",
                                     kv("open", str(_n(r.get("open"))), W), kv("stale", str(_n(r.get("stale"))), W),
                                     kv("done", str(_n(r.get("done"))), W), kv("last", _when(r.get("last")), W), "",
                                     T.dim("the TODOS panel lists the open actions and questions")], "actions": []})
    for nf in _l(d.get("notes")):
        name = _s(nf.get("name"), 30)
        if nf.get("error"):
            rows.append(kv("notes", T.dim("%s: %s" % (name, _s(nf.get("error"), 40))), W))
            continue
        rec = _l(nf.get("recent"))
        cells = [T.c(name, "fg") + T.dim(" %d%s" % (_n(nf.get("notes")), "+" if nf.get("capped") else ""))]
        cells += ["%s %s" % (_s(r.get("title"), 40), T.dim(_age(r.get("at")))) for r in rec]
        rows.append(kv("notes", fitted(cells, room - W), W))
        items.append({"key": "notes:%s" % name, "source": "knowledge",
                      "text": T.c(name, "fg") + T.dim("  %d notes" % _n(nf.get("notes"))),
                      "detail": [T.c(name, "fg", bold=True), "", kv("notes", str(_n(nf.get("notes"))), W), ""] +
                      [kv("edited", "%s  %s" % (_s(r.get("title"), 80), T.dim(_age(r.get("at")))), W) for r in rec],
                      "actions": []})
    links = _l(d.get("links"))
    if links:
        rows.append(kv("links", fitted([T.c(_s(ln.get("name"), 30), "sky") for ln in links], room - W), W))
        for ln in links:
            items.append({"key": "link:%s" % _s(ln.get("name"), 30), "source": "knowledge", "url": _s(ln.get("url"), 300),
                          "text": T.c("↗", "sky") + " " + _s(ln.get("name"), 30) + T.dim("  " + _s(ln.get("url"), 120)),
                          "detail": [T.c(_s(ln.get("name"), 30), "fg", bold=True), "", kv("url", _s(ln.get("url"), 300), W)],
                          "actions": ["open"]})
    for part, why in _d(d.get("errors")).items():
        rows.append(T.c(T.g("crit"), "red") + " " + T.dim(T.fit("%s: %s" % (_s(part, 20), _s(why, 100)), max(4, room - 2))))
    if not rows:
        return None
    if any(a.get("sev") == "warn" for a in _l(d.get("alerts"))):
        level = "warn"
    return x.panel("knowledge", rows, level=level, note=x.stale_note("knowledge", ttl_mult=4), items=items)

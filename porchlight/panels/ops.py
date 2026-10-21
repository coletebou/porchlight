"""Ops panels: health routes, backups and certificates, file budgets."""
from __future__ import annotations

from .. import core, theme as T
from ..ui import Panel, ago, cells, kv, panel


def endpoint_cells(rows, cw, slow_ms=1500) -> list:
    items = []
    for r in rows:
        if r.get("up"):
            ms = r.get("ms") or 0
            lv = "warn" if r.get("slow") or ms > slow_ms else "ok"
            items.append(T.dot("ok") + " " + r["label"] + " " + T.sev("%d" % ms, lv) +
                         ((" " + T.dim(r["info"])) if r.get("info") else ""))
        else:
            items.append(T.dot("crit") + " " + T.c(r["label"], "red") + " " + T.dim(str(r.get("error") or r.get("status"))))
    return cells(items, cw, 15)


@panel("health", title="HEALTH")
def p_health(x):
    rows, lvl = [], "ok"
    h = x.data("health")
    if h:
        lines = endpoint_cells(h.get("rows") or [], x.cw - 10)
        rows += [kv("routes" if i == 0 else "", ln, 9) for i, ln in enumerate(lines)]
        if any(not r.get("up") for r in h.get("rows") or []):
            lvl = "crit" if h.get("alerts") else "warn"
    m = x.data("mongodb")
    if m and m.get("summary"):
        lvl = T.worst(lvl, m.get("status", "unknown"))
        rows.append(kv("mongodb", T.dot(m.get("status", "unknown")) + " " + m["summary"], 9))
    dr = x.data("drift")
    for r in (dr or {}).get("rows") or []:
        if r.get("ok"):
            txt = T.dot("ok") + " " + r["label"] + T.dim(" @ %s = %s" % (r.get("deployed"), r.get("ref")))
        elif r.get("error"):
            txt = T.dot("unknown") + " " + r["label"] + T.dim(" " + r["error"])
        else:
            behind = r.get("behind")
            txt = T.dot("warn") + " " + r["label"] + " " + T.c(
                ("%d behind %s" % (behind, r.get("ref"))) if behind and behind > 0 else "≠ %s" % r.get("ref"), "amber") + \
                T.dim(" (%s → %s)" % (r.get("deployed"), r.get("want")))
            lvl = T.worst(lvl, "warn")
        rows.append(kv("deploy", txt, 9))
    if not rows:
        return None
    return Panel("health", "HEALTH", rows, lvl, x.stale_note("health", 4, 900) if h else "")


@panel("backups", title="BACKUPS")
def p_backups(x):
    rows, lvl, items = [], "ok", []
    for r in (x.data("backups") or {}).get("rows") or []:
        if r.get("ok"):
            l2 = "ok"
        elif r.get("result") in ("success", "file", None, "missing"):
            l2 = "warn"  # the last run did not fail: it is overdue, missing, or its file is stale
        else:
            l2 = "crit"
        lvl = T.worst(lvl, l2)
        age = core.human_age(core.now() - r["last"]) if r.get("last") else "never"
        nxt = (T.dim(" next " + core.human_age(r["next"] - core.now()))) if r.get("next") and r["next"] > core.now() else ""
        items.append(T.dot(l2) + " " + r["name"] + " " + T.dim(age) + nxt)
    rows += cells(items, x.cw - 1, 18)
    ce = x.data("certs")
    if ce:
        bits = []
        for r in ce.get("rows") or []:
            if r.get("ok"):
                l2 = "ok"
            elif r.get("days") is None or r["days"] < 10:
                l2 = "crit"
            else:
                l2 = "warn"
            lvl = T.worst(lvl, l2)
            bits.append(T.dot(l2) + " " + r["name"] + " " + (T.dim("%dd" % r["days"]) if r.get("days") is not None
                                                              else T.c(str(r.get("error") or "invalid")[:18], "red")))
        rows += [kv("tls" if i == 0 else "", ln, 5) for i, ln in enumerate(cells(bits, x.cw - 6, 18))]
    if not rows:
        return None
    return Panel("backups", "BACKUPS", rows, lvl)


@panel("files", title="FILES")
def p_files(x):
    d = x.data("files")
    if not d or not d.get("rows"):
        return None
    rows, lvl = [], "ok"
    for r in d["rows"][:8]:
        p = r.get("pct", 0)
        l2 = T.level_for(p, 90, 100)
        lvl = T.worst(lvl, l2)
        cap = "/".join(s for s in ((core.human_bytes(r["max_bytes"]) if r.get("max_bytes") else ""),
                                   ("%dL" % r["max_lines"] if r.get("max_lines") else "")) if s)
        rows.append(T.pad(T.c(r["label"], "fg"), 22) + " " + T.bar(p, 10, 90, 100) + " " +
                    T.sev("%3d%%" % p, l2) + T.dim("  %s · %dL" % (core.human_bytes(r["bytes"]), r["lines"])) +
                    (T.dim(" of " + cap) if cap else "") + (T.dim(" · " + ago(r["mtime"])) if r.get("mtime") else ""))
    if len(d["rows"]) > 8:
        rows.append(T.dim("%s %d more files" % (T.ellipsis(), len(d["rows"]) - 8)))
    return Panel("files", "FILES", rows, lvl, "%d tracked" % d.get("files", 0))


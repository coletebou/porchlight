"""The TODOS panel: open items across providers, the ask-coverage line, and
Items/Actions for porchlight -i. Record shape: docs/contracts/todo-item-v1.md.

Nothing here touches the network; everything comes from the `todos` record.
"""
from __future__ import annotations

import time

from .. import core, theme as T
from ..ui import Action, Item, Panel, SEP, kv, panel

W = 9
MAX_ITEMS = 50          # ui.Item objects attached for porchlight -i / `porchlight view`
_PRI_LEVEL = {"urgent": "crit", "high": "warn", "medium": "info", "low": "unknown", "none": "stale"}
_PROVIDER_ORDER = ("badland", "drover", "reflections", "github", "file")


# ── Typed reads: a record kept from another version may hold anything ───────
def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _s(v, n: int = 120) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _items(v) -> list:
    return [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []


def _pri_dot(priority) -> str:
    return T.dot(_PRI_LEVEL.get(priority, "stale"))


def _due_text(due, priority) -> str:
    now = core.now()
    overdue = due < now
    when = core.human_age(now - due) if overdue else time.strftime("%b %d", time.localtime(due))
    text = ("overdue " + when) if overdue else ("due " + when)
    if overdue:
        return T.c(text, "red" if priority in ("urgent", "high") else "amber")
    return T.dim(text)


def _summary_line(providers: dict) -> str:
    bits = []
    for name in _PROVIDER_ORDER:
        p = providers.get(name)
        if not isinstance(p, dict):
            continue
        if p.get("ok"):
            bits.append("%s %d" % (name, int(p.get("open") or 0)))
        else:
            err = _s(p.get("error"), 30)
            bits.append(T.dot("unknown") + " " + name + (T.dim(" (%s)" % err) if err else ""))
    return SEP.join(bits) if bits else T.dim("no providers configured")


def _item_line(it: dict, room: int) -> str:
    head = _pri_dot(it.get("priority")) + " " + _s(it.get("title"), 160)
    tail_bits = []
    grp = _s(it.get("group"), 30)
    if grp:
        tail_bits.append(T.dim(grp))
    due = it.get("due")
    if isinstance(due, (int, float)):
        tail_bits.append(_due_text(due, it.get("priority")))
    if not tail_bits:
        return head
    tail = SEP.join(tail_bits)
    return (head + SEP + tail) if T.width(head) + T.width(str(SEP)) + T.width(tail) <= room else head


def _asks_line(asks: dict) -> str:
    ids = asks.get("ids") if isinstance(asks.get("ids"), dict) else {}
    systems = asks.get("systems") if isinstance(asks.get("systems"), list) else []
    total = len(ids)
    everywhere = sum(1 for by in ids.values() if systems and all(s in (by if isinstance(by, dict) else {}) for s in systems))
    missing = {}
    for by in ids.values():
        by = by if isinstance(by, dict) else {}
        for s in systems:
            if s not in by:
                missing[s] = missing.get(s, 0) + 1
    bits = ["%d" % total, "%d everywhere" % everywhere]
    if missing:
        order = sorted(missing, key=lambda s: (-missing[s], s))
        bits.append("missing: " + SEP.join("%s %d" % (s, missing[s]) for s in order))
    return SEP.join(bits)


def _detail(it: dict) -> list:
    lines = []
    if it.get("group"):
        lines.append("group: " + str(it["group"]))
    if it.get("state"):
        lines.append("state: " + str(it["state"]))
    if it.get("priority") and it["priority"] != "none":
        lines.append("priority: " + str(it["priority"]))
    due = it.get("due")
    if isinstance(due, (int, float)):
        lines.append("due: " + time.strftime("%Y-%m-%d", time.localtime(due)))
    for key, label in (("created", "created"), ("updated", "updated")):
        v = it.get(key)
        if isinstance(v, (int, float)):
            lines.append("%s: %s ago" % (label, core.human_age(core.now() - v)))
    if it.get("tags"):
        lines.append("tags: " + ", ".join(str(t) for t in it["tags"]))
    if it.get("asks"):
        lines.append("asks: " + ", ".join(str(a) for a in it["asks"]))
    if it.get("host"):
        lines.append("host: " + str(it["host"]))
    return lines


def _ui_item(it: dict) -> Item:
    full_id = "todos:" + str(it.get("id") or "")
    title = _s(it.get("title"), 160) or "(untitled)"
    actions = [Action("copy", "copy id", "y", {"text": str(it.get("id") or "")})]
    if "done" in (it.get("can") or []):
        actions.append(Action("todos.done", "mark done", "d", {"id": str(it.get("id") or "")}, writes=True,
                              confirm="Mark '%s' done in %s?" % (title[:60], it.get("provider"))))
    return Item(full_id, title, detail=_detail(it), level="ok", url=str(it.get("url") or ""), actions=actions)


def _ask_gap_items(asks: dict) -> list:
    ids = asks.get("ids") if isinstance(asks.get("ids"), dict) else {}
    systems = asks.get("systems") if isinstance(asks.get("systems"), list) else []
    if not systems:
        return []
    out = []
    for aid in sorted(ids):
        by = ids[aid] if isinstance(ids[aid], dict) else {}
        have = [s for s in systems if s in by]
        missing = [s for s in systems if s not in by]
        if not missing:
            continue
        detail = ["present in: " + ", ".join(have) if have else "present nowhere checked",
                  "missing from: " + ", ".join(missing)]
        out.append(Item("todos:ask:" + aid, aid + " missing: " + ", ".join(missing), detail=detail, level="info"))
    return out


@panel("todos", title="TODOS")
def p_todos(x):
    d = x.data("todos")
    if not isinstance(d, dict):
        return None  # the todos source is off or has not run yet
    items = _items(d.get("items"))
    providers = _d(d.get("providers"))
    total = int(d.get("total")) if isinstance(d.get("total"), (int, float)) else len(items)
    from ..registry import SOURCES
    shown = int(SOURCES["todos"].config(x.host).get("shown", 6)) if "todos" in SOURCES else 6
    room = max(10, x.cw - 1 - W)

    rows = [kv("open", _summary_line(providers), W)]
    for i, it in enumerate(items[:max(0, shown)]):
        rows.append(kv("items" if i == 0 else "", _item_line(it, room), W))
    asks = d.get("asks") if isinstance(d.get("asks"), dict) else None
    if asks and not asks.get("error"):
        rows.append(kv("asks", _asks_line(asks), W))
    elif asks and asks.get("error"):
        rows.append(kv("asks", T.c(_s(asks["error"], 80), "amber"), W))

    lvl = "warn" if any(not p.get("ok") for p in providers.values() if isinstance(p, dict)) else "ok"
    if isinstance(d.get("alerts"), list) and d["alerts"]:
        lvl = "warn"

    panel_items = [_ui_item(it) for it in items[:MAX_ITEMS]]
    if asks and not asks.get("error"):
        panel_items += _ask_gap_items(asks)

    stale = x.stale_note("todos")
    note_bits = ["%d open" % total]
    if stale:
        note_bits.append(T.strip(stale))
    note = SEP.join(note_bits)
    return Panel("todos", "TODOS", rows, lvl, note, items=panel_items)

"""The AI CHATS panel: ai-cli's saved chats, newest first, from the `ai_chats` record.
In `porchlight -i` each chat is an item; `A` opens ai-cli's TUI and `ai -i -c` resumes
the newest chat."""
from __future__ import annotations

import re
import time

from .. import core, theme as T
from ..ui import kv, panel

W = 9


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _s(v, n=60) -> str:
    """Record text for one line, control characters out (a record from another version of
    the source, or edited by hand, must not reach the terminal as escape sequences)."""
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        return ""
    return " ".join(_CONTROL.sub(" ", str(v)).split())[:n]


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _age(t) -> str:
    return core.human_age(max(0, time.time() - t)) if isinstance(t, (int, float)) and t else ""


@panel("ai_chats", title="AI CHATS")
def p_ai_chats(x):
    d = x.data("ai_chats")
    if not isinstance(d, dict):
        return None
    chats = [c for c in d.get("sessions") or [] if isinstance(c, dict)]
    if not chats:
        msg = _s(d.get("error"), 80) or "no saved chats yet: ai -i"
        return x.panel("ai_chats", [T.dim(msg)], note=x.stale_note("ai_chats"))
    room = x.cw - 1
    rows, items = [], []
    for n, c in enumerate(chats):
        models = ", ".join(_s(p, 20) for p in _list(c.get("providers")) if isinstance(p, str)) or _s(c.get("model"), 30)
        age = _age(c.get("updated"))
        turns = c.get("turns") if isinstance(c.get("turns"), int) else 0
        tail = T.dim("%s%s" % (("%d turns " % turns) if turns else "", age))
        name = _s(c.get("name"), 60)
        left = room - T.width(tail) - 1
        if n < 5:
            rows.append(T.pad(T.fit(T.c(name, "fg") + T.dim("  " + models), left), left) + " " + tail)
        items.append({"key": "ai-chat:%s" % name, "source": "ai_chats",
                      "text": T.c(name, "fg") + T.dim("  %s · %s" % (models or "?", age or "?")),
                      "detail": [T.c(name, "fg", bold=True), "", kv("models", models or "?", W),
                                 kv("turns", str(turns), W), kv("updated", age + " ago" if age else "?", W), "",
                                 T.dim("A opens ai-cli's TUI · `ai -i -c` resumes the newest chat")],
                      "actions": []})
    if len(chats) > 5:
        rows.append(T.dim("  +%d more · porchlight -i" % (len(chats) - 5)))
    if d.get("skipped"):
        rows.append(T.dim("  %d unreadable chat file%s skipped" % (d["skipped"], "" if d["skipped"] == 1 else "s")))
    return x.panel("ai_chats", rows, note=x.stale_note("ai_chats"), items=items)

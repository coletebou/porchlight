"""Example plugin: copy to ~/.config/porchlight/plugins/todo.py

Counts open items in a todo.txt-style file and warns past a threshold.
Configure with:

    [sources.todo]
    path = "~/todo.txt"
    warn_at = 20
"""
import os

from porchlight import kv, panel, source, theme as T


@source("todo", ttl=300, defaults={"path": "~/todo.txt", "warn_at": 20})
def todo(ctx):
    """Open items in my todo file."""
    path = os.path.expanduser(ctx.cfg["path"])
    with open(path, encoding="utf-8") as f:
        items = [ln.strip() for ln in f if ln.strip() and not ln.startswith("x ")]
    data = {"open": len(items), "top": items[:3]}
    if len(items) >= int(ctx.cfg["warn_at"]):
        data["alerts"] = [{"sev": "warn", "msg": "%d open todos" % len(items)}]
    return data


@panel("todo", title="TODO")
def todo_panel(x):
    d = x.data("todo")
    if not d:
        return None
    rows = [kv("open", T.c(str(d["open"]), "fg", bold=True))] + [T.dim("· ") + t for t in d["top"]]
    return x.panel("todo", rows, note=x.stale_note("todo"))

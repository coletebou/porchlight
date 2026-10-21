"""The public plugin API (re-exported lazily by `porchlight`).

    from porchlight import source, panel, Panel, kv, cells, SEP, theme, core

    @source("todo", ttl=300, desc="Open items in my todo file")
    def todo(ctx):
        items = [l for l in open(ctx.cfg.get("path", "/dev/null")) if l.strip()]
        return {"open": len(items), "alerts": [] if len(items) < 20 else
                [{"sev": "warn", "msg": "%d open todos" % len(items)}]}

    @panel("todo", title="TODO")
    def todo_panel(x):
        d = x.data("todo")
        return x.panel("todo", [kv("open", str(d["open"]))]) if d else None
"""
from . import core, theme  # noqa: F401
from .registry import EVERYONE, source  # noqa: F401
from .ui import SEP, Panel, cells, kv, panel  # noqa: F401

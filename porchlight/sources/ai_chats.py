"""ai-cli's saved chats, from `ai sessions --json` (github.com/coletebou/ai-cli).

ai-cli asks several models at once, in a one-shot query, a chat REPL (`ai -i`) or a
side-by-side TUI (`ai -t`). It saves each chat; `ai sessions --json` lists them as
`{"version": 1, "sessions": [{"name", "updated", "turns", "providers", "model"?}]}`,
metadata only: never a message, never a key. This source keeps that list, so the AI
CHATS panel can show what you talked to which models about, and `porchlight -i` can
take you into ai-cli (`A`). Off unless the `ai` binary is installed.
"""
from __future__ import annotations

import json

from .. import core, secrets
from ..registry import source

DEFAULTS = {"limit": 8, "command": ["ai", "sessions", "--json"]}


def clean(v, n=80) -> str:
    return secrets.mask_shapes(" ".join(str(v if v is not None else "").split()))[:n]


@source("ai_chats", ttl=600, timeout=10, binaries=("ai",), defaults=DEFAULTS)
def ai_chats(ctx):
    """ai-cli's saved chats: name, models, turns, last update (metadata only)."""
    argv = [str(a) for a in ctx.cfg.get("command") or DEFAULTS["command"]]
    exe = core.which(argv[0])
    if not exe:
        return {"error": "%s not installed" % argv[0], "sessions": []}
    limit = max(1, min(50, int(ctx.cfg.get("limit") or 8)))
    rc, out, err = ctx.run([exe] + argv[1:] + ["--limit", str(limit)], timeout=8)
    if rc != 0:
        lines = (err or "").strip().splitlines()
        raise RuntimeError("ai sessions: %s" % clean(lines[-1] if lines else "exit %d" % rc, 120))
    doc = json.loads(out or "{}")
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise RuntimeError("ai sessions: unknown output version %r" % (doc.get("version") if isinstance(doc, dict) else None))
    rows = []
    for s in doc.get("sessions") or []:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        provs = [clean(p, 20) for p in (s.get("providers") if isinstance(s.get("providers"), list) else []) if isinstance(p, str)][:6]
        rows.append({"name": clean(s.get("name"), 60), "providers": provs, "model": clean(s.get("model"), 40),
                     "turns": int(s["turns"]) if isinstance(s.get("turns"), int) and not isinstance(s.get("turns"), bool) else 0,
                     "updated": int(core.iso_epoch(s.get("updated")) or 0) if isinstance(s.get("updated"), str) else 0})
    return {"sessions": rows[:limit], "skipped": int(doc.get("skipped") or 0) if isinstance(doc.get("skipped"), int) else 0}

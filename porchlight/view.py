"""`porchlight view`: the dashboard as data (contract: docs/contracts/porchlight-view-v1.md).

The same Frame the login render lays out (render.prepare), as one JSON document:
the alerts, every panel with plain-text lines, and the items and actions behind
them. porchlight -i, ai-cli (`ai motd`) and anything else that wants the dashboard
without parsing ANSI read this. No credential is ever in it: panels are built from
the cache, which holds none, and every string passes secrets.mask_shapes again.
"""
from __future__ import annotations

import json

from . import __version__, core, secrets, theme as T

SCHEMA = "porchlight-view"
VERSION = 1


def _plain(s) -> str:
    return secrets.mask_shapes(T.strip(str(s)))


def fleet_info(host, cache) -> dict:
    """Where the fleet-wide panels come from: this machine (a hub, or no fleet) or the
    hub's bundle, and how old that bundle is."""
    from . import hosts
    b = cache.bundle if hosts.bundle_ok(cache.bundle) else {}  # an unknown version: as if none arrived
    if host.hub or not b:
        return {"role": "hub" if host.hub else "solo", "hub": host.label if host.hub else "", "bundle_age": None}
    ts = b.get("ts")
    age = core.now() - ts if isinstance(ts, (int, float)) else None
    return {"role": "peer", "hub": str(b.get("from") or ""), "bundle_age": age}


def panel_dict(p) -> dict:
    return {"key": p.key, "title": _plain(p.title), "level": p.level, "note": _plain(p.note),
            "lines": [_plain(ln) for ln in p.lines],
            "items": [_item(i) for i in getattr(p, "items", [])]}


def _item(i) -> dict:
    d = i.to_dict()
    d["text"] = _plain(d["text"])
    d["detail"] = [_plain(x) for x in d["detail"]]
    return d


def build(host, cache, width=None, only=None, frame=None) -> dict:
    """The view document. `frame`: reuse a render.prepare() result."""
    from . import render
    f = frame or render.prepare(host, cache, width=width or 120, only=only, compact=False)
    return {
        "schema": SCHEMA, "v": VERSION, "porchlight": __version__,
        "host": host.name, "label": host.label, "identity": host.identity, "ts": core.now(),
        "fleet": fleet_info(host, cache),
        "alerts": [{"sev": a.get("sev"), "msg": _plain(a.get("msg", "")), "src": a.get("src", ""),
                    "fix": _plain(a.get("fix", "")), "new": bool(a.get("new"))} for a in f.al],
        "panels": [panel_dict(p) for p in f.built],
    }


def run(host, cache, flags, words) -> int:
    """porchlight view [panel...] [--compact-json]: print the view document."""
    from . import panels
    only = [w for w in words if w in panels.BUILDERS] or None
    doc = build(host, cache, only=only)
    print(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) if "--compact-json" in flags
          else json.dumps(doc, indent=2, ensure_ascii=False))
    return 0

"""This machine's compact status, written after every collector run.

The hub pulls it from each peer (sources/fleet.py) to build the fleet table, so
it stays small: vitals, agent counts, top alerts, an optional badge, and the
records of sources marked share=True (bounded in size).
"""
from __future__ import annotations

import json

from . import alerts, core, live, registry

SHARED_MAX_BYTES = 16 * 1024


def build(host, cache=None) -> dict:
    cache = cache or registry.Cache()
    lv = alerts.probe_all(host)
    v = lv["vitals"]
    pr = cache.data("procs") or {}
    found = alerts.own(alerts.gather(host, cache, lv), host)
    agents = pr.get("agents") or {}
    snap = {
        "v": 1, "host": host.name, "label": host.label, "identity": host.identity, "ts": core.now(),
        "os": v.get("os"), "uptime": v.get("uptime"), "load": round(v["load"][0], 2), "cores": v["cores"],
        "mem": round(core.pct(v["mem"]["used"], v["mem"]["total"])),
        "swap": round(core.pct(v["swap"]["used"], v["swap"]["total"])) if v["swap"]["total"] else None,
        "disks": [{"m": d["mount"], "p": round(d["pct"])} for d in v["disks"]],
        "agents": {"total": sum(int(a.get("count", 0)) for a in agents.values() if isinstance(a, dict)),
                   "by": {k: int(a.get("count", 0)) for k, a in agents.items() if isinstance(a, dict) and a.get("count")}},
        "tmux": lv.get("tmux") or live.tmux(),
        "alerts": [{"sev": a["sev"], "msg": a["msg"]} for a in found[:8]],
        "counts": {"crit": sum(1 for a in found if a["sev"] == "crit"),
                   "warn": sum(1 for a in found if a["sev"] == "warn")},
    }
    badge_src = host.cfg.get("fleet.badge")
    if badge_src:
        d = cache.data(str(badge_src))
        if isinstance(d, dict) and d.get("badge"):
            snap["badge"] = str(d["badge"])[:12]
    shared, used = {}, 0
    for name, src in registry.SOURCES.items():
        if not registry.shares(src, host) or registry.scope_of(src, host) != "local" or not registry.applies(src, host):
            continue
        rec = core.read_json(registry.src_path(name))  # this host's own record, never a re-share
        if not isinstance(rec, dict) or rec.get("data") is None:
            continue
        size = len(json.dumps(rec, default=str))
        if used + size > SHARED_MAX_BYTES:
            continue
        used += size
        shared[name] = {k: rec.get(k) for k in ("ts", "ok", "data", "error", "host")}
    if shared:
        snap["shared"] = shared
    return snap


def write(host) -> dict:
    snap = build(host)
    core.write_json_atomic(core.SNAPSHOT_PATH, snap)
    return snap

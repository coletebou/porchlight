"""One ranked alert list from live probes, source records and fleet peers."""
from __future__ import annotations

import re

from . import core, live, registry

SEV_RANK = {"crit": 0, "warn": 1, "info": 2}


def glyph(sev: str):
    """(glyph, color) per severity: the attention strip and `porchlight alerts`."""
    from . import theme as T
    return {"crit": (T.g("crit"), "red"), "warn": (T.g("warn"), "amber")}.get(sev, (T.g("info"), "sky"))


def probe_all(host, vitals=None, repos=()) -> dict:
    """Every live probe the renderer and the snapshot share, run once, in parallel."""
    ignore = tuple(host.cfg.items("alerts.ignore_units"))
    jobs = {"units": lambda: live.units(host), "failed": lambda: live.failed_units(ignore),
            "launchd": lambda: live.launchd(host), "tmux": live.tmux}
    if host.ports:
        jobs["ports"] = lambda: live.ports(host.ports)
    if not vitals:
        jobs["vitals"] = lambda: live.vitals(host)
    for p in repos:
        jobs["repo:" + p] = (lambda p=p: live.repo(p))
    lv = core.parallel(jobs)
    lv["vitals"] = vitals or lv.get("vitals") or live.vitals(host, disks=False)
    found = [lv.pop("repo:" + p, None) for p in repos]
    lv["repos"] = [r for r in found if r]
    return lv


def own(found) -> list:
    """This host's own alerts: live probes plus host-local sources. Excludes
    peer alerts imported from the fleet table and fleet-wide (global) ones."""
    return [a for a in found if a.get("src") == "live" or
            (a.get("src") in registry.SOURCES and registry.SOURCES[a["src"]].scope == "local")]


def _muted(msg: str, patterns) -> bool:
    for p in patterns:
        try:
            if re.search(p, msg, re.I):
                return True
        except re.error:
            if p.lower() in msg.lower():
                return True
    return False


def gather(host, cache, lv) -> list:
    if not isinstance(lv, dict) or "vitals" not in lv:
        lv = probe_all(host, lv)
    v = lv["vitals"]
    th = host.cfg.table("thresholds")
    out = []

    def add(sev, msg, src="live", fix=None):
        a = {"sev": sev, "msg": msg, "src": src}
        if fix:
            a["fix"] = str(fix)[:200]
        out.append(a)

    for d in v.get("disks", []):
        p = d["pct"]
        if p >= th.get("disk_crit", 92):
            add("crit", "disk %s %d%% full on %s" % (d["mount"], p, host.label))
        elif p >= th.get("disk_warn", 85):
            add("warn", "disk %s %d%% on %s" % (d["mount"], p, host.label))
    for m in v.get("stalled") or []:
        add("warn", "mount %s not responding on %s" % (m, host.label))
    memp = core.pct(v["mem"]["used"], v["mem"]["total"])
    if memp >= th.get("mem_crit", 98):
        add("crit", "memory %d%% on %s" % (memp, host.label))
    elif memp >= th.get("mem_warn", 92):
        add("warn", "memory %d%% on %s" % (memp, host.label))
    if v["swap"]["total"]:
        sw = core.pct(v["swap"]["used"], v["swap"]["total"])
        if sw >= th.get("swap_crit", 95):
            add("crit", "swap %d%% on %s" % (sw, host.label))
        elif sw >= th.get("swap_warn", 80):
            add("warn", "swap %d%% on %s" % (sw, host.label))
    load, cores = v["load"][0], v["cores"]
    if load >= th.get("load_crit", 2.0) * cores:
        add("crit", "load %.1f on %d cores (%s)" % (load, cores, host.label))
    elif load >= th.get("load_warn", 1.0) * cores:
        add("warn", "load %.1f on %d cores (%s)" % (load, cores, host.label))
    failed = lv.get("failed") or []
    for u in failed:
        add("crit", "%s failed on %s" % (u, host.label))
    failed_ids = {u.split(":", 1)[-1] for u in failed}
    for label, lvl, detail, ids in lv.get("units") or []:
        if lvl != "ok" and not (set(ids) & failed_ids):  # already reported as a failed unit
            add(lvl, "%s %s on %s" % (label, detail or "down", host.label))
    for label, lvl, detail, _job in lv.get("launchd") or []:
        if lvl != "ok":
            add("warn", "launchd %s %s" % (label, detail))
    # Closed dev ports are state, not alerts: a stopped dev server is a choice.

    for name, src in registry.SOURCES.items():
        if not registry.serves(src, host) or (src.scope != "global" and not registry.applies(src, host)):
            continue
        d = cache.data(name)
        if isinstance(d, dict):
            stale = cache.age(name) > max(6 * src.ttl, 3600)
            for a in d.get("alerts") or []:
                add(a.get("sev", "warn"), a.get("msg", "") + (" (stale)" if stale else ""), name, a.get("fix"))

    # Peers' critical alerts, on whichever host has the fleet table.
    max_age = float(host.cfg.get("alerts.peer_max_age", 1800))
    for peer, rec in cache.fleet_hosts(host.hub).items():
        snap = (rec or {}).get("snap") or {}
        if peer == host.name or core.now() - snap.get("ts", 0) > max_age:
            continue
        label = snap.get("label", peer)
        for a in snap.get("alerts") or []:
            if a.get("sev") != "crit":
                continue
            # Name the peer unless its alert already does ("disk / 93% full on db1").
            msg = a["msg"] if label in a.get("msg", "") else "%s: %s" % (label, a["msg"])
            add("crit", msg, "peer")

    mute = host.cfg.items("alerts.ignore")
    seen, uniq = set(), []
    for a in sorted(out, key=lambda a: SEV_RANK.get(a["sev"], 3)):
        key = a["msg"].replace(" (stale)", "")
        if key in seen or (mute and _muted(a["msg"], mute)):
            continue
        seen.add(key)
        uniq.append(a)
    return uniq

"""Core panels: system, services, agents, network, fleet, repos."""
from __future__ import annotations

import os

from .. import core, live, theme as T
from ..ui import SEP, Panel, cells, fnum, kv, panel


def _thr(x, key, default):
    return float(x.cfg.get("thresholds." + key, default))


@panel("system", title="SYSTEM")
def p_system(x):
    v, cw = x.v, x.cw
    bw = max(8, min(28, cw - 1 - 6 - 24))
    rows = []
    dw, dc = _thr(x, "disk_warn", 85), _thr(x, "disk_crit", 92)

    def meter(label, p, bar, value, warn, crit):
        rows.append(T.dim(label.ljust(6)) + bar + "  " + T.sev("%3d%%" % round(p), T.level_for(p, warn, crit)) + "  " + value)
    load, cores = v["load"][0], v["cores"]
    lp = 100.0 * load / cores
    lw = 100 * _thr(x, "load_warn", 1.0)  # the meter turns amber at 70% of it, red at it
    rows.append(T.dim("cpu".ljust(6)) + T.bar(lp, bw, lw * 0.7, lw) + "  " +
                T.sev(fnum(load), T.level_for(lp, lw * 0.7, lw)) + T.dim(" load · %d cores" % cores))
    m = v["mem"]
    mp = core.pct(m["used"], m["total"])
    mw, mc = _thr(x, "mem_warn", 92), _thr(x, "mem_crit", 98)
    meter("mem", mp, T.bar(mp, bw, mw - 12, mw), T.dim("%s/%s" % (core.human_bytes(m["used"]), core.human_bytes(m["total"]))), mw, mc)
    s = v["swap"]
    if s["total"]:
        sp = core.pct(s["used"], s["total"])
        sw, sc = _thr(x, "swap_warn", 80), _thr(x, "swap_crit", 95)
        meter("swap", sp, T.bar(sp, bw, sw - 20, sw), T.dim("%s/%s" % (core.human_bytes(s["used"]), core.human_bytes(s["total"]))), sw, sc)
    labels = dict({"/": "disk", "C:": "C:"}, **x.host.mount_labels)
    for d in v["disks"]:
        meter(labels.get(d["mount"], os.path.basename(d["mount"].rstrip("/"))[:5] or "disk"), d["pct"],
              T.bar(d["pct"], bw, dw, dc), T.dim("%s/%s" % (core.human_bytes(d["used"]), core.human_bytes(d["total"]))), dw, dc)
    for mnt in v.get("stalled") or []:
        rows.append(T.dim("disk".ljust(6)) + T.c("%s %s not responding" % (T.g("crit"), mnt), "amber"))
    procs = v.get("procs") or (x.cache.data("procs") or {}).get("total")
    tail = []
    if procs:
        tail.append("%s procs" % procs)
    tail.append("up " + core.human_age(v.get("uptime", 0)))
    if "#linux" in x.host.tags and (core.which("systemctl") or x.lv.get("failed")):
        failed = x.lv.get("failed") or []
        tail.append(T.c("%d failed unit%s" % (len(failed), "" if len(failed) == 1 else "s"), "red") if failed
                    else T.c("0 failed units", "green"))
    rows.append(SEP.join(tail))
    lvl = T.worst(*[T.level_for(d["pct"], dw, dc) for d in v["disks"]])
    return Panel("system", "SYSTEM", rows, lvl)


def _remote_login() -> str:
    """macOS Remote Login (sshd): a TCP connect to :22 answers in microseconds."""
    import socket
    try:
        socket.create_connection(("127.0.0.1", 22), timeout=0.1).close()
        return "on"
    except OSError:
        return "off"


@panel("services", title="SERVICES")
def p_services(x):
    items, lvl = [], "ok"
    optional = {str(o) for o in x.cfg.items("services.optional")}
    for label, lv, detail, _ids in (x.lv.get("units") or []) + (x.lv.get("launchd") or []):
        if lv != "ok" and label in optional:
            lv = "unknown"  # an optional service that is down is state, not an alarm
        items.append(T.dot(lv) + " " + (T.c(label, "fg") if lv == "ok" else T.sev(label, lv)) +
                     ((" " + T.dim(detail)) if detail else ""))
        lvl = T.worst(lvl, lv if lv != "unknown" else "ok")
    if "#mac" in x.host.tags:  # no systemd units to show tailscaled and sshd: ask them directly
        ts = x.data("tailscale") or {}
        if ts.get("installed") and not any(i[0] == "tailscale" for i in x.lv.get("launchd") or []):
            ok = ts.get("state") == "Running"
            items.append(T.dot("ok" if ok else "unknown") + " tailscale" + ("" if ok else T.dim(" " + str(ts.get("state")))))
        if x.cfg.get("services.remote_login", True):
            state = _remote_login()
            items.append(T.dot("ok" if state == "on" else "unknown") + " ssh" + ("" if state == "on" else T.dim(" off")))
    for w in ((x.data("procs") or {}).get("watch") or {}).values():
        n, need = w.get("count", 0), w.get("min", 0)
        lv = "ok" if n >= need else w.get("sev", "warn")
        items.append(T.dot(lv) + " " + w.get("label", "?") + " " + T.dim(str(n)))
        lvl = T.worst(lvl, lv)
    rows = cells(items, x.cw - 1, 15)
    failed = x.lv.get("failed") or []
    for u in failed[:4]:
        rows.append(T.c(T.g("crit") + " ", "red") + T.c(u, "red") + T.dim(" failed"))
        lvl = "crit"
    if len(failed) > 4:
        rows.append(T.dim("%s %d more failed units" % (T.ellipsis(), len(failed) - 4)))
    if not rows:
        return None
    return Panel("services", "SERVICES", rows, lvl)


@panel("agents", title="AGENTS")
def p_agents(x):
    pr = x.data("procs") or {}
    rows = []
    ag = pr.get("agents") or {}
    parts = []
    for name, a in sorted(ag.items(), key=lambda kv_: -int((kv_[1] or {}).get("count", 0))):
        if not a.get("count"):
            continue
        sub = a.get("sub") or {}
        txt = "%d %s" % (a["count"], name)
        if len(sub) > 1 or (sub and "tui" not in sub):
            txt += T.dim(" (%s)" % ", ".join("%d %s" % (n, k) for k, n in sorted(sub.items())))
        parts.append(txt)
    if parts:
        rows.append(kv("agents", SEP.join(parts)))
    elif pr:
        rows.append(kv("agents", T.dim("no AI coding agents running")))
    tm = x.lv.get("tmux") or {"sessions": 0, "windows": 0, "attached": 0}
    if tm["sessions"] or parts:
        rows.append(kv("tmux", "%d sessions" % tm["sessions"] + SEP + "%d windows" % tm["windows"] +
                       (T.dim(" (%d attached)" % tm["attached"]) if tm["attached"] else "")))
    lvl = "ok"
    if pr:
        bits = []
        mcp = pr.get("mcp") or {}
        if mcp.get("count"):
            bits.append("%d mcp servers" % mcp["count"])
        orphans = sum(len(a.get("orphans") or []) for a in ag.values()) + int(mcp.get("orphans") or 0)
        if orphans:
            bits.append(T.c("%d orphaned" % orphans, "amber"))
            lvl = "warn"
        bits.append(("%d zombies" % pr.get("zombies", 0)) if pr.get("zombies") else T.dim("0 zombies"))
        rows.append(kv("hygiene", SEP.join(bits)))
        top = pr.get("top") or []
        if top:
            rows.append(kv("top mem", SEP.join("%s %s" % (t["name"], T.dim(core.human_bytes(t["rss"]))) for t in top[:3])))
    if not rows:
        return None
    return Panel("agents", "AGENTS", rows, lvl, x.stale_note("procs", 6, 900))


def _tunnels(x) -> list:
    """ssh port forwards from the process census, re-checked for liveness now."""
    out = []
    for t in (x.data("procs") or {}).get("tunnels") or []:
        t = dict(t)
        t["alive"] = bool(t.get("pid")) and live.alive(int(t["pid"]))
        out.append(t)
    return out


@panel("network", title="NETWORK")
def p_network(x):
    rows, lvl = [], "ok"
    ts = x.data("tailscale") or {}
    if ts.get("installed"):
        running = ts.get("state") == "Running"
        l2 = "ok" if running else ("crit" if ts.get("required") else "unknown")
        lvl = T.worst(lvl, l2)
        nxt = min((r["expiry"] for r in ts.get("rows") or [] if r.get("expiry")), default=0)
        txt = (" %d/%d peers online" % (ts.get("online", 0), ts.get("peers", 0))) if running else (" " + str(ts.get("state") or "?"))
        if nxt and running:
            txt += T.dim(" · next key expiry %s" % core.human_age(nxt - core.now()))
        rows.append(kv("tailscale", T.dot(l2) + txt, 10))
    tun = _tunnels(x)
    fwd = {str(p) for t in tun if t["alive"] and t.get("kind") == "L" for p in t.get("ports") or []}
    items = []
    for t in tun:
        if not t["alive"]:
            continue
        host, port_list = (t.get("host") or "?")[:18], "/".join(t.get("ports") or [])
        if t.get("kind") == "D":
            items.append(T.dot("ok") + " socks" + T.dim(" :%s %s " % (port_list, T.g("via"))) + host)
        elif t.get("kind") == "R":
            items.append(T.dot("ok") + " " + host + T.dim(" ←:%s" % port_list))
        else:
            items.append(T.dot("ok") + " " + host + T.dim(" :%s" % port_list))
    rows += [kv("tunnels" if i == 0 else "", it, 10) for i, it in enumerate(cells(items, x.cw - 11, 16))]
    ports = x.lv.get("ports") or []
    if ports:
        items = []
        for port, label, is_open in ports:
            via = T.c(" " + T.g("via"), "sky") if str(port) in fwd else ""
            items.append(T.dot("ok" if is_open else "unknown") + " " + (label if is_open else T.dim(label)) + T.dim(" :%d" % port) + via)
        rows += [kv("ports" if i == 0 else "", it, 10) for i, it in enumerate(cells(items, x.cw - 11, 16))]
    if not rows:
        return None
    return Panel("network", "NETWORK", rows, lvl, x.stale_note("tailscale", 4, 1800))


def fleet_table(x) -> dict:
    """{host: {"snap", "seen", "ok"}} including this host from live probes."""
    from ..alerts import own as own_alerts
    mine = own_alerts(x.alerts, x.host)  # counted like the peers' snapshots: this host's own alerts only
    table = dict(x.cache.fleet_hosts(x.host.hub))
    own = dict(core.read_json(core.SNAPSHOT_PATH) or {})
    own.update({"host": x.host.name, "label": x.host.label, "ts": core.now(),
                "load": round(x.v["load"][0], 2), "cores": x.v["cores"],
                "mem": round(core.pct(x.v["mem"]["used"], x.v["mem"]["total"])),
                "disks": [{"m": d["mount"], "p": round(d["pct"])} for d in x.v["disks"]],
                "counts": {"crit": sum(1 for a in mine if a["sev"] == "crit"),
                           "warn": sum(1 for a in mine if a["sev"] == "warn")}})
    table[x.host.name] = {"ok": True, "seen": core.now(), "snap": own}
    return table


@panel("fleet", title="FLEET")
def p_fleet(x):
    table = fleet_table(x)
    if len(table) <= 1 and not x.host.hub:
        return None  # a lone machine has no fleet to show
    order = [str(n) for n in x.cfg.items("fleet.order")]
    labels = x.cfg.table("fleet.labels")
    ts_rows = {r["name"]: r for r in (x.data("tailscale") or {}).get("rows") or []}
    names = [n for n in order if n in table] + sorted(n for n in table if n not in order)
    hdr = T.dim("%-11s %5s %4s %4s  %-9s %-8s %s" % ("host", "load", "mem", "disk", "agents", "alerts", "seen"))
    rows, lvl = [" " + hdr], "ok"
    dw, dc = _thr(x, "disk_warn", 85), _thr(x, "disk_crit", 92)
    for n in names:
        rec = table.get(n) or {}
        snap = rec.get("snap") or {}
        age = core.now() - (snap.get("ts") or rec.get("seen") or 0)
        tsr = ts_rows.get(n) or ts_rows.get((snap.get("label") or "").lower()) or {}
        online = None  # None: Tailscale has no row for this host
        if tsr:
            # Tailscale's Online bit flaps during control-plane reconnects: trust
            # "offline" only when the node has also not been seen for 10 minutes.
            online = tsr.get("online") or core.now() - (tsr.get("seen") or 0) < 600
        label = str(labels.get(n) or snap.get("label") or n)[:11]
        if not snap or age > 1800:
            state = "crit" if online is False else "unknown"
            err = rec.get("error") or ""
            if online is False:
                why = "offline"
            elif not snap and rec.get("ok"):
                why = "reachable · porchlight not installed yet"
            elif err:
                why = err[:40]
            else:
                why = "no data"
            rows.append(T.dot(state) + " " + T.pad(label, 10) + " " + T.dim(why + ((" · " + core.human_age(age) + " ago") if snap else "")))
            lvl = T.worst(lvl, "warn" if state == "crit" else "ok")
            continue
        cnt = snap.get("counts") or {}
        nc, nw = cnt.get("crit", 0), cnt.get("warn", 0)
        state = "crit" if nc else "warn" if nw else "ok"
        lvl = T.worst(lvl, state)
        load, cores = snap.get("load", 0), snap.get("cores") or 1
        disk = max((d.get("p", 0) for d in snap.get("disks") or []), default=0)
        ag = snap.get("agents") or {}
        if snap.get("badge"):
            agents = str(snap["badge"])[:9]
        elif ag.get("total"):
            agents = " ".join("%d%s" % (n_, k[:2]) for k, n_ in sorted((ag.get("by") or {}).items(), key=lambda i: -i[1])[:2])
        else:
            agents = T.dim("-")
        badges = []
        if nc:
            badges.append(T.c("%d%s" % (nc, T.g("crit")), "red"))
        if nw:
            badges.append(T.c("%d%s" % (nw, T.g("warn")), "amber"))
        rows.append(T.dot(state) + " " + T.pad(T.c(label, "fg"), 10) + " " +
                    T.pad(T.sev(fnum(load), T.level_for(100 * load / cores, 70, 100)), 5, ">") + " " +
                    T.pad(T.sev("%d%%" % snap.get("mem", 0), T.level_for(snap.get("mem", 0), 85, 95)), 4, ">") + " " +
                    T.pad(T.sev("%d%%" % disk, T.level_for(disk, dw, dc)), 4, ">") + "  " +
                    T.pad(agents, 9) + " " + T.pad(" ".join(badges) or T.c("ok", "green"), 8) + " " +
                    T.dim("now" if age < 90 else core.human_age(age)))
    if x.host.hub:
        note = x.stale_note("fleet", 4, 900)
    elif x.cache.bundle:
        note = "via %s · %s" % (x.cache.bundle.get("from", "hub"), core.human_age(core.now() - (x.cache.bundle.get("ts") or 0)))
    else:
        note = "no hub bundle"
    return Panel("fleet", "FLEET", rows, lvl, note)


def repo_rows(r, label: str, untracked: int = 0) -> list:
    bits = [(T.c("%d modified" % r["dirty"], "amber") if r["dirty"] else T.c("clean", "green"))]
    if untracked:
        bits.append(T.c("%d untracked" % untracked, "slate"))
    if r["ahead"]:
        bits.append(T.c(T.g("up") + "%d" % r["ahead"], "sky"))
    if r["behind"]:
        bits.append(T.c(T.g("down") + "%d" % r["behind"], "rose"))
    if r.get("last"):
        bits.append(T.dim(core.human_age(core.now() - r["last"], " ago")))
    return [T.c(label, "fg") + T.dim(" @ ") + T.c(r["branch"], "violet") + "  " + SEP.join(bits)]


@panel("repos", title="REPOS")
def p_repos(x):
    rows = []
    untracked = (x.data("repos") or {}).get("repos") or {}
    for r in x.lv.get("repos") or []:
        rows += repo_rows(r, r["path"].replace(core.HOME, "~", 1), (untracked.get(r["path"]) or {}).get("untracked", 0))
    return Panel("repos", "REPOS", rows) if rows else None

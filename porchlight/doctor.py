"""`porchlight doctor`: is every source fresh, and if not, why."""
from __future__ import annotations

import os

from . import __version__, core, hosts, registry, theme as T
from .collect import is_due


def timer_state() -> str:
    if core.IS_MAC:
        rc, out, _ = core.run(["/bin/launchctl", "list", "dev.porchlight.collect"], 2)
        if rc != 0:
            return T.c("launchd agent dev.porchlight.collect not loaded · porchlight install timer", "amber")
        m = [ln for ln in out.splitlines() if "LastExitStatus" in ln]
        return T.c("launchd dev.porchlight.collect loaded", "green") + (T.dim(" · " + m[0].strip().rstrip(";")) if m else "")
    if not core.which("systemctl"):
        return T.c("no systemd: run `porchlight collect --quiet` from cron every 5 minutes", "amber")
    rc, out, _ = core.run(["systemctl", "--user", "show", "porchlight-collect.timer",
                           "-p", "ActiveState,LastTriggerUSec"], 3)
    props = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    if props.get("ActiveState") != "active":
        return T.c("systemd porchlight-collect.timer %s · porchlight install timer" % (
            props.get("ActiveState") or "missing"), "amber")
    return T.c("systemd porchlight-collect.timer active", "green") + T.dim(" · last %s" % props.get("LastTriggerUSec", "?"))


def run(host, cache, plugin_errors=()) -> int:
    from . import secrets
    now = core.now()
    cfg = host.cfg
    print(T.c("porchlight doctor", "gold", bold=True) + T.dim("  ·  %s (%s · %s%s)" % (
        host.label if host.label == host.name else "%s = %s" % (host.label, host.name),
        host.identity, host.role, " · hub of %d" % len(host.peers) if host.hub else "")))
    print(T.dim("config ") + (cfg.path if cfg.exists else T.c("%s (not found: defaults · porchlight init)" % cfg.path, "amber")) +
          (T.dim("  ·  matched hosts: ") + ", ".join(cfg.matched) if cfg.matched else ""))
    print(T.dim("cache  ") + core.CACHE_DIR + T.dim("  ·  color ") + T.MODE + (T.dim(" · ascii") if T.ASCII else ""))
    for p in list(cfg.problems) + list(plugin_errors) + registry.check_settings(cfg):
        print(T.c("%s %s" % (T.g("warn"), p), "amber"))
    print(timer_state())
    # docs/fleet-consistency.md: the fleet-wide half, its source, and its age.
    own_shared = [str(p) for p in cfg.items("fleet.shared_panels")]
    b = cache.bundle
    if own_shared:
        shared_from = "this host's fleet.shared_panels (%d panel%s)" % (len(own_shared), "" if len(own_shared) == 1 else "s")
    elif hosts.bundle_ok(b) and (b.get("layout") or {}).get("shared_panels"):
        n = len(b["layout"]["shared_panels"])
        shared_from = "the hub's layout.shared_panels (%d panel%s)" % (n, "" if n == 1 else "s")
    else:
        shared_from = "none"
    print(T.dim("fleet shared panels from %s" % shared_from))
    if not host.hub:
        if not b:
            print(T.dim("fleet bundle: none yet"))
        elif not hosts.bundle_ok(b):
            print(T.c("fleet bundle: v%r ignored (unsupported version, treated as no bundle)" % b.get("v"), "amber"))
        else:
            age = hosts.bundle_age(b)
            stale = hosts.bundle_stale(host, b)
            line = "fleet bundle: v%s from %s, %s old%s" % (b.get("v"), b.get("from"), core.human_age(age),
                                                              " · STALE (threshold %s)" % core.human_age(hosts.stale_after(host, b)) if stale else "")
            theirs = b.get("porchlight")
            skew = T.dim("  ·  hub porchlight %s vs ours %s" % (theirs, __version__)) if theirs and theirs != __version__ else ""
            print((T.c(line, "amber") if stale else T.dim(line)) + skew)
    print()
    print(T.dim("%-16s %-6s %6s %8s  %-5s %7s  %s" % ("source", "scope", "ttl", "age", "state", "ms", "note")))
    env = None
    off = []
    for name in sorted(registry.SOURCES):
        s = registry.SOURCES[name]
        why = registry.state(s, host)
        if why != "on":
            off.append((name, why))
            continue
        local = core.read_json(registry.src_path(name))
        rec = cache.rec(name)
        origin = "hub" if rec and rec != local else "local"
        age = now - rec.get("ts", 0) if rec else None
        if rec is None:
            state, color = "none", "dim"
        elif rec.get("ok"):
            state, color = "ok", "green"
        elif rec.get("skip"):
            state, color = "skip", "slate"
        else:
            state, color = "FAIL", "red"
        note = (rec or {}).get("error") or ""
        ms = (rec or {}).get("ms") or 0
        if not note and origin == "local" and ms > s.timeout * 1000:
            note = "slow: %.1fs > %ss budget" % (ms / 1000.0, s.timeout)
        elif not note:
            note = s.desc
        if s.needs and state != "ok":
            env = env or secrets.load(cfg)
            miss = [k for k in s.needs if not env.get(k)]
            if miss:
                note = "missing " + ", ".join(miss)
        due = is_due(s, host, now, cache)
        print("%-16s %-6s %6s %8s  %s %7s  %s" % (
            name[:16], registry.scope_of(s, host) if origin == "local" else "hub", core.human_age(s.ttl),
            core.human_age(age) if age is not None else "-", T.pad(T.c(state, color), 5),
            ms if origin == "local" and rec else "", T.dim(note[:96]) + (T.c(" due", "sky") if due else "")))
    if off:
        print()
        groups = {}
        for name, why in off:
            groups.setdefault(why, []).append(name)
        for why, names in sorted(groups.items()):
            print(T.dim("off (%s): " % why) + ", ".join(names))
    if os.path.exists(core.LOG_PATH):
        print()
        print(T.dim("last collector runs:"))
        for line in core.read_text(core.LOG_PATH).splitlines()[-4:]:
            print("  " + T.dim(line[:200]))
    return 0

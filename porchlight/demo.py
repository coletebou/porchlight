"""`porchlight demo` and `porchlight themes`: a fictional fleet, no setup needed.

The demo writes sample records into a scratch cache, swaps the live probes for
fixed readings, and renders the real dashboard code path: what you see is what
your own machines will look like once their sources have data.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile

from . import core

DEMO_CONFIG = """
repos = ["~/code/webapp", "~/code/infra"]
ports = [{ port = 5173, label = "vite" }, { port = 8000, label = "api" }, { port = 8080, label = "admin" }]

[general]
label = "atlas"
identity = "personal"
role = "server"
clock_secondary = "Europe/London"
hints = ["alerts", "doctor", "all"]

[layout]
panels = "auto"

[fleet]
peers = ["web-1", "db-1", "gpu-box", "nas"]
order = ["atlas", "web-1", "db-1", "gpu-box", "nas"]

[sources.procs]
watch = [{ name = "queue-worker", match = "queue-worker", min = 2 }]

[sources.sites]
targets = [{ label = "example.com", url = "https://example.com" }]

[sources.health]
targets = [{ label = "api", url = "http://127.0.0.1:8000/health" }]

[sources.certs]
targets = ["example.com"]

[sources.files]
targets = [{ path = "~/CLAUDE.md", max_bytes = 40000 }]

[sources.backups]
units = ["restic-home"]

[sources.drift]
targets = [{ repo = "~/code/webapp", file = "/srv/webapp/REVISION" }]

[[custom]]
name = "restic"
title = "RESTIC"
command = "true"
"""


def _core_fixtures(now: float) -> dict:
    pid = os.getpid()  # alive for as long as the demo runs, so tunnels render as up

    def snap(label, load, cores, mem, disk, crit=0, warn=0, agents=None, badge=None, age=40):
        s = {"v": 1, "host": label, "label": label, "ts": now - age, "load": load, "cores": cores, "mem": mem,
             "disks": [{"m": "/", "p": disk}], "agents": {"total": sum((agents or {}).values()), "by": agents or {}},
             "counts": {"crit": crit, "warn": warn}, "alerts": []}
        if badge:
            s["badge"] = badge
        if crit:
            s["alerts"].append({"sev": "crit", "msg": "GPU 0 at 91°C on %s" % label})
        return {"ok": True, "seen": now - age, "snap": s}
    return {
        "procs": {"total": 412, "zombies": 0,
                  "agents": {"claude": {"count": 3, "orphans": []},
                             "codex": {"count": 2, "orphans": [], "sub": {"app-server": 1, "tui": 1}},
                             "aider": {"count": 1, "orphans": []}},
                  "mcp": {"count": 6, "orphans": 1},
                  "top": [{"name": "postgres", "rss": 1.3e9}, {"name": "node", "rss": 9.1e8}, {"name": "claude", "rss": 4.4e8}],
                  "watch": {"queue-worker": {"label": "queue-worker", "count": 2, "min": 2, "sev": "warn"}},
                  "tunnels": [{"kind": "L", "ports": ["5173"], "host": "devbox", "pid": pid},
                              {"kind": "D", "ports": ["1080"], "host": "bastion", "pid": pid}]},
        "tailscale": {"installed": True, "state": "Running", "ip": "100.64.0.7", "online": 9, "peers": 11,
                      "rows": [{"name": "web-1", "online": True, "seen": now, "expiry": now + 9 * 86400},
                               {"name": "db-1", "online": True, "seen": now, "expiry": 0},
                               {"name": "gpu-box", "online": True, "seen": now, "expiry": 0},
                               {"name": "nas", "online": False, "seen": now - 7200, "expiry": 0}]},
        "weather": {"icon": "☀️", "desc": "clear", "temp": 68, "unit": "°F", "hi": 72, "lo": 55, "rain": 10,
                    "sunset": "18:42", "src": "open-meteo"},
        "quotes": {"items": ["There are only two hard things in computer science: cache invalidation and naming things. — Phil Karlton"]},
        "sites": {"rows": [{"label": "example.com", "url": "https://example.com", "status": 200, "ms": 212, "up": True},
                           {"label": "docs", "url": "https://docs.example.com", "status": 200, "ms": 340, "up": True},
                           {"label": "status", "url": "https://status.example.com", "status": 200, "ms": 188, "up": True},
                           {"label": "shop", "url": "https://shop.example.com", "status": 200, "ms": 1720, "up": True,
                            "slow": True}], "up": 4, "total": 4},
        "health": {"rows": [{"label": "api", "status": 200, "ms": 9, "up": True, "info": "v2.4.1"},
                            {"label": "worker", "status": 200, "ms": 6, "up": True},
                            {"label": "search", "status": 0, "ms": 0, "up": False, "error": "refused"}],
                   "up": 2, "total": 3,
                   "alerts": [{"sev": "crit", "msg": "search health down (refused) on atlas"}]},
        "certs": {"rows": [{"name": "example.com", "days": 64.2, "ok": True}, {"name": "api.example.com", "days": 12.4, "ok": False}],
                  "alerts": [{"sev": "warn", "msg": "TLS cert api.example.com expires in 12d"}]},
        "backups": {"rows": [{"name": "restic-home", "ok": True, "last": now - 5 * 3600, "next": now + 19 * 3600, "result": "success"},
                             {"name": "db-dump", "ok": True, "last": now - 3600, "next": now + 3 * 3600, "result": "success"},
                             {"name": "photos", "ok": False, "last": now - 4 * 86400, "next": 0, "result": "success"}],
                    "alerts": [{"sev": "warn", "msg": "backup photos overdue (4d ago)"}]},
        "files": {"files": 3, "rows": [
            {"label": "AGENTS.md", "bytes": 36400, "lines": 512, "pct": 91.0, "max_bytes": 40000, "max_lines": 0, "mtime": now - 7200},
            {"label": "CLAUDE.md", "bytes": 24800, "lines": 301, "pct": 62.0, "max_bytes": 40000, "max_lines": 0, "mtime": now - 86400},
            {"label": "MEMORY.md", "bytes": 11520, "lines": 88, "pct": 48.0, "max_bytes": 24000, "max_lines": 190, "mtime": now - 600}],
            "alerts": [{"sev": "warn", "msg": "AGENTS.md at 91% of its budget"}]},
        "drift": {"rows": [{"label": "webapp", "ok": False, "deployed": "3a4f9c1", "want": "9be02d7", "behind": 2, "ref": "origin/main"},
                           {"label": "infra", "ok": True, "deployed": "c0ffee1", "want": "c0ffee1", "behind": 0, "ref": "origin/main"}]},
        "repos": {"repos": {os.path.join(core.HOME, "code/webapp"): {"untracked": 2, "changed": 3}}},
        "restic": {"status": "ok", "summary": "last snapshot 5h ago · 412 GiB · 0 errors",
                   "rows": [{"label": "repository", "value": "b2:acme-backups", "status": "ok"},
                            {"label": "snapshots", "value": "1,284 (30 daily · 12 monthly)"}]},
        "fleet": {"hosts": {"web-1": snap("web-1", 0.42, 4, 41, 63, badge="12/12"),
                            "db-1": snap("db-1", 1.21, 8, 78, 88, warn=1),
                            "gpu-box": snap("gpu-box", 7.93, 8, 92, 71, crit=1, agents={"claude": 2, "codex": 1}),
                            "nas": {"ok": False, "seen": now - 7200, "error": "ssh timeout", "fails": 3}}},
    }


def _relative(v, now):
    """Demo files write times as "+N"/"-N" seconds from now, so the sample data
    never ages: resolve them at load."""
    if isinstance(v, dict):
        return {k: _relative(x, now) for k, x in v.items()}
    if isinstance(v, list):
        return [_relative(x, now) for x in v]
    if isinstance(v, str) and len(v) > 1 and v[0] in "+-" and v[1:].isdigit():
        return now + int(v)
    return v


def _patch_live(now: float):
    from . import live, theme as T

    def vitals(host, disks=True):
        return {"cores": 12, "ip": "192.168.1.20", "load": [3.18, 2.9, 2.4], "os": "Debian GNU/Linux 12 (bookworm)",
                "mem": {"used": 21.4 * 2 ** 30, "total": 32 * 2 ** 30}, "swap": {"used": 1.1 * 2 ** 30, "total": 4 * 2 ** 30},
                "uptime": 9 * 86400 + 4 * 3600, "procs": 412, "stalled": [],
                "disks": [{"mount": "/", "used": 290 * 2 ** 30, "total": 476 * 2 ** 30, "pct": 61.0},
                          {"mount": "/srv", "used": 3.3 * 2 ** 40, "total": 4 * 2 ** 40, "pct": 83.0}] if disks else []}

    def repo(path):
        p = core.expand(path)
        if p.endswith("webapp"):
            return {"path": p, "branch": "main", "ahead": 1, "behind": 0, "dirty": 3, "last": now - 2 * 3600}
        return {"path": p, "branch": "main", "ahead": 0, "behind": 2, "dirty": 0, "last": now - 3 * 86400}
    live.vitals = vitals
    live.units = lambda host: [("nginx", "ok", "", ["nginx.service"]), ("docker", "ok", "", ["docker.service"]),
                               ("postgres", "ok", "", ["postgresql.service"]), ("runners", "ok", "3/3", []),
                               ("caddy", "ok", "%s4" % T.g("reset"), ["caddy.service"]), ("tailscale", "ok", "", ["tailscaled.service"])]
    live.failed_units = lambda ignore=(): ["photos-sync.service"]
    live.launchd = lambda host: []
    live.tmux = lambda: {"sessions": 3, "windows": 11, "attached": 1}
    live.ports = lambda items: [(5173, "vite", True), (8000, "api", True), (8080, "admin", False)]
    live.repo = repo


def build(theme=None, keep=False):
    """Write the demo cache and config; returns (host, cache, tmpdir)."""
    from . import config, hosts, registry, theme as T
    tmp = tempfile.mkdtemp(prefix="porchlight-demo-")
    core.use_cache_dir(os.path.join(tmp, "cache"))
    core.ensure_dirs()
    now = core.now()
    data = _core_fixtures(now)
    for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "demo", "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                extra = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(extra, dict):
            data.update({k: _relative(v, now) for k, v in extra.items() if not k.startswith("_")})
    ages = {"fleet": 60, "procs": 45, "weather": 900, "quotes": 86400}
    for name, d in data.items():
        core.write_json_atomic(registry.src_path(name), {"v": 1, "name": name, "ts": now - ages.get(name, 120),
                                                         "ok": True, "data": d, "ms": 120})
    cfg_path = os.path.join(tmp, "config.toml")
    with open(cfg_path, "w") as f:
        f.write(DEMO_CONFIG)
    if theme:
        os.environ["PORCHLIGHT_THEME"] = theme
    cfg = config.load(cfg_path, host="atlas", fresh=True)
    T.setup(cfg)
    host = hosts.current(cfg)
    host.tags = (host.tags - {"#mac", "#wsl"}) | {"#linux"}  # the demo machine is a Linux server wherever it runs
    _patch_live(now)
    core.write_json_atomic(os.path.join(core.STATE_DIR, "last_visit.json"),
                           {"ts": now - 3 * 3600, "alerts": ["photos-sync.service failed on atlas"]})
    from . import snapshot, sources
    sources.register_all(host)
    snapshot.write(host)  # what the collector would have written: the fleet row for this host
    return host, registry.Cache(), tmp


def run(flags, opts, words) -> int:
    from . import render, theme as T
    if opts.get("--color"):
        T.set_mode(opts["--color"])
    host, cache, tmp = build(opts.get("--theme"), keep="--keep" in flags)
    if "--ascii" in flags:
        T.set_ascii(True)
    try:
        width = int(opts["--width"]) if (opts.get("--width") or "").isdigit() else None
        only = [w for w in words if not w.startswith("-")] or None
        compact = True if "--compact" in flags else (False if "--full" in flags else None)
        print(render.render(host, cache, width=width, only=only, compact=compact))
    finally:
        if "--keep" in flags:
            print("demo cache kept in %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


def themes(opts) -> int:
    """Every theme: its name, a gradient sample, severity colors and a meter."""
    from . import font, render, theme as T
    names = [opts["--theme"]] if opts.get("--theme") else sorted(T.THEMES)
    for name in names:
        T.configure(name)
        art = render.paint(font.render(name, "small"), T.ACCENT)
        facts = [T.c(name, T.gradient(T.ACCENT, 0.5), bold=True) + T.dim("   [theme] name = \"%s\"" % name),
                 "  ".join(T.dot(s) + " " + T.sev(s, s) for s in ("ok", "warn", "crit", "info", "unknown")),
                 T.bar(35, 10) + " " + T.bar(78, 10) + " " + T.bar(96, 10) + "  " + T.c("fg", "fg") + " " +
                 T.c("text2", "text2") + " " + T.dim("dim") + " " + T.c("violet", "violet") + " " + T.c("sky", "sky") +
                 " " + T.c("rose", "rose") + " " + T.c("slate", "slate")]
        width = max(T.width(a) for a in art)
        for i in range(max(len(art), len(facts))):
            print(" " + T.pad(art[i] if i < len(art) else "", width) + "   " + (facts[i] if i < len(facts) else ""))
        print()
    print(T.dim("accents (any theme): ") + ", ".join(sorted(T.ACCENTS)) + T.dim("   [theme] accent = \"ember\""))
    return 0

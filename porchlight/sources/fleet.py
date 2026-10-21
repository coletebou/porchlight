"""Hub ⇄ peer exchange over your existing SSH config (no new service or port).

One connection per peer per cycle: the hub writes its bundle (global source
records plus the fleet table) to the peer's <cache>/hub/bundle.json on stdin,
and the same command prints the peer's own snapshot.json back. Peers therefore
render the account-wide panels and the fleet table without a single API call
of their own; if the hub goes quiet they fall back after a while (see
registry.Source.fallback).
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time

from .. import core
from ..registry import SOURCES, source, src_path


def remote_command(cache_dir: str = "") -> str:
    d = shlex.quote(cache_dir) if cache_dir else '"${XDG_CACHE_HOME:-$HOME/.cache}/porchlight"'
    if cache_dir.startswith("~/"):
        d = '"$HOME"/' + shlex.quote(cache_dir[2:])
    return ("umask 077; d=%s; mkdir -p \"$d/hub\" && cat > \"$d/hub/.bundle.tmp\" && "
            "mv -f \"$d/hub/.bundle.tmp\" \"$d/hub/bundle.json\"; cat \"$d/snapshot.json\" 2>/dev/null || true" % d)


def build_bundle(host, peers_prev: dict) -> dict:
    srcs = {}
    from ..registry import scope_of
    for name, s in SOURCES.items():
        if scope_of(s, host) == "global":
            rec = core.read_json(src_path(name))
            if rec:
                srcs[name] = rec
    hosts = dict(peers_prev)
    own = core.read_json(core.SNAPSHOT_PATH)
    if own:
        hosts[host.name] = {"ok": True, "seen": own.get("ts", core.now()), "snap": own}
    return {"v": 1, "from": host.name, "ts": core.now(), "sources": srcs, "fleet": {"hosts": hosts}}


def exchange(peer: str, payload: bytes, cfg) -> dict:
    ssh = core.which("ssh") or "/usr/bin/ssh"
    connect = int(cfg.get("fleet.connect_timeout", 6))
    timeout = float(cfg.get("fleet.push_timeout", 25))
    argv = [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % connect, "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2", "-o", "LogLevel=ERROR"]
    for opt in cfg.items("fleet.ssh_options"):
        argv += ["-o", str(opt)]
    argv += [peer, remote_command(str(cfg.get("fleet.remote_cache") or ""))]
    t0 = time.monotonic()
    try:
        p = subprocess.run(argv, input=payload, capture_output=True, timeout=timeout)
        ms = int((time.monotonic() - t0) * 1000)
    except subprocess.TimeoutExpired:
        return {"ok": False, "unreachable": True, "error": "ssh timeout", "ms": int(timeout * 1000)}
    except OSError as e:
        return {"ok": False, "unreachable": True, "error": str(e)[:120], "ms": 0}
    if p.returncode != 0:  # 255: ssh itself failed; anything else: the remote shell did
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "unreachable": p.returncode == 255, "ms": ms,
                "error": (err[-1] if err else "ssh rc %d" % p.returncode)[:160]}
    try:
        snap = json.loads(p.stdout.decode("utf-8", "replace") or "null")
    except ValueError:
        snap = None
    return {"ok": True, "snap": snap if isinstance(snap, dict) else None, "ms": ms,
            "error": None if isinstance(snap, dict) else "peer has no snapshot yet"}


@source("fleet", ttl=240, timeout=30, hosts=("#hub",))
def fleet(ctx):
    """Swap bundle and snapshot with every peer (hub only)."""
    prev_hosts = (ctx.prev or {}).get("hosts") or {}
    carry = {k: v for k, v in prev_hosts.items() if k != ctx.host.name and k in ctx.host.peers}
    payload = json.dumps(build_bundle(ctx.host, carry), separators=(",", ":"), default=str).encode()
    from concurrent.futures import ThreadPoolExecutor
    peers = ctx.host.peers
    with ThreadPoolExecutor(max_workers=max(1, min(16, len(peers)))) as ex:
        results = dict(zip(peers, ex.map(lambda p: exchange(p, payload, ctx.host.cfg), peers)))
    out, alerts = {}, []
    for peer, res in results.items():
        old = prev_hosts.get(peer) or {}
        if res.get("snap"):
            out[peer] = {"ok": True, "seen": core.now(), "snap": res["snap"], "ms": res["ms"], "fails": 0}
        else:
            fails = (int(old.get("fails", 0)) + 1) if res.get("unreachable") else 0
            out[peer] = {"ok": bool(res.get("ok")), "seen": old.get("seen", 0), "snap": old.get("snap"),
                         "error": res.get("error"), "ms": res.get("ms"), "fails": fails}
            if fails >= 3:
                alerts.append({"sev": "warn", "msg": "%s unreachable over ssh ×%d (%s)" % (peer, fails, res.get("error")),
                               "fix": "ssh %s true" % peer})
    data = {"hosts": out, "bytes": len(payload)}
    if alerts:
        data["alerts"] = alerts
    return data

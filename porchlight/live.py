"""Cheap probes run at render time (and for the fleet snapshot).

Budget: a few ms on Linux (procfs, statvfs, one `systemctl show` per scope) and
about 10 ms on macOS (one sysctl, one vm_stat, one `launchctl list`). Anything
slower belongs in a collector source.
"""
from __future__ import annotations

import fnmatch
import os
import platform
import re
import socket

from . import core, theme as T

# Units shown when [services] lists none: whichever of these exist on the box.
AUTO_UNITS = [
    ("sshd", "sshd"), ("ssh", "sshd"), ("docker", "docker"), ("containerd", "containerd"), ("podman", "podman"),
    ("nginx", "nginx"), ("caddy", "caddy"), ("apache2", "apache"), ("httpd", "apache"), ("haproxy", "haproxy"),
    ("traefik", "traefik"), ("postgresql", "postgres"), ("mysql", "mysql"), ("mariadb", "mariadb"),
    ("redis-server", "redis"), ("redis", "redis"), ("mongod", "mongod"), ("tailscaled", "tailscale"),
    ("cloudflared", "cloudflared"), ("wg-quick@wg0", "wireguard"), ("fail2ban", "fail2ban"),
    ("ufw", "ufw"), ("cron", "cron"), ("crond", "cron"), ("k3s", "k3s"), ("kubelet", "kubelet"),
    ("netdata", "netdata"), ("prometheus", "prometheus"), ("grafana-server", "grafana"),
    ("syncthing@*", "syncthing"),
]
# launchd jobs shown on a Mac when [services] lists none: Homebrew services.
AUTO_LAUNCHD = [("homebrew.mxcl.*", None)]


def _meminfo() -> dict:
    out = {}
    for line in core.read_text("/proc/meminfo").splitlines():
        k, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[k] = int(parts[0]) * 1024
    return out


def _disk(mount: str):
    try:
        st = os.statvfs(mount)
        # A configured mount that is only a directory on / (nothing attached yet)
        # would repeat the root disk. Not on macOS: the Data volume reports the
        # same st_dev as / through its firmlink.
        if not core.IS_MAC and mount != "/" and os.stat(mount).st_dev == os.stat("/").st_dev:
            return None
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    if total <= 0:
        return None
    avail = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    return {"mount": mount, "used": used, "total": used + avail, "pct": core.pct(used, used + avail)}


def primary_ip() -> str:
    """Source address of the default route; a UDP connect sends no packet."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("192.0.2.1", 53))  # TEST-NET-1: routable shape, never answered
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def mac_version() -> str:
    """ProductVersion straight from the plist: platform.mac_ver() imports plistlib
    (~7 ms), more than the rest of the macOS vitals together."""
    txt = core.read_text("/System/Library/CoreServices/SystemVersion.plist", 8192)
    m = re.search(r"<key>ProductVersion</key>\s*<string>([^<]+)</string>", txt)
    return m.group(1) if m else (platform.mac_ver()[0] or "?")


def _os_linux() -> str:
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)', core.read_text("/etc/os-release"), re.M)
    return m.group(1) if m else "Linux"


def _disks(mounts, budget: float = 0.8):
    """statvfs each mount in a daemon thread: a dead network mount (NFS, FUSE,
    CIFS) can block statvfs indefinitely, and a login must never wait on it.
    Returns (disks, stalled mounts)."""
    import threading
    import time
    res = {}
    threads = [threading.Thread(target=lambda m=m: res.__setitem__(m, _disk(m)), daemon=True) for m in mounts]
    for t in threads:
        t.start()
    deadline = time.monotonic() + budget
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return [res[m] for m in mounts if res.get(m)], [m for m in mounts if m not in res]


def vitals(host, disks: bool = True) -> dict:
    v = {"cores": os.cpu_count() or 1, "ip": primary_ip()}
    try:
        v["load"] = list(os.getloadavg())
    except OSError:
        v["load"] = [0.0, 0.0, 0.0]
    if core.IS_MAC:
        rc, out, _ = core.run(["/usr/sbin/sysctl", "-n", "hw.memsize", "vm.swapusage", "kern.boottime"], 2)
        lines = out.splitlines()
        total = int(lines[0]) if lines and lines[0].isdigit() else 0
        sw = re.findall(r"(total|used) = ([\d.]+)M", lines[1] if len(lines) > 1 else "")
        swd = {k: float(x) * 1048576 for k, x in sw}
        bt = re.search(r"sec = (\d+)", lines[2] if len(lines) > 2 else "")
        rc, vm, _ = core.run(["/usr/bin/vm_stat"], 2)
        page = int((re.search(r"page size of (\d+)", vm) or [0, 16384])[1])

        def pages(key):
            return int((re.search(r"%s:\s+(\d+)" % key, vm) or [0, 0])[1])
        # What Activity Monitor calls "memory used": app (active) + wired + compressed.
        used = (pages("Pages active") + pages("Pages wired down") + pages("Pages occupied by compressor")) * page
        v["mem"] = {"used": used, "total": total}
        v["swap"] = {"used": swd.get("used", 0), "total": swd.get("total", 0)}
        v["uptime"] = core.now() - int(bt.group(1)) if bt else 0
        v["os"] = "macOS " + mac_version()
    else:
        mi = _meminfo()
        total = mi.get("MemTotal", 0)
        v["mem"] = {"used": total - mi.get("MemAvailable", mi.get("MemFree", 0)), "total": total}
        v["swap"] = {"used": mi.get("SwapTotal", 0) - mi.get("SwapFree", 0), "total": mi.get("SwapTotal", 0)}
        up = core.read_text("/proc/uptime").split()
        v["uptime"] = float(up[0]) if up else 0
        try:
            v["procs"] = sum(1 for d in os.listdir("/proc") if d.isdigit())
        except OSError:
            pass
        v["os"] = _os_linux()
    mounts = list(host.mounts)
    if core.IS_WSL and "/mnt/c" not in mounts:
        mounts.append("/mnt/c")
    v["disks"], v["stalled"] = _disks(mounts) if disks else ([], [])
    for d in v["disks"]:
        if d["mount"] == "/System/Volumes/Data":
            d["mount"] = "/"
        elif d["mount"] == "/mnt/c" and core.IS_WSL:
            d["mount"] = "C:"
    return v


# ── systemd ─────────────────────────────────────────────────────────────────
def parse_show(out: str) -> list:
    """`systemctl show` output (blank-line separated blocks) → [{Property: value}]."""
    blocks, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        k, _, val = line.partition("=")
        cur[k] = val
    return blocks


def _unit_name(u: str) -> str:
    return u if "." in u.split("@")[-1] else u + ".service"


def _expand_globs(units, user=False) -> dict:
    """{pattern: [unit names]} for glob patterns, via one list-units call."""
    pats = [u for u in units if any(ch in u for ch in "*?[")]
    if not pats:
        return {}
    argv = ["systemctl"] + (["--user"] if user else []) + [
        "list-units", "--all", "--plain", "--no-legend", "--no-pager"] + [_unit_name(p) for p in pats]
    rc, out, _ = core.run(argv, 3)
    names = [line.split()[0] for line in out.splitlines() if line.split()]
    return {p: [n for n in names if fnmatch.fnmatchcase(n, _unit_name(p))] for p in pats}


def _systemctl_show(units, user=False) -> dict:
    """{unit: {Id, ActiveState, SubState, Result, NRestarts, LoadState}} in one call."""
    if not units:
        return {}
    argv = ["systemctl"] + (["--user"] if user else []) + [
        "show", "--no-pager", "-p", "Id,LoadState,ActiveState,SubState,Result,NRestarts"]
    argv += [_unit_name(u) for u in units]
    rc, out, _ = core.run(argv, 3)
    res = {}
    for b in parse_show(out):
        if b.get("Id"):  # configs name units bare ("docker") or in full ("snap.x.y.service")
            res[b["Id"]] = res[b["Id"].rsplit(".", 1)[0]] = b
    return res


def unit_list(host) -> list:
    """(unit, label, scope) to show: the configured ones, else the AUTO_UNITS present."""
    if host.units:
        return host.units
    if not host.auto_services or core.IS_MAC or not core.which("systemctl"):
        return []
    return [(u, label, "system") for u, label in AUTO_UNITS]


def units(host) -> list:
    """[(label, level, detail)] with same-label units merged ("gh-runners 3/3")."""
    wanted = unit_list(host)
    if not wanted or core.IS_MAC:
        return []
    out_units = []
    for scope in ("system", "user"):
        names = [u for u, _, s in wanted if s == scope]
        globs = _expand_globs(names, scope == "user")
        for u, label, s in wanted:
            if s != scope:
                continue
            if u in globs:
                out_units += [(n, label, s) for n in globs[u]]
            else:
                out_units.append((u, label, s))
    st = _systemctl_show([u for u, _, s in out_units if s == "system"])
    st.update({("user:" + k): v for k, v in _systemctl_show([u for u, _, s in out_units if s == "user"], user=True).items()})
    groups, seen = {}, set()  # label → unit states, in config order
    for unit, label, scope in out_units:
        key = ("user:" + unit) if scope == "user" else unit
        info = st.get(key) or st.get(key.rsplit(".", 1)[0]) or {}
        ident = (scope, info.get("Id") or key)
        if ident in seen:  # an alias of a unit already listed (sshd.service → ssh.service)
            continue
        seen.add(ident)
        groups.setdefault(label, []).append(info)
    out = []
    for label, infos in groups.items():
        present = [i for i in infos if i.get("LoadState") not in (None, "", "not-found")]
        if not present:
            continue
        up = sum(1 for i in present if i.get("ActiveState") == "active")
        failed = any(i.get("ActiveState") == "failed" for i in present)
        restarts = sum(int(i.get("NRestarts") or 0) for i in present)
        if up == len(present):
            lvl = "ok"
        elif failed or up == 0:
            lvl = "crit"
        else:
            lvl = "warn"
        # One unit of a same-label group (e.g. sshd vs ssh on different distros).
        if not host.units and up == 0 and not failed:
            continue  # an auto-detected unit that is merely installed is not news
        if len(present) > 1:
            detail = "%d/%d" % (up, len(present))
        elif up:
            detail = ""
        else:
            detail = present[0].get("SubState") or present[0].get("ActiveState") or "?"
        if restarts >= 3 and lvl == "ok":
            detail = (detail + " %s%d" % (T.g("reset"), restarts)).strip()
        out.append((label, lvl, detail, [i.get("Id", "") for i in present]))
    return out


def failed_units(ignore=()) -> list:
    if core.IS_MAC or not core.which("systemctl"):
        return []
    names = []
    for user in (False, True):
        argv = ["systemctl"] + (["--user"] if user else []) + [
            "list-units", "--state=failed", "--no-legend", "--plain", "--no-pager"]
        rc, out, _ = core.run(argv, 3)
        for line in out.splitlines():
            parts = line.split()
            if not parts or parts[0].startswith("session-"):
                continue
            if any(fnmatch.fnmatchcase(parts[0], p) for p in ignore):
                continue
            names.append(("user:" if user else "") + parts[0])
    return names


# ── macOS launchd ───────────────────────────────────────────────────────────
def launchd(host) -> list:
    """Mac launchd jobs: a daemon must have a PID; an interval job a zero last exit."""
    if not core.IS_MAC:
        return []
    wanted = host.launchd or (AUTO_LAUNCHD if host.auto_services else [])
    if not wanted:
        return []
    rc, out, _ = core.run(["/bin/launchctl", "list"], 2)
    table = {}
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            table[parts[2]] = (parts[0], parts[1])
    res, seen = [], set()
    for label, short in wanted:
        if any(ch in label for ch in "*?["):  # every loaded job under a prefix, named by the rest
            prefix = label.split("*", 1)[0]
            jobs = [(lbl, (short + ":" if short else "") + lbl[len(prefix):])
                    for lbl in sorted(table) if fnmatch.fnmatchcase(lbl, label)]
        else:
            jobs = [(label, short or label.rsplit(".", 1)[-1])] if label in table else []
        for job, name in jobs:
            name = name[:16]
            if job in seen:
                continue
            seen.add(job)
            pid, status = table[job]
            if pid != "-":
                res.append((name, "ok", "", job))
            elif status in ("0", "-"):
                res.append((name, "ok", "idle", job))
            else:
                res.append((name, "crit", "exit %s" % status, job))
    return res


# ── tmux, ports, repos ──────────────────────────────────────────────────────
def tmux() -> dict:
    tm = core.which("tmux")
    empty = {"sessions": 0, "windows": 0, "attached": 0}
    if not tm:
        return empty
    rc, out, _ = core.run([tm, "list-windows", "-a", "-F", "#{session_name}\t#{session_attached}"], 2)
    if rc != 0:
        return empty
    rows = [ln.split("\t") for ln in out.splitlines() if ln]
    sess = {r[0]: r[1] for r in rows if len(r) == 2}
    return {"sessions": len(sess), "windows": len(rows),
            "attached": sum(1 for a in sess.values() if a not in ("0", ""))}


def ports(items) -> list:
    """[(port, label, open)] for TCP ports; a refused connect returns at once."""
    out = []
    for port, label, addr in items:
        try:
            socket.create_connection((addr, port), timeout=0.2).close()
            out.append((port, label, True))
        except OSError:
            out.append((port, label, False))
    return out


def alive(pid: int) -> bool:
    """kill -0; EPERM still means the process exists (another user's)."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def repo(path: str):
    """Branch, tracked-dirty count, ahead/behind and last commit age; None if not a repo."""
    path = core.expand(path)
    if not os.path.exists(os.path.join(path, ".git")):
        return None
    rc, out, _ = core.run(["git", "-C", path, "status", "--porcelain=v2", "--branch", "-uno"], 3)
    if rc != 0:
        return None
    r = {"path": path, "branch": "?", "ahead": 0, "behind": 0, "dirty": 0}
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            r["branch"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            a, b = line.split()[2:4]
            r["ahead"], r["behind"] = int(a), -int(b)
        elif not line.startswith("#"):
            r["dirty"] += 1
    rc, last, _ = core.run(["git", "-C", path, "log", "-1", "--format=%ct"], 3)
    r["last"] = int(last.strip()) if last.strip().isdigit() else None
    return r

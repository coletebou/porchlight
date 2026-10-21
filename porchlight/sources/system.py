"""Host sources: process census, Tailscale, repos, quotes, HTTP endpoints,
TLS certificates, backups, file budgets, deploy drift and MongoDB."""
from __future__ import annotations

import json
import os
import re
import socket
import time

from .. import core
from ..registry import source

# ── Process census ──────────────────────────────────────────────────────────
# AI coding agents by name → a regex on the command (before its first " -flag").
AGENTS = {
    "claude": r"(?:^|/)claude(?:\s|$)|@anthropic-ai/claude-code",
    "codex": r"(?:^|/)codex(?:-[a-z0-9_]+)*(?:\s|$)|@openai/codex",
    "aider": r"(?:^|/)aider(?:\s|$)|-m aider(?:\s|$)",
    "gemini": r"(?:^|/)gemini(?:\s|$)|@google/gemini-cli",
    "opencode": r"(?:^|/)opencode(?:\s|$)",
    "goose": r"(?:^|/)goose(?:\s|$)",
    "cursor-agent": r"(?:^|/)cursor-agent(?:\s|$)",
    "amp": r"(?:^|/)amp(?:\s|$)|@sourcegraph/amp",
    "copilot": r"(?:^|/)copilot(?:\s|$)|@github/copilot",
    "qwen": r"(?:^|/)qwen(?:\s|$)|@qwen-code/qwen-code",
    "crush": r"(?:^|/)crush(?:\s|$)",
    "grok": r"(?:^|/)grok(?:\s|$)",
}
# codex runs as several kinds of process; anything else is the interactive TUI.
CODEX_SUBS = ("app-server", "exec", "mcp-server", "proto", "login", "resume")
MCP = re.compile(r"@playwright/mcp|playwright-mcp|mcp-server|@modelcontextprotocol/|(?:^|[/\s])[\w.-]+-mcp(?:\s|$)")
WRAPPERS = ("npm ", "npx ", "sh ", "/bin/sh", "bash ", "/bin/bash", "uv ", "uvx ")


def _etime(e: str) -> int:
    """BSD ps etime "[[dd-]hh:]mm:ss" → seconds."""
    days = 0
    if "-" in e:
        d, e = e.split("-", 1)
        days = int(d) if d.isdigit() else 0
    secs = 0
    for part in e.split(":"):
        secs = secs * 60 + (int(part) if part.isdigit() else 0)
    return days * 86400 + secs


def ps_rows():
    """[(pid, ppid, user, rss_bytes, stat, args, has_tty, age_s)] for every process."""
    rows = []
    if core.IS_MAC:
        rc, out, _ = core.run(["/bin/ps", "-axww", "-o", "pid=,ppid=,user=,rss=,stat=,tty=,etime=,args="], 8)
        for line in out.splitlines():
            parts = line.split(None, 7)
            if len(parts) == 8 and parts[0].isdigit():
                rows.append((int(parts[0]), int(parts[1]), parts[2], int(parts[3]) * 1024, parts[4], parts[7],
                             parts[5] not in ("??", "?"), _etime(parts[6])))
        return rows
    import pwd
    page = os.sysconf("SC_PAGE_SIZE")
    hz = os.sysconf("SC_CLK_TCK")
    up = float((core.read_text("/proc/uptime").split() or ["0"])[0])
    users = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = core.read_text("/proc/%s/stat" % d)
            rpar = stat.rfind(")")
            fields = stat[rpar + 2:].split()
            uid = os.stat("/proc/" + d).st_uid
            with open("/proc/%s/cmdline" % d, "rb") as f:
                args = f.read(4096).replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except (OSError, IndexError):
            continue
        if uid not in users:
            try:
                users[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                users[uid] = str(uid)
        comm = stat[stat.find("(") + 1:rpar]
        rows.append((int(d), int(fields[1]), users[uid], int(fields[21]) * page, fields[0], args or "[%s]" % comm,
                     fields[4] != "0", int(up - int(fields[19]) / hz)))
    return rows


def short_name(args: str) -> str:
    m = re.search(r"/([^/]+)\.app/", args)
    if m:
        return m.group(1)
    first = args.split()[0] if args.split() else "?"
    return os.path.basename(first)[:24]


def parse_tunnel(args: str):
    """An `ssh`/`autossh` command line → {kind: L|R|D, ports, host}, or None.
    Understands clustered flags (-fNL 5173:…), attached values (-D1080) and bind
    addresses (-D 127.0.0.1:1080, -L 127.0.0.1:9000:db:5432)."""
    toks = args.split()
    if not toks or os.path.basename(toks[0]) not in ("ssh", "autossh"):
        return None
    takes_arg = set("BbcDEeFIiJLlMmOoPpQRSWw")
    kind, ports, host, i = None, [], None, 1
    while i < len(toks):
        t = toks[i]
        if t.startswith("-") and len(t) > 1 and t != "--":
            j = 1
            while j < len(t):
                flag = t[j]
                if flag in takes_arg:
                    val = t[j + 1:] or (toks[i + 1] if i + 1 < len(toks) else "")
                    if not t[j + 1:]:
                        i += 1  # the value was the next word
                    if flag in "LRD":
                        kind = kind or flag
                        spec = val.split(":")
                        if flag == "D":
                            port = spec[-1]           # [bind_address:]port
                        elif len(spec) == 4:
                            port = spec[1]            # bind_address:port:host:hostport
                        elif len(spec) == 3:
                            port = spec[0]            # port:host:hostport
                        else:
                            port = ""
                        if port.isdigit():
                            ports.append(port)
                    break
                j += 1
        elif host is None:
            host = t.split("@")[-1]
        i += 1
    if not kind:
        return None
    return {"kind": kind, "ports": ports, "host": host or "?"}


@source("procs", ttl=120, timeout=12, defaults={
    "agents": list(AGENTS), "extra_agents": {}, "orphan_age": 300, "orphan_alert": 1,
    "mcp_orphan_alert": 5, "zombie_alert": 25, "watch": []})
def procs(ctx):
    """Process census: AI coding agents, orphans, MCP servers, zombies, ssh tunnels, top memory."""
    cfg = ctx.cfg
    names = [n for n in cfg.get("agents") or [] if n in AGENTS]
    matchers = {n: re.compile(AGENTS[n]) for n in names}
    for n, rx in (cfg.get("extra_agents") or {}).items():
        try:
            matchers[str(n)] = re.compile(str(rx))
        except re.error:
            pass
    watch = []
    for w in cfg.get("watch") or []:
        if isinstance(w, dict) and w.get("match"):
            watch.append({"label": str(w.get("name") or w["match"])[:16], "match": str(w["match"]),
                          "min": int(w.get("min", 1)), "sev": str(w.get("sev", "warn")), "count": 0})
    rows = ps_rows()
    me = {"total": len(rows), "zombies": 0, "agents": {}, "mcp": {"count": 0, "orphans": 0}, "tunnels": []}
    orphan_age = int(cfg.get("orphan_age", 300))
    for pid, ppid, _user, _rss, stat, args, has_tty, age in rows:
        head = args.split(" -", 1)[0]
        if "Z" in stat:
            me["zombies"] += 1
        if ".app/" in args and "/Contents/MacOS/" in args:
            hit = None  # desktop apps (Claude.app, Codex.app) are not CLI agents
        else:
            # The command before its first flag, plus the first three words for
            # interpreter launches ("python3 -m aider", "node …/claude").
            lead = " ".join(args.split()[:3])
            hit = next((n for n, rx in matchers.items() if rx.search(head) or rx.search(lead)), None)
        if hit and not head.startswith(("node -e", "grep ")):
            a = me["agents"].setdefault(hit, {"count": 0, "orphans": [], "sub": {}})
            a["count"] += 1
            if hit == "codex":
                toks = args.split()
                sub = next((t for t in toks[1:] if not t.startswith("-")), "tui")
                sub = sub if sub in CODEX_SUBS else "tui"
                a["sub"][sub] = a["sub"].get(sub, 0) + 1
            # An orphan: no terminal, reparented to init/launchd, older than orphan_age.
            if not has_tty and ppid == 1 and age > orphan_age and not (hit == "codex" and a["sub"].get("app-server")):
                a["orphans"].append(pid)
        if MCP.search(args) and not args.startswith(WRAPPERS):
            me["mcp"]["count"] += 1
            if ppid == 1 and not has_tty:
                me["mcp"]["orphans"] += 1
        for w in watch:
            if w["match"] in args:
                w["count"] += 1
        tun = parse_tunnel(args)
        if tun:
            tun["pid"] = pid
            me["tunnels"].append(tun)
    for a in me["agents"].values():
        a["orphans"] = a["orphans"][:12]
        if not a["sub"]:
            a.pop("sub")
    top = sorted(rows, key=lambda r: r[3], reverse=True)[:5]
    me["top"] = [{"name": short_name(r[5]), "rss": r[3], "user": r[2]} for r in top]
    if watch:
        me["watch"] = {w["label"]: {k: w[k] for k in ("label", "count", "min", "sev")} for w in watch}
    alerts = []
    for n, a in me["agents"].items():
        if len(a["orphans"]) >= int(cfg.get("orphan_alert", 1)) > 0:
            alerts.append({"sev": "warn", "msg": "%d orphaned %s process%s on %s" % (
                len(a["orphans"]), n, "" if len(a["orphans"]) == 1 else "es", ctx.host.label),
                "fix": "kill %s" % " ".join(str(p) for p in a["orphans"][:6])})
    if me["mcp"]["orphans"] >= int(cfg.get("mcp_orphan_alert", 5)) > 0:
        alerts.append({"sev": "warn", "msg": "%d orphaned MCP server processes" % me["mcp"]["orphans"]})
    if me["zombies"] >= int(cfg.get("zombie_alert", 25)) > 0:
        alerts.append({"sev": "warn", "msg": "%d zombie processes" % me["zombies"]})
    for w in watch:
        if w["count"] < w["min"]:
            alerts.append({"sev": w["sev"] if w["sev"] in ("warn", "crit", "info") else "warn",
                           "msg": "%s: %d running (want %d) on %s" % (w["label"], w["count"], w["min"], ctx.host.label)})
    if alerts:
        me["alerts"] = alerts
    return me


# ── Tailscale ───────────────────────────────────────────────────────────────
WIN_TS = "/mnt/c/Program Files/Tailscale/tailscale.exe"
MAC_TS = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def tailscale_bin(host=None):
    # WSL with mirrored networking shares the Windows node, and its own tailscaled
    # (if any) sits logged out: ask the Windows CLI.
    if host is not None and "#wsl" in host.tags and os.path.exists(WIN_TS):
        return WIN_TS
    return core.which("tailscale") or (MAC_TS if os.path.exists(MAC_TS) else None)


def _ts_epoch(stamp) -> int:
    """A tailscale status timestamp → epoch secs; Go's zero time ("0001-…") means never."""
    return core.utc_epoch(stamp) if stamp and not str(stamp).startswith("0001") else 0


@source("tailscale", ttl=300, timeout=8, defaults={"required": False, "expiry_warn_days": 14})
def tailscale(ctx):
    """Local tailscaled: this node's IP, backend state, peers online, key expiry."""
    ts = tailscale_bin(ctx.host)
    if not ts:
        return {"installed": False}
    rc, out, err = core.run([ts, "status", "--json"], 8)
    try:
        st = json.loads(out)
    except ValueError:
        return {"installed": True, "state": "error", "error": (err or out).strip()[:120] or "no output"}
    selfn = st.get("Self") or {}
    peers = list((st.get("Peer") or {}).values())
    now = core.now()
    warn_s = float(ctx.cfg.get("expiry_warn_days", 14)) * 86400
    rows = [{"name": (p.get("HostName") or p.get("DNSName", "?").split(".")[0]).lower(),
             "online": bool(p.get("Online")), "seen": _ts_epoch(p.get("LastSeen", "")),
             "expiry": _ts_epoch(p.get("KeyExpiry", "")), "os": p.get("OS", "")} for p in peers]
    soon = [r for r in rows if r["expiry"] and r["expiry"] - now < warn_s]
    self_exp = _ts_epoch(selfn.get("KeyExpiry", ""))
    data = {"installed": True, "state": st.get("BackendState"), "ip": (selfn.get("TailscaleIPs") or [""])[0],
            "online": sum(1 for r in rows if r["online"]), "peers": len(rows), "rows": rows,
            "self_expiry": self_exp, "required": bool(ctx.cfg.get("required"))}
    alerts = []
    # `tailscale status --json` exits 0 even when stopped: judge BackendState.
    if data["state"] != "Running" and ctx.cfg.get("required"):
        alerts.append({"sev": "crit", "msg": "tailscale %s on %s" % (data["state"], ctx.host.label),
                       "fix": "sudo tailscale up"})
    if self_exp and self_exp - now < warn_s:
        alerts.append({"sev": "warn", "msg": "tailscale key on %s expires in %s" % (
            ctx.host.label, core.human_age(self_exp - now))})
    for r in soon[:3]:
        alerts.append({"sev": "warn", "msg": "tailscale key %s expires in %s" % (r["name"], core.human_age(r["expiry"] - now))})
    if alerts:
        data["alerts"] = alerts
    return data


# ── Repos ───────────────────────────────────────────────────────────────────
@source("repos", ttl=120, timeout=10, hosts=lambda h: bool(h.repos))
def repos(ctx):
    """Untracked-file counts (too slow for the login path; tracked state is live)."""
    out = {}
    for path in ctx.host.repos:
        rc, txt, _ = core.run(["git", "-C", path, "status", "--porcelain", "-unormal"], 8)
        if rc == 0:
            lines = txt.splitlines()
            out[path] = {"untracked": sum(1 for ln in lines if ln.startswith("??")),
                         "changed": sum(1 for ln in lines if not ln.startswith("??"))}
    return {"repos": out}


# ── Quotes ──────────────────────────────────────────────────────────────────
@source("quotes", ttl=7 * 86400, timeout=15, scope="global", retry=6 * 3600,
        defaults={"zenquotes": True, "fortune": True, "pool": 250})
def quotes(ctx):
    """A week's pool of short quotes for the footer: zenquotes, then fortune(6)."""
    want = int(ctx.cfg.get("pool", 250))
    items, seen = [], set()

    def add(text):
        text = " ".join(str(text).split())
        if 10 < len(text) < 160 and text.lower() not in seen and not text.upper().startswith("QOTD"):
            seen.add(text.lower())
            items.append(text)
    if ctx.cfg.get("zenquotes", True):
        for _ in range(max(1, min(5, want // 50))):  # 50 quotes per call; be polite between calls
            try:
                r = ctx.http("https://zenquotes.io/api/quotes", timeout=6)
            except Exception:  # noqa: BLE001
                break
            if not (r.ok and isinstance(r.body, list)):
                break
            for q in r.body:
                text, who = (q.get("q") or "").strip(), (q.get("a") or "").strip()
                if text and "zenquotes" not in text.lower():
                    add("%s — %s" % (text, who) if who else text)
            time.sleep(0.3)
    fortune = core.which("fortune", ("/usr/games",)) if ctx.cfg.get("fortune", True) else None
    for _ in range(60 if fortune and len(items) < want // 2 else 0):
        rc, out, _err = core.run([fortune, "-s", "-n", "140"], 2)
        if rc == 0:
            add(out)
    if len(items) < 10:  # a thin pool would repeat itself: keep the previous one
        prev = (ctx.prev or {}).get("items") or []
        if prev:
            return {"items": prev, "kept": True}
        raise RuntimeError("no quote source reachable")
    return {"items": items[:want]}


# ── HTTP endpoints ──────────────────────────────────────────────────────────
def _dig(body, path: str):
    cur = body
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def probe(t: dict) -> dict:
    """One endpoint: {label, url, expect?, verify?, timeout?, show?, contains?, method?}."""
    label, url = str(t.get("label") or t.get("url")), str(t["url"])
    expect = t.get("expect")
    try:
        r = core.http(url, method=str(t.get("method") or "GET"), timeout=float(t.get("timeout", 8)),
                      headers={"Accept": t.get("accept", "*/*")}, verify=bool(t.get("verify", True)),
                      redirects=bool(t.get("follow", False)))
    except Exception as e:  # noqa: BLE001
        return {"label": label, "url": url, "status": 0, "ms": 0, "up": False, "error": core.net_reason(e)}
    if expect:
        up = r.status in (expect if isinstance(expect, list) else [expect])
    else:
        up = r.status < 500 if not t.get("strict") else 200 <= r.status < 400
    row = {"label": label, "url": url, "status": r.status, "ms": r.ms, "up": up}
    if up and t.get("contains") and str(t["contains"]) not in (r.body if isinstance(r.body, str) else json.dumps(r.body)):
        row.update(up=False, error="missing text")
    if t.get("show") and isinstance(r.body, (dict, list)):
        val = _dig(r.body, t["show"])
        if val is not None:
            row["info"] = str(t.get("format", "{}")).replace("{}", str(val))[:24]
    if not up and not row.get("error"):
        row["error"] = "HTTP %d" % r.status
    return row


def check_targets(ctx, sev="crit") -> dict:
    targets = [t for t in ctx.cfg.get("targets") or [] if isinstance(t, dict) and t.get("url")]
    jobs = {str(i): (lambda t=t: probe(t)) for i, t in enumerate(targets)}
    res = core.parallel(jobs, timeout=float(ctx.cfg.get("deadline", 15)))
    rows = [res.get(str(i)) or {"label": str(t.get("label") or t["url"]), "url": t["url"], "status": 0, "ms": 0,
                                 "up": False, "error": "timeout"} for i, t in enumerate(targets)]
    slow = int(ctx.cfg.get("slow_ms", 1500))
    for r in rows:
        r["slow"] = r["up"] and r["ms"] > slow
    # Two strikes: a deploy restarts services, so one refused connection is not an outage.
    strikes = int(ctx.cfg.get("strikes", 2))
    hist = (ctx.prev or {}).get("fails") or {}
    fails = {r["label"]: (int(hist.get(r["label"], 0)) + 1 if not r["up"] else 0) for r in rows}
    data = {"rows": rows, "up": sum(1 for r in rows if r["up"]), "total": len(rows), "fails": fails}
    down = [r for r in rows if not r["up"] and fails[r["label"]] >= strikes]
    if down:
        data["alerts"] = [{"sev": sev, "msg": "%s down (%s)" % (r["label"], r.get("error") or r["status"])} for r in down]
    return data


@source("sites", ttl=300, timeout=15, scope="global", requires=("targets",),
        defaults={"targets": [], "strikes": 2, "slow_ms": 1500})
def sites(ctx):
    """Public HTTP endpoints: status and latency, probed in parallel (hub-collected)."""
    return check_targets(ctx)


@source("health", ttl=120, timeout=12, requires=("targets",), defaults={"targets": [], "strikes": 2, "slow_ms": 1500})
def health(ctx):
    """This machine's own health routes (loopback/LAN services)."""
    data = check_targets(ctx)
    for a in data.get("alerts") or []:
        a["msg"] = a["msg"].replace(" down", " health down") + " on %s" % ctx.host.label
    return data


# ── TLS certificates ────────────────────────────────────────────────────────
def cert_days(name: str, addr: str, port: int, timeout: float = 5.0) -> float:
    """Days until the certificate served at addr:port for SNI `name` expires."""
    import ssl  # ~4 ms of import the login render never needs
    sctx = ssl.create_default_context()
    with socket.create_connection((addr, port), timeout=timeout) as sock, sctx.wrap_socket(sock, server_hostname=name) as ss:
        cert = ss.getpeercert()
    return (ssl.cert_time_to_seconds(cert["notAfter"]) - core.now()) / 86400


@source("certs", ttl=6 * 3600, timeout=15, retry=1800, requires=("targets",),
        defaults={"targets": [], "warn_days": 21, "crit_days": 10})
def certs(ctx):
    """TLS certificate expiry (and validity) for the names you serve."""
    import ssl
    warn, crit = float(ctx.cfg.get("warn_days", 21)), float(ctx.cfg.get("crit_days", 10))
    targets = [{"name": t} if isinstance(t, str) else t for t in ctx.cfg.get("targets") or []]
    targets = [t for t in targets if isinstance(t, dict) and t.get("name")]
    res = core.parallel({t["name"]: (lambda t=t: cert_days(t["name"], t.get("connect") or t["name"], int(t.get("port", 443))))
                         for t in targets}, timeout=12)
    rows, alerts = [], []
    for t in targets:
        n = t["name"]
        days = res.get(n)
        if days is None:
            # Re-run once to get the reason: the parallel wrapper swallows exceptions.
            try:
                days = cert_days(n, t.get("connect") or n, int(t.get("port", 443)), 4)
                err = None
            except ssl.SSLCertVerificationError as e:
                err = "invalid: %s" % (getattr(e, "verify_message", "") or "verify failed")
            except Exception as e:  # noqa: BLE001
                err = core.net_reason(e)
            if days is None:
                rows.append({"name": n, "days": None, "ok": False, "error": err})
                alerts.append({"sev": "crit", "msg": "TLS cert %s unverifiable (%s)" % (n, err)})
                continue
        rows.append({"name": n, "days": round(days, 1), "ok": days > warn})
        if days < warn:
            alerts.append({"sev": "crit" if days < crit else "warn", "msg": "TLS cert %s expires in %dd" % (n, days)})
    data = {"rows": rows}
    if alerts:
        data["alerts"] = alerts
    return data


# ── Backups: systemd timer runs and file freshness ──────────────────────────
def unit_runs(units) -> dict:
    """{unit: {result, exit, ended, active, last, next}}; times are epoch secs."""
    from ..live import parse_show
    names = [u if "." in u else u + ".service" for u in units]
    rc, out, _ = core.run(["systemctl", "show", "--timestamp=unix", "--no-pager", "-p",
                           "Id,Result,ExecMainStatus,ExecMainExitTimestamp,ActiveState"] + names, 4)
    res = {}
    for cur in parse_show(out):
        if not cur.get("Id"):
            continue
        ts = cur.get("ExecMainExitTimestamp", "").lstrip("@")
        res[cur["Id"].rsplit(".", 1)[0]] = {
            "result": cur.get("Result"), "exit": cur.get("ExecMainStatus"),
            "ended": int(ts) if ts.isdigit() else 0, "active": cur.get("ActiveState")}
    rc, out, _ = core.run(["systemctl", "list-timers", "--all", "--output=json", "--no-pager"], 4)
    try:
        timers = json.loads(out) if rc == 0 else []
    except ValueError:
        timers = []
    for t in timers:
        u = (t.get("activates") or t.get("unit", "")).rsplit(".", 1)[0]
        if u in res:
            res[u]["last"] = int((t.get("last") or 0) / 1e6)
            res[u]["next"] = int((t.get("next") or 0) / 1e6)
    return res


@source("backups", ttl=600, timeout=10, defaults={"units": [], "files": []}, requires=("units|files",))
def backups(ctx):
    """Backup jobs: systemd service results and timer cadence, plus files that must stay fresh."""
    import glob
    now = core.now()
    rows, alerts = [], []
    units = [{"unit": u} if isinstance(u, str) else u for u in ctx.cfg.get("units") or []]
    units = [u for u in units if isinstance(u, dict) and u.get("unit")]
    runs = unit_runs([u["unit"] for u in units]) if units and core.which("systemctl") else {}
    for u in units:
        unit = str(u["unit"]).rsplit(".service", 1)[0]
        r = runs.get(unit)
        name = str(u.get("label") or unit)
        if not r:
            rows.append({"name": name, "ok": False, "result": "missing", "last": 0, "next": 0})
            alerts.append({"sev": "warn", "msg": "backup %s: unit %s not found" % (name, unit)})
            continue
        last, nxt = r.get("last") or r.get("ended") or 0, r.get("next") or 0
        interval = float(u.get("every_hours", 0)) * 3600 or ((nxt - last) if (nxt and last and nxt > last) else 86400)
        ok = r.get("result") in ("success", None, "")
        overdue = bool(last) and now - last > interval * 1.5 + 3600
        rows.append({"name": name, "ok": ok and not overdue, "last": last, "next": nxt, "result": r.get("result"),
                     "interval": interval})
        # Judge the service, not its timer: a timer stays green while every run fails.
        if not ok:
            alerts.append({"sev": "crit", "msg": "backup %s %s" % (name, r.get("result")),
                           "fix": "journalctl -u %s -n 50" % unit})
        elif overdue:
            alerts.append({"sev": "warn", "msg": "backup %s overdue (%s)" % (name, core.human_age(now - last, " ago"))})
        elif not nxt and not u.get("every_hours"):
            alerts.append({"sev": "warn", "msg": "backup %s: no timer scheduled" % name})
    for f in ctx.cfg.get("files") or []:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        paths = sorted(glob.glob(core.expand(f["path"])), key=core.mtime)
        mt = core.mtime(paths[-1]) if paths else 0
        name = str(f.get("label") or os.path.basename(str(f["path"])))
        max_h = float(f.get("max_age_hours", 26))
        ok = bool(mt) and now - mt < max_h * 3600
        rows.append({"name": name, "ok": ok, "last": mt, "next": 0, "result": "file", "interval": max_h * 3600})
        if not mt:
            alerts.append({"sev": "warn", "msg": "backup %s: nothing at %s" % (name, f["path"])})
        elif not ok:
            alerts.append({"sev": "warn", "msg": "backup %s is %s old (max %dh)" % (name, core.human_age(now - mt), max_h)})
    data = {"rows": rows}
    if alerts:
        data["alerts"] = alerts
    return data


# ── File budgets (context files, memory indexes, logs) ──────────────────────
@source("files", ttl=1800, timeout=10, requires=("targets",), defaults={"targets": []})
def files(ctx):
    """Size and line budgets for files that must stay small (CLAUDE.md, AGENTS.md, MEMORY.md, ...)."""
    import glob
    rows, alerts = [], []
    for t in ctx.cfg.get("targets") or []:
        if not isinstance(t, dict) or not (t.get("path") or t.get("glob")):
            continue
        pat = core.expand(t.get("glob") or t["path"])
        max_b, max_l = int(t.get("max_bytes") or 0), int(t.get("max_lines") or 0)
        cap = "/".join("%d%s" % (n, unit) for n, unit in ((max_b, "B"), (max_l, "L")) if n)
        warn_at = float(t.get("warn_pct", 90))
        for p in sorted(glob.glob(pat))[:20]:
            try:
                with open(p, "rb") as fh:
                    raw = fh.read(8 << 20)
            except OSError:
                continue
            nb, nl = len(raw), raw.count(b"\n")
            share = max(100.0 * nb / max_b if max_b else 0, 100.0 * nl / max_l if max_l else 0)
            label = str(t.get("label") or "")
            if not label or t.get("glob"):
                parent = os.path.basename(os.path.dirname(p))
                label = ("%s/%s" % (parent, os.path.basename(p))) if t.get("glob") else os.path.basename(p)
            rows.append({"label": label[:24], "bytes": nb, "lines": nl, "pct": round(share, 1),
                         "max_bytes": max_b, "max_lines": max_l, "mtime": core.mtime(p)})
            if share >= 100:
                alerts.append({"sev": str(t.get("sev", "crit")),
                               "msg": "%s over budget: %dB/%dL (cap %s)" % (label, nb, nl, cap)})
            elif share >= warn_at:
                alerts.append({"sev": "warn", "msg": "%s at %d%% of its budget" % (label, share)})
    rows.sort(key=lambda r: -r["pct"])
    data = {"rows": rows[:16], "files": len(rows)}
    if alerts:
        data["alerts"] = alerts
    return data


# ── Deploy drift ────────────────────────────────────────────────────────────
@source("drift", ttl=600, timeout=20, requires=("targets",), defaults={"targets": [], "fetch": False})
def drift(ctx):
    """Is what is deployed what the repo says? Deployed revision vs a ref."""
    rows, alerts = [], []
    for t in ctx.cfg.get("targets") or []:
        if not isinstance(t, dict) or not t.get("repo"):
            continue
        label = str(t.get("label") or os.path.basename(str(t["repo"]).rstrip("/")))
        repo = core.expand(t["repo"])
        ref = str(t.get("ref") or "HEAD")
        deployed = ""
        if t.get("file"):
            words = core.read_text(core.expand(t["file"]), 200).split()
            deployed = words[0] if words else ""
        elif t.get("url"):
            try:
                r = core.http(str(t["url"]), timeout=6)
                body = r.body
                deployed = str(_dig(body, t.get("field", "sha")) if isinstance(body, dict) else body or "").strip()
            except Exception as e:  # noqa: BLE001
                rows.append({"label": label, "ok": False, "error": core.net_reason(e)})
                continue
        elif t.get("command"):
            argv = t["command"] if isinstance(t["command"], list) else ["/bin/sh", "-c", str(t["command"])]
            rc, out, _ = core.run(argv, 10)
            deployed = out.strip().split()[0] if rc == 0 and out.strip() else ""
        if not re.match(r"^[0-9a-f]{7,40}$", deployed or ""):
            rows.append({"label": label, "ok": False, "error": "no deployed revision"})
            continue
        if ctx.cfg.get("fetch") or t.get("fetch"):
            core.run(["git", "-C", repo, "fetch", "--quiet", "--no-tags"], 15)
        rc, want, _ = core.run(["git", "-C", repo, "rev-parse", ref], 4)
        want = want.strip()
        if rc != 0 or not want:
            rows.append({"label": label, "ok": False, "error": "cannot resolve %s" % ref})
            continue
        same = want.startswith(deployed) or deployed.startswith(want)
        behind = 0
        if not same:
            rc, out, _ = core.run(["git", "-C", repo, "rev-list", "--count", "%s..%s" % (deployed, want)], 4)
            behind = int(out.strip()) if rc == 0 and out.strip().isdigit() else -1
        rows.append({"label": label, "ok": same, "deployed": deployed[:9], "want": want[:9], "behind": behind, "ref": ref})
        if not same:
            hours = float(t.get("grace_hours", 0))
            if hours <= 0 or (core.now() - core.mtime(os.path.join(repo, ".git", "FETCH_HEAD"))) > hours * 3600:
                alerts.append({"sev": str(t.get("sev", "warn")), "msg": "%s deployed %s is %s %s" % (
                    label, deployed[:9], ("%d commits behind" % behind) if behind > 0 else "not", ref)})
    data = {"rows": rows}
    if alerts:
        data["alerts"] = alerts
    return data


# ── MongoDB ─────────────────────────────────────────────────────────────────
_MONGO_JS = (
    "const t0=Date.now();const d=connect(process.env.PORCHLIGHT_MONGO_URI).getSiblingDB(process.env.PORCHLIGHT_MONGO_DB||'admin');"
    "const ok=d.runCommand({ping:1}).ok;const ms=Date.now()-t0;const h=d.hello();const s=d.stats();"
    "print(JSON.stringify({ok:ok,ms:ms,set:h.setName||null,primary:!!(h.primary||h.isWritablePrimary),"
    "hosts:(h.hosts||[]).length,db:d.getName(),data:Number(s.dataSize),storage:Number(s.storageSize),"
    "index:Number(s.indexSize),collections:Number(s.collections),objects:Number(s.objects)}))")  # BSON Longs → numbers


@source("mongodb", ttl=900, timeout=30, optin=True, binaries=("mongosh",), share=True,
        defaults={"uri_env": "MONGODB_URI", "env_file": "", "database": "", "slow_ms": 3000})
def mongodb(ctx):
    """MongoDB health over mongosh: ping, hello, dbStats (read-only).
    The URI travels in the environment, never argv (ps shows argv to every user)."""
    key = str(ctx.cfg.get("uri_env") or "MONGODB_URI")
    uri = (core.dotenv(ctx.cfg["env_file"]).get(key) if ctx.cfg.get("env_file") else None) or ctx.env.get(key)
    if not uri:
        return {"status": "unknown", "error": "no %s" % key}
    mongosh = core.which("mongosh", (os.path.expanduser("~/.npm-global/bin"),))
    env = dict(os.environ, PORCHLIGHT_MONGO_URI=uri, PORCHLIGHT_MONGO_DB=str(ctx.cfg.get("database") or ""))
    rc, out, err = core.run([mongosh, "--nodb", "--quiet", "--eval", _MONGO_JS], 25, env=env)
    line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("{")), "")
    if rc != 0 or not line:
        msg = re.sub(r"mongodb(\+srv)?://[^\s@/]+@", "mongodb://<redacted>@", (err or out).replace(uri, "<uri>"))
        raise RuntimeError("mongosh rc %d: %s" % (rc, msg.strip().splitlines()[-1][:140] if msg.strip() else ""))
    d = json.loads(line)
    ok = bool(d.get("ok")) and d.get("primary")
    d["status"] = "ok" if ok else "crit"
    d["summary"] = "%s · ping %dms · %s data · %s coll" % (
        "primary up" if d.get("primary") else "NO PRIMARY", d.get("ms", 0),
        core.human_bytes(d.get("data") or 0), d.get("collections", "?"))
    if not ok:
        d["alerts"] = [{"sev": "crit", "msg": "MongoDB %s unhealthy (%s)" % (d.get("db"), d["summary"])}]
    elif d.get("ms", 0) > int(ctx.cfg.get("slow_ms", 3000)):
        d["alerts"] = [{"sev": "warn", "msg": "MongoDB slow: connect+ping %dms" % d["ms"]}]
    return d


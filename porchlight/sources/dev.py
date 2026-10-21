"""Developer and work-machine sources: container stacks, AWS credentials, EC2
instances, pull/merge requests on GitHub and GitLab, and reachability.

  docker     containers grouped by compose project (up/total, unhealthy, restart
             loops, fresh crashes), plus the containers you say must be running
  aws_auth   the credentials the AWS CLI would use (static keys, the SSO token
             clock) and an STS GetCallerIdentity check
  instances  your EC2 instances (type, IP, uptime), or a cache file when AWS says no
  prs        your open pull/merge requests, and the ones waiting on your review
  reach      ssh routes (a TCP connect, no login) and the endpoints you depend
             on, directly or through an `ssh -D` SOCKS proxy

What is merely environmental renders as state and never alerts: the container
engine not started, no AWS login on this box, off VPN, a stopped stack or EC2
instance. Alerts are for things to act on: an unhealthy, crash-looping or freshly
crashed container, an expected container that is down, AWS credentials that
exist but are rejected, your PRs with conflicts, changes requested or failing
CI, an endpoint answering 5xx twice in a row.

Every call is read-only: `docker ps`, `aws sts get-caller-identity`, `aws ec2
describe-instances`, one GitHub GraphQL search, GitLab GETs, `ssh -G`, TCP
connects and GETs. Modules this file imports at the top are already loaded by
the core (sources register on every login render); heavier ones load on use.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import socket
import time

from .. import core, registry, secrets
from ..registry import source


def _some(names, n: int = 5) -> str:
    """'a, b, c +2 more' for alert text."""
    names = list(names)
    return ", ".join(names[:n]) + (" +%d more" % (len(names) - n) if len(names) > n else "")


# ── Containers ──────────────────────────────────────────────────────────────
_PS_FORMAT = ('{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Label "com.docker.compose.project"}}'
              '\t{{.Label "com.docker.compose.service"}}')
_EXITED = re.compile(r"Exited \((-?\d+)\)\s+(?:Less than a second|(?:About\s+)?(an?|\d+)\s+"
                     r"(second|minute|hour|day|week|month|year)s?)\s+ago")
_UNIT_S = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000,
           "year": 31536000}
STOPPED = (130, 143)  # SIGINT/SIGTERM: Ctrl-C, `docker stop`, `compose stop`; a choice, not a crash
# Where container CLIs live outside a launchd/systemd PATH: Docker Desktop,
# OrbStack, Rancher Desktop and the Podman installer. colima drives Homebrew's
# docker, which core.which already finds in /opt/homebrew/bin and /usr/local/bin.
ENGINE_DIRS = ("/Applications/Docker.app/Contents/Resources/bin", "~/.docker/bin", "~/.orbstack/bin",
               "~/.rd/bin", "/opt/podman/bin")
_DOWN = ("cannot connect", "is the docker daemon running", "error during connect", "connection refused",
         "no such file or directory")


def engine_binary(setting: str = ""):
    """The configured container CLI (a name or a path), else docker, else podman."""
    if setting:
        return core.which(core.expand(setting), ENGINE_DIRS)
    return core.which("docker", ENGINE_DIRS) or core.which("podman", ENGINE_DIRS)


def _exit_info(status: str):
    """'Exited (1) 3 hours ago' → (1, 10800); (code or None, None) when the age is unparseable."""
    m = _EXITED.search(status or "")
    if not m:
        code = re.search(r"Exited \((-?\d+)\)", status or "")
        return (int(code.group(1)) if code else None), None
    if m.group(2) is None:  # "Less than a second ago"
        return int(m.group(1)), 0
    n = 1 if m.group(2) in ("a", "an") else int(m.group(2))
    return int(m.group(1)), n * _UNIT_S[m.group(3)]


def ps_rows(text: str) -> list:
    """`docker ps -a` lines in _PS_FORMAT → [{name, state, status, project, service, code, ago}]."""
    out = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 5 or not parts[0]:
            continue
        name, state, status, project, service = parts[:5]
        # podman prints "<no value>" for a label the container does not carry
        project, service = ("" if v == "<no value>" else v for v in (project, service))
        state = state.lower()
        code, ago = _exit_info(status) if state in ("exited", "dead") else (None, None)
        out.append({"name": name, "state": state, "status": status, "project": project, "service": service,
                    "code": code, "ago": ago})
    return out


def active(p) -> bool:
    """Running, or crash-looping: a project whose containers all restart is not stopped."""
    return bool(p["running"] or p["restarting"])


def _ignored(project: str, ignore) -> bool:
    return bool(project) and any(fnmatch.fnmatchcase(project, str(p)) for p in ignore or ())


def group(rows, ignore=()) -> dict:
    """Containers → per compose project counts and problems, plus the standalone ones.
    Exit 0 is a one-shot that finished (migrations, seeds), 130/143 a deliberate
    stop, anything else a crash. Projects in `ignore` (globs) are left out."""
    projects = {}
    other = {"running": 0, "total": 0, "unhealthy": [], "restarting": []}
    for c in rows:
        if _ignored(c["project"], ignore):
            continue
        if not c["project"]:
            other["total"] += 1
            if c["state"] == "running":
                other["running"] += 1
                if "(unhealthy)" in c["status"]:
                    other["unhealthy"].append(c["name"])
            elif c["state"] == "restarting":
                other["restarting"].append(c["name"])
            continue
        p = projects.setdefault(c["project"], {"name": c["project"], "running": 0, "total": 0, "done": 0,
                                               "starting": 0, "stopped": 0, "unhealthy": [], "restarting": [],
                                               "failed": []})
        p["total"] += 1
        label = c["service"] or c["name"]
        if c["state"] == "running":
            p["running"] += 1
            if "(unhealthy)" in c["status"]:
                p["unhealthy"].append(label)
            elif "health: starting" in c["status"] or "(starting)" in c["status"]:  # docker, podman
                p["starting"] += 1
        elif c["state"] == "restarting":
            p["restarting"].append(label)
        elif c["state"] in ("exited", "dead"):
            if c["code"] == 0:
                p["done"] += 1  # one-shots finish with 0 by design
            elif c["code"] in STOPPED:
                p["stopped"] += 1
            else:
                p["failed"].append({"name": label, "code": c["code"], "ago": c["ago"]})
        else:
            p["stopped"] += 1  # created or paused: present but not running, and not a crash
    rows = sorted(projects.values(), key=lambda p: (not active(p), p["name"]))
    return {"projects": rows, "other": other}


def parse_ps(text: str, ignore=()) -> dict:
    """`docker ps -a` output (_PS_FORMAT) → per compose project counts and problems."""
    return group(ps_rows(text), ignore)


def stack_alerts(projects, recent_s: float = 6 * 3600) -> list:
    """Warnings for running stacks: unhealthy containers and crashes younger than
    recent_s (an older crash is history, not news). A stopped stack is a choice,
    not a fault; restart loops are reported on their own, as critical."""
    out = []
    for p in projects:
        if not active(p):
            continue
        bits = []
        if p["unhealthy"]:
            bits.append("%d unhealthy (%s)" % (len(p["unhealthy"]), ", ".join(p["unhealthy"][:3])))
        fresh = [f for f in p["failed"] if f.get("ago") is not None and f["ago"] < recent_s]
        if fresh:
            bits.append("%s exited %s" % (", ".join(f["name"] for f in fresh[:3]),
                                         "/".join(sorted({str(f["code"]) for f in fresh}))))
        if bits:
            out.append({"sev": "warn", "msg": "%s stack: %s" % (p["name"], "; ".join(bits))})
    return out


def _globby(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def expected_check(rows, patterns) -> dict:
    """Which expected containers are not running. An entry is a container name or
    glob, or project/service (also globbable) when it contains a slash. Each entry
    must match a running container; a glob also reports every container it matches
    that is not running, except one-shots that finished with exit 0."""
    want = up = 0
    missing = []
    for pat in patterns:
        hits = [c for c in rows if fnmatch.fnmatchcase(
            ("%s/%s" % (c["project"], c["service"])) if "/" in pat else c["name"], pat)]
        if _globby(pat):
            hits = [c for c in hits if not (c["state"] in ("exited", "dead") and c["code"] == 0)]
        if not hits:
            want += 1
            missing.append(pat)
        elif not _globby(pat):
            want += 1
            if any(c["state"] == "running" for c in hits):
                up += 1
            else:
                missing.append(pat)
        else:
            want += len(hits)
            up += sum(1 for c in hits if c["state"] == "running")
            missing += [c["name"] for c in hits if c["state"] != "running" and c["name"] not in missing]
    return {"want": want, "up": up, "missing": missing}


def expected_entries(cfg) -> tuple:
    """(patterns, problem): `expected` plus the names in `expected_file`, a JSON list
    (or an object holding a "running" or "expected" list) that another tool writes."""
    pats = [str(p) for p in cfg.get("expected") or [] if str(p).strip()]
    path = str(cfg.get("expected_file") or "")
    if not path:
        return pats, None
    doc = core.read_json(core.expand(path))
    if isinstance(doc, dict):
        doc = doc.get("running", doc.get("expected"))
    if not isinstance(doc, list):
        return pats, "expected_file %s is unreadable or not a JSON list of names" % path
    for p in doc:
        if isinstance(p, str) and p.strip() and p not in pats:
            pats.append(p)
    return pats, None


@source("docker", ttl=120, timeout=10, defaults={
    "binary": "", "ignore_projects": [], "expected": [], "expected_file": "", "recent_crash_hours": 6})
def docker(ctx):
    """Containers per compose project: up/total, unhealthy, restart loops, fresh crashes, expected ones."""
    cfg = ctx.cfg
    setting = str(cfg.get("binary") or "")
    exe = engine_binary(setting)
    name = os.path.basename(exe or setting or "docker")
    recent = float(cfg.get("recent_crash_hours", 6)) * 3600
    ignore = [str(p) for p in cfg.get("ignore_projects") or []]
    patterns, problem = expected_entries(cfg)
    data = {"engine": "missing", "binary": name, "recent_s": recent}
    rows = []
    if exe:
        rc, out, err = core.run([exe, "ps", "-a", "--format", _PS_FORMAT], 8)
        low = (err or out or "").lower()
        if rc == 0:
            data["engine"] = "up"
            rows = ps_rows(out)
            data.update(group(rows, ignore))
        elif rc == 127:
            pass  # gone since `which` found it
        elif "permission denied" in low:
            data["engine"] = "denied"  # the socket exists; this user may not use it
        elif rc == 124 or any(s in low for s in _DOWN):
            data["engine"] = "down"  # Docker Desktop / colima / podman machine not started
        else:
            raise RuntimeError("%s ps rc %d: %s" % (name, rc, (err or out).strip()[:160]))
    alerts = []
    if data["engine"] == "up":
        alerts += stack_alerts(data["projects"], recent)
        sick = data["other"]["unhealthy"]
        if sick:
            alerts.append({"sev": "warn", "msg": "unhealthy container%s: %s" % ("" if len(sick) == 1 else "s", _some(sick))})
        looping = [c["name"] for c in rows if c["state"] == "restarting" and not _ignored(c["project"], ignore)]
        if looping:
            alerts.append({"sev": "crit", "msg": "restart loop: %s" % _some(looping),
                           "fix": "%s logs --tail 50 %s" % (name, looping[0])})
    # Engine down or missing is state, not an alert: unless containers were declared
    # expected, in which case they are down, and that is exactly what was asked about.
    if patterns and data["engine"] == "denied":
        alerts.append({"sev": "warn", "msg": "expected containers unverifiable: no access to the %s socket" % name})
    elif patterns:
        ex = expected_check(rows, patterns) if data["engine"] == "up" else \
            {"want": len(patterns), "up": 0, "missing": list(patterns)}
        data["expected"] = ex
        if ex["missing"]:
            why = "" if data["engine"] == "up" else " (%s %s)" % (
                name, "not running" if data["engine"] == "down" else "not installed")
            alerts.append({"sev": "crit", "msg": "container down: %s%s" % (_some(ex["missing"]), why)})
        data["badge"] = "%d/%d" % (ex["up"], ex["want"])
    elif data["engine"] == "up":
        live = [p for p in data["projects"] if active(p)]
        want = sum(p["total"] - p["done"] for p in live) + data["other"]["running"]
        if want:  # for the fleet table: running / what the running stacks should have up
            data["badge"] = "%d/%d" % (sum(p["running"] for p in live) + data["other"]["running"], want)
    if problem:
        alerts.append({"sev": "warn", "msg": problem})
    if alerts:
        data["alerts"] = alerts
    return data


# ── AWS credentials ─────────────────────────────────────────────────────────
AWS_DIR = os.path.join(core.HOME, ".aws")
DEFAULT_LOGIN = "aws sso login"


def _ini(path: str) -> dict:
    import configparser
    cp = configparser.RawConfigParser(strict=False)  # tolerate the duplicate keys hand edits leave
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return {}
    return {s: dict(cp.items(s)) for s in cp.sections()}


def aws_files(aws_dir=None, env=None, profile: str = "") -> dict:
    """What the AWS files say, without the network: profiles holding static keys,
    SSO profiles, and the newest SSO access token's expiry. Never reads a key out,
    and keeps nothing of a token but its expiry and whether it can be refreshed.
    AWS_CONFIG_FILE and AWS_SHARED_CREDENTIALS_FILE are honored as the CLI does;
    the token cache is always <aws_dir>/sso/cache."""
    env = os.environ if env is None else env
    aws_dir = aws_dir or AWS_DIR
    creds_path = core.expand(env.get("AWS_SHARED_CREDENTIALS_FILE") or os.path.join(aws_dir, "credentials"))
    cfg_path = core.expand(env.get("AWS_CONFIG_FILE") or os.path.join(aws_dir, "config"))
    creds, conf = _ini(creds_path), _ini(cfg_path)
    profiles = {(s[8:] if s.startswith("profile ") else s): kv for s, kv in conf.items()
                if not s.startswith(("sso-session ", "services "))}
    current = profile or env.get("AWS_PROFILE") or env.get("AWS_DEFAULT_PROFILE") or "default"
    mine = profiles.get(current) or {}
    sso = sorted(n for n, kv in profiles.items() if kv.get("sso_session") or kv.get("sso_start_url"))
    account = mine.get("sso_account_id") or next(
        (kv["sso_account_id"] for kv in profiles.values() if kv.get("sso_account_id")), None)
    expires, refreshable = 0, False
    cache = os.path.join(aws_dir, "sso", "cache")
    try:
        names = sorted(os.listdir(cache))
    except OSError:
        names = []
    for n in names:
        if not n.endswith(".json") or n.startswith("botocore-client"):
            continue  # botocore-client-* are OIDC client registrations, not tokens
        j = core.read_json(os.path.join(cache, n))
        # Only SSO access tokens carry startUrl: other tools keep their own files here.
        if isinstance(j, dict) and j.get("startUrl") and j.get("accessToken") and j.get("expiresAt"):
            exp = core.utc_epoch(str(j["expiresAt"]))
            if exp >= expires:
                expires, refreshable = exp, bool(j.get("refreshToken"))
    has_env = bool(env.get("AWS_ACCESS_KEY_ID"))
    static = {s for s, kv in creds.items() if kv.get("aws_access_key_id")}
    static |= {n for n, kv in profiles.items() if kv.get("aws_access_key_id")}
    return {"static": sorted(static), "env": has_env, "profile": current,
            "region": env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or mine.get("region") or None,
            "sso": {"profiles": sso, "account": account, "expires": expires or None, "refreshable": refreshable},
            "fp": [core.mtime(creds_path), core.mtime(cfg_path), expires, has_env, env.get("AWS_PROFILE") or ""]}


def aws_env(extra=None, region: str = "") -> dict:
    """The environment for an `aws` call: this process's own (what a fresh shell's
    `aws` would see), any AWS_* settings from the secrets files, and no pager or
    prompt. Service bearer tokens (AWS_BEARER_TOKEN_<SERVICE>, such as an AI SDK's
    Bedrock key) are dropped: each authenticates one unrelated API, so it has no
    business riding along on an STS or EC2 call, nor sitting in a process that has
    no use for it."""
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (extra or {}).items() if k.startswith("AWS_")})
    for k in [k for k in env if k.startswith("AWS_BEARER_TOKEN")]:
        env.pop(k)
    env.update(AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off")
    if region:
        env.update(AWS_REGION=region, AWS_DEFAULT_REGION=region)
    return env


def _aws_flags(region: str, profile: str) -> list:
    # --profile, unlike AWS_PROFILE, also outranks AWS_ACCESS_KEY_ID in the environment.
    return (["--region", region] if region else []) + (["--profile", profile] if profile else [])


_REJECTED = {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId", "SignatureDoesNotMatch",
             "AccessDenied", "AccessDeniedException", "UnrecognizedClientException", "InvalidAccessKeyId",
             "AuthFailure", "UnauthorizedOperation"}
_REJECTED_TEXT = ("token has expired", "sso session", "error loading sso token", "refresh failed",
                  "security token included in the request is invalid")


def classify_sts(rc: int, out: str, err: str) -> dict:
    """An `aws` call's outcome → ok | rejected (credentials exist, AWS says no) |
    none (nothing configured) | unreachable (network) | error (anything else)."""
    if rc == 0:
        try:
            j = json.loads(out)
        except ValueError:
            return {"state": "error", "error": "unparseable sts reply"}
        arn = j.get("Arn") or ""
        tail = arn.split(":", 5)[-1] if arn.count(":") >= 5 else arn
        principal = ("role/" + tail.split("/")[1]) if tail.startswith("assumed-role/") else tail
        return {"state": "ok", "account": j.get("Account"), "principal": principal}
    text = " ".join((err or out or "").split())
    low = text.lower()
    m = re.search(r"\(([A-Za-z]+)\)", text)
    code = m.group(1) if m else ""
    if rc == 124 or any(s in low for s in ("could not connect", "connect timeout", "read timeout",
                                           "name or service not known", "nodename nor servname")):
        state = "unreachable"
    elif "unable to locate credentials" in low or "could not be found" in low:
        state = "none"
    elif code in _REJECTED or any(s in low for s in _REJECTED_TEXT):
        state = "rejected"
    else:
        state = "error"
    return {"state": state, "error": code or text[:100] or "rc %d" % rc}


def login_hint(cfg, profile: str, files: dict) -> str:
    """What fixes rejected credentials: login_command; `aws configure` instead of
    the default when this machine has static keys and no SSO profile."""
    cmd = str(cfg.get("login_command") or DEFAULT_LOGIN)
    sso = files["sso"]["profiles"]
    if cmd == DEFAULT_LOGIN and not sso and (files["static"] or files["env"]):
        cmd = "aws configure"
    if profile and cmd in (DEFAULT_LOGIN, "aws configure"):
        cmd += " --profile " + profile
    elif cmd == DEFAULT_LOGIN and sso and files["profile"] not in sso:
        cmd += " --profile " + sso[0]  # the CLI's active profile is not an SSO one: name one that is
    return cmd


@source("aws_auth", ttl=300, timeout=20, binaries=("aws",), defaults={
    "region": "", "profile": "", "login_command": DEFAULT_LOGIN, "recheck_minutes": 30})
def aws_auth(ctx):
    """AWS credentials the CLI would use: static keys, the SSO token clock, an STS identity check."""
    cfg = ctx.cfg
    region, profile = str(cfg.get("region") or ""), str(cfg.get("profile") or "")
    env = aws_env(ctx.env, region)
    f = aws_files(env=env, profile=profile)
    fp = f["fp"] + [profile, region]
    data = {"cli": True, "region": region or f["region"], "profile": f["profile"], "static": f["static"],
            "env": f["env"], "sso": f["sso"], "login": login_hint(cfg, profile, f)}
    aws = core.which("aws", tuple(cfg.get("paths") or ()))
    if not aws:
        data["cli"] = False
        return data
    # GetCallerIdentity is a network round trip: repeat it only when the files (or the
    # token, or the environment) changed, every recheck_minutes, or after a network miss.
    prev = (ctx.prev or {}).get("sts") or {}
    recheck = float(cfg.get("recheck_minutes", 30)) * 60
    if prev.get("fp") == fp and core.now() - float(prev.get("at") or 0) < recheck \
            and prev.get("state") not in (None, "unreachable"):
        data["sts"] = prev
    else:
        rc, out, err = core.run([aws, "sts", "get-caller-identity", "--output", "json"] + _aws_flags(region, profile),
                                15, env=env)
        st = classify_sts(rc, out, err)
        if st.get("error"):
            st["error"] = secrets.redact(st["error"], ctx.env, 100)
        data["sts"] = dict(st, at=core.now(), fp=fp)
    if data["sts"]["state"] == "rejected":
        data["alerts"] = [{"sev": "warn", "msg": "AWS credentials rejected (%s)" % data["sts"]["error"],
                           "fix": data["login"]}]
    return data


# ── EC2 instances ───────────────────────────────────────────────────────────
_EC2_QUERY = ("Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,InstanceType,PrivateIpAddress,"
              "InstanceId,LaunchTime,State.Name]")
_LIVE = "Name=instance-state-name,Values=running,stopped,stopping,pending"  # terminated ones are gone
DEFAULT_START = "aws ec2 start-instances --instance-ids {id}"
_ORDER = {"running": 0, "pending": 1, "stopping": 2, "stopped": 3}


def parse_instances(text: str) -> list:
    """describe-instances text (name, type, ip, id, launched, state; tab-separated,
    the format of a cache file too) → rows, running first. "None" is the CLI's
    text for a missing value: an untagged instance goes by its id."""
    rows = []
    for line in (text or "").splitlines():
        p = [x.strip() for x in line.split("\t")]
        if len(p) < 6 or not p[3].startswith("i-"):
            continue
        name, itype, ip, iid, launched, state = p[:6]
        rows.append({"name": iid if name in ("None", "") else name, "type": itype,
                     "ip": "" if ip in ("None", "") else ip, "id": iid,
                     "launched": core.iso_epoch(launched) or 0, "state": state})
    return sorted(rows, key=lambda r: (_ORDER.get(r["state"], 4), r["name"]))


def _fleet(rows, src, asof, cfg, region: str, profile: str) -> dict:
    hint = str(cfg.get("start_hint", DEFAULT_START) or "")
    flags = " ".join(_aws_flags(region, profile)) if hint == DEFAULT_START else ""
    for r in rows:
        if r["state"] == "stopped" and hint:
            r["start"] = hint.replace("{id}", r["id"]).replace("{name}", r["name"]) + (" " + flags if flags else "")
    return {"src": src, "asof": asof, "rows": rows[:max(1, int(cfg.get("limit", 50)))], "total": len(rows),
            "running": sum(1 for r in rows if r["state"] == "running")}


@source("instances", ttl=600, timeout=25, hosts=("#dev",), binaries=("aws",), defaults={
    "region": "", "profile": "", "filters": [], "cache_file": "", "start_hint": DEFAULT_START, "limit": 50})
def instances(ctx):
    """EC2 instances (all you can see, or the ones your filters pick); a cache file when AWS says no."""
    cfg = ctx.cfg
    region, profile = str(cfg.get("region") or ""), str(cfg.get("profile") or "")
    filters = [str(f).strip() for f in cfg.get("filters") or [] if str(f).strip()]
    if not any(f.startswith("Name=instance-state-name,") for f in filters):
        filters.append(_LIVE)
    aws = core.which("aws", tuple(cfg.get("paths") or ()))
    state, err = "error", "aws CLI not installed"
    if aws:
        rc, out, e = core.run([aws, "ec2", "describe-instances", "--filters"] + filters +
                              ["--query", _EC2_QUERY, "--output", "text", "--max-items", "500"] +
                              _aws_flags(region, profile), 20, env=aws_env(ctx.env, region))
        if rc == 0:
            return _fleet(parse_instances(out), "aws", core.now(), cfg, region, profile)
        st = classify_sts(rc, out, e)
        state, err = st["state"], secrets.redact(st["error"], ctx.env, 100)
    prev = ctx.prev or {}
    path = core.expand(str(cfg.get("cache_file") or ""))
    rows = parse_instances(core.read_text(path)) if path else []
    if rows and core.mtime(path) >= float(prev.get("asof") or 0):  # the newer of the cache and our last list
        return dict(_fleet(rows, "cache", core.mtime(path), cfg, region, profile), aws_error=err)
    if state == "none" and not prev.get("rows"):
        return {"src": None, "rows": [], "total": 0, "running": 0, "state": "none"}  # no AWS here: nothing to show
    # Rejected or expired credentials, or off the network: a soft failure keeps the last list.
    return {"error": "describe-instances: %s" % err, "status": "unknown"}


# ── Pull and merge requests ─────────────────────────────────────────────────
# One read-only GraphQL request: both searches, each PR's mergeability, review
# verdicts (latest per reviewer) and the CI rollup of its head commit.
_GH_QUERY = (
    "query($mine: String!, $review: String!, $n: Int!) {"
    " mine: search(query: $mine, type: ISSUE, first: $n) { issueCount nodes { ...pr } }"
    " review: search(query: $review, type: ISSUE, first: $n) { issueCount nodes { ...pr } } }"
    " fragment pr on PullRequest { number title url isDraft updatedAt mergeable reviewDecision"
    " repository { nameWithOwner } latestOpinionatedReviews(first: 20) { nodes { state } }"
    " commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } }")
_GH_SEARCH = "is:pr is:open archived:false sort:updated-desc "
_GH_MERGE = {"MERGEABLE": "mergeable", "CONFLICTING": "conflicts"}
_GH_CI = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure", "PENDING": "pending", "EXPECTED": "pending"}
_GL_CI = {"success": "success", "failed": "failure", "running": "pending", "pending": "pending", "created": "pending",
          "preparing": "pending", "waiting_for_resource": "pending", "scheduled": "pending"}
_GL_UNCHECKED = ("unchecked", "checking", "preparing", "approvals_syncing", "cannot_be_merged_recheck")
FORGES = {"gh": "github", "gl": "gitlab"}


class ForgeError(Exception):
    """A forge answered, but not with a list (unauthorized, rate-limited, a bad query)."""


def github_token(env, host: str = "github.com"):
    """GH_TOKEN or GITHUB_TOKEN (GH_ENTERPRISE_TOKEN for another host), else `gh auth token`."""
    keys = ("GH_TOKEN", "GITHUB_TOKEN") if host == "github.com" else ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    for k in keys:
        if env.get(k):
            return env[k]
    gh = core.which("gh")
    if not gh:
        return None
    # Always name the host: bare `gh auth token` answers for GH_HOST, which may be another instance.
    rc, out, _ = core.run([gh, "auth", "token", "--hostname", host], 5)
    tok = out.strip()
    return tok if rc == 0 and tok and not any(ch.isspace() for ch in tok) else None


def gh_row(n) -> dict:
    """A GraphQL PullRequest node → the shared row shape."""
    states = [r.get("state") for r in ((n.get("latestOpinionatedReviews") or {}).get("nodes") or []) if isinstance(r, dict)]
    approvals, changes = states.count("APPROVED"), states.count("CHANGES_REQUESTED")
    if n.get("reviewDecision") == "APPROVED":
        approvals = max(approvals, 1)
    elif n.get("reviewDecision") == "CHANGES_REQUESTED":
        changes = max(changes, 1)
    commits = [c for c in ((n.get("commits") or {}).get("nodes") or []) if isinstance(c, dict)]
    roll = ((commits[-1].get("commit") or {}).get("statusCheckRollup") or {}).get("state") if commits else None
    repo = (n.get("repository") or {}).get("nameWithOwner") or "?"
    return {"forge": "gh", "ref": "%s#%s" % (repo, n["number"]), "n": n["number"], "title": str(n.get("title") or "")[:120],
            "merge": _GH_MERGE.get(n.get("mergeable"), "unknown"), "approvals": approvals, "changes": changes,
            "ci": _GH_CI.get(roll), "draft": bool(n.get("isDraft")), "updated": core.iso_epoch(n.get("updatedAt")) or 0,
            "url": str(n.get("url") or "")}


def github_prs(http, token: str, n: int, api: str = "https://api.github.com") -> dict:
    """Your open PRs and the ones waiting on your review, in one GraphQL POST (a query: read-only)."""
    r = http(api.rstrip("/") + "/graphql", method="POST", timeout=12, redirects=False,
             headers={"Authorization": "bearer " + token},
             json_body={"query": _GH_QUERY, "variables": {"mine": _GH_SEARCH + "author:@me",
                                                          "review": _GH_SEARCH + "review-requested:@me", "n": n}})
    body = r.body if isinstance(r.body, dict) else {}
    d = body.get("data") or {}
    if not r.ok or not d:
        msg = body.get("message") or ((body.get("errors") or [{}])[0] or {}).get("message") or "HTTP %d" % r.status
        raise ForgeError("unauthorized" if r.status == 401 else str(msg)[:100])
    out = {}
    for key in ("mine", "review"):  # nodes are null where SAML or permissions hide a PR
        res = d.get(key) or {}
        rows = [gh_row(x) for x in res.get("nodes") or [] if isinstance(x, dict) and x.get("number")]
        out[key], out[key + "_total"] = rows, int(res.get("issueCount") or len(rows))
    return out


def gl_row(m, approvals=None, pipelines=None) -> dict:
    """A GitLab MR (REST) → the shared row shape. approvals and pipelines are the
    extra GETs made for your own MRs; without them, CI is read off detailed_merge_status."""
    dms = str(m.get("detailed_merge_status") or "")
    ms = str(m.get("merge_status") or "")  # GitLab before 15.6 has no detailed_merge_status
    if m.get("has_conflicts") or dms in ("conflict", "broken_status") or (not dms and ms == "cannot_be_merged"):
        merge = "conflicts"
    elif dms in _GL_UNCHECKED or (not dms and ms != "can_be_merged"):
        merge = "unknown"
    else:
        merge = "mergeable"  # no conflicts; other blockers (approvals, discussions) are not git's business
    if isinstance(pipelines, list):
        ci = _GL_CI.get((pipelines[0] or {}).get("status")) if pipelines and isinstance(pipelines[0], dict) else None
    else:
        ci = {"ci_still_running": "pending", "ci_must_pass": "failure"}.get(dms)
    by = approvals.get("approved_by") if isinstance(approvals, dict) else None
    return {"forge": "gl", "ref": (m.get("references") or {}).get("full") or "!%s" % m.get("iid"), "n": m.get("iid"),
            "title": str(m.get("title") or "")[:120], "merge": merge, "approvals": len(by) if isinstance(by, list) else 0,
            "changes": 1 if dms == "requested_changes" else 0, "ci": ci,
            "draft": bool(m.get("draft") or m.get("work_in_progress")),
            "updated": core.iso_epoch(m.get("updated_at")) or 0, "url": str(m.get("web_url") or "")}


def _header(headers, name: str):
    return next((v for k, v in (headers or {}).items() if k.lower() == name.lower()), None)


def gitlab_prs(http, token: str, base: str, n: int, detail: int = 10) -> dict:
    """Your open MRs and the ones you are a reviewer on, from GitLab's REST API (GETs
    only). Approvals and the latest pipeline are fetched for your first `detail` MRs."""
    import urllib.parse
    api = base.rstrip("/") + "/api/v4"
    # Bearer, not PRIVATE-TOKEN: core.http knows Authorization is a credential and
    # never follows a redirect with it (urllib would forward a custom header).
    hdr = {"Authorization": "Bearer " + token}

    def get(path):
        r = http(api + path, headers=hdr, timeout=10, redirects=False)
        if r.status == 401:
            raise ForgeError("unauthorized")
        if not r.ok:
            msg = r.body.get("message") if isinstance(r.body, dict) else None
            raise ForgeError(str(msg or "HTTP %d" % r.status)[:100])
        return r
    who = get("/user").body
    me = who.get("username") if isinstance(who, dict) else None
    if not me:
        raise ForgeError("no username from /user")
    q = "state=opened&order_by=updated_at&sort=desc&per_page=%d" % n
    lists = {"mine": get("/merge_requests?scope=created_by_me&" + q),
             "review": get("/merge_requests?scope=all&reviewer_username=%s&%s" % (urllib.parse.quote(me), q))}
    mrs = {k: [m for m in (r.body if isinstance(r.body, list) else []) if isinstance(m, dict)] for k, r in lists.items()}
    jobs = {}
    for m in mrs["mine"][:max(0, int(detail))]:
        if m.get("project_id") and m.get("iid"):
            p = "/projects/%s/merge_requests/%s" % (m["project_id"], m["iid"])
            jobs["a%s" % m.get("id")] = (lambda p=p: get(p + "/approvals").body)
            jobs["p%s" % m.get("id")] = (lambda p=p: get(p + "/pipelines?per_page=1").body)
    extra = core.parallel(jobs, timeout=10) if jobs else {}  # a failed extra GET just leaves that field unknown
    out = {"mine": [gl_row(m, extra.get("a%s" % m.get("id")), extra.get("p%s" % m.get("id"))) for m in mrs["mine"]],
           "review": [gl_row(m) for m in mrs["review"]]}
    for k, r in lists.items():
        total = _header(r.headers, "X-Total")
        out[k + "_total"] = int(total) if str(total or "").isdigit() else len(out[k])
    return out


def pr_alerts(mine) -> list:
    """Your own PRs that need you: conflicts, changes requested, failing CI. Drafts never alert."""
    ready = [m for m in mine if not m.get("draft")]
    out = []
    conf = [m["ref"] for m in ready if m.get("merge") == "conflicts"]
    chg = [m["ref"] for m in ready if m.get("changes")]
    ci = [m["ref"] for m in ready if m.get("ci") == "failure"]
    if conf:
        out.append({"sev": "warn", "msg": "%d of your PRs %s merge conflicts (%s)" % (
            len(conf), "has" if len(conf) == 1 else "have", ", ".join(conf[:4]))})
    if chg:
        out.append({"sev": "warn", "msg": "changes requested on %s" % ", ".join(chg[:4])})
    if ci:
        out.append({"sev": "warn", "msg": "CI failing on %s" % ", ".join(ci[:4])})
    return out


def _guard(fn):
    """A forge fetch that returns {"error"} instead of raising (core.parallel would
    turn an exception into a silent None)."""
    def run():
        try:
            return fn()
        except ForgeError as e:
            return {"error": str(e)}
        except OSError as e:  # DNS, refused, TLS, timeout: URLError is an OSError
            return {"error": core.net_reason(e)}
        except Exception as e:  # noqa: BLE001 — a malformed reply must not hide behind "timeout"
            return {"error": "%s: %s" % (type(e).__name__, e)}
    return run


def _repo(ref: str) -> str:
    return re.split(r"[#!]", ref, maxsplit=1)[0]


def gitlab_base(cfg, env) -> str:
    """gitlab_url, else the instance glab is pointed at (GITLAB_HOST, GL_HOST,
    GITLAB_URI), else gitlab.com: a token minted for a self-managed instance must
    never be sent to another host."""
    url = str(cfg.get("gitlab_url") or env.get("GITLAB_HOST") or env.get("GL_HOST") or env.get("GITLAB_URI")
              or "https://gitlab.com").strip().rstrip("/")
    return url if "://" in url else "https://" + url


@source("prs", ttl=900, timeout=40, scope="global", defaults={
    "github": True, "github_url": "https://api.github.com", "gitlab": "auto", "gitlab_url": "",
    "repos": [], "limit": 20, "detail": 10})
def prs(ctx):
    """Your open pull/merge requests on GitHub and GitLab, and the ones waiting on your review."""
    import urllib.parse
    cfg, env = ctx.cfg, ctx.env
    n = max(1, min(50, int(cfg.get("limit", 20))))
    jobs, errors = {}, {}
    if cfg.get("github", True) is not False:
        api = str(cfg.get("github_url") or "https://api.github.com")
        host = urllib.parse.urlsplit(api).hostname or "github.com"
        tok = github_token(env, "github.com" if host == "api.github.com" else host)
        if tok:  # no token anywhere: GitHub is simply not set up here (not an error)
            jobs["gh"] = lambda: github_prs(ctx.http, tok, n, api)
    gl, gl_tok = cfg.get("gitlab", "auto"), env.get("GITLAB_TOKEN")
    if gl is True or (gl == "auto" and gl_tok):
        if gl_tok:
            jobs["gl"] = lambda: gitlab_prs(ctx.http, gl_tok, gitlab_base(cfg, env), n, int(cfg.get("detail", 10)))
        else:
            errors["gl"] = "no GITLAB_TOKEN"
    res = core.parallel({k: _guard(fn) for k, fn in jobs.items()}, timeout=30)
    prev = ctx.prev or {}
    mine, review, counts, ok = [], [], {}, []
    for forge in sorted(jobs):
        r = res.get(forge) or {"error": "timeout"}
        if r.get("error"):
            errors[forge] = secrets.redact(str(r["error"]), env, 120)
            # Off VPN a self-hosted GitLab is out of reach while GitHub is not: keep
            # that forge's last rows (its entry in `errors` says they are old).
            mine += [m for m in prev.get("mine") or [] if m.get("forge") == forge]
            review += [m for m in prev.get("review") or [] if m.get("forge") == forge]
            if forge in (prev.get("counts") or {}):
                counts[forge] = prev["counts"][forge]
            continue
        ok.append(forge)
        mine, review = mine + r["mine"], review + r["review"]
        counts[forge] = {"mine": r["mine_total"], "review": r["review_total"]}
    if errors and not ok:  # off the network or unauthorized everywhere: keep the last list
        return {"error": "; ".join("%s: %s" % kv for kv in sorted(errors.items())), "status": "unknown"}
    pats = [str(p) for p in cfg.get("repos") or [] if str(p).strip()]
    if pats:
        mine = [m for m in mine if any(fnmatch.fnmatchcase(_repo(m["ref"]), p) for p in pats)]
        review = [m for m in review if any(fnmatch.fnmatchcase(_repo(m["ref"]), p) for p in pats)]
    mine.sort(key=lambda m: -(m.get("updated") or 0))
    review.sort(key=lambda m: -(m.get("updated") or 0))
    data = {"mine": mine[:n], "review": review[:n], "forges": sorted(jobs), "counts": counts,
            "open": len(mine) if pats else sum(c.get("mine", 0) for c in counts.values()),
            "review_open": len(review) if pats else sum(c.get("review", 0) for c in counts.values())}
    if errors:
        data["errors"] = errors
    al = pr_alerts(data["mine"])
    if al:
        data["alerts"] = al
    return data


# ── Reachability: ssh routes and the endpoints you depend on ────────────────
_CURL_WHY = {5: "no proxy dns", 6: "no dns", 7: "refused", 28: "timeout", 35: "tls error", 52: "empty reply",
             56: "reset", 60: "tls error", 97: "socks error"}


def parse_ssh_g(text: str) -> dict:
    """`ssh -G` output → {hostname, port, proxyjump, proxycommand} (first value wins, "none" dropped)."""
    out = {}
    for line in (text or "").splitlines():
        k, _, v = line.strip().partition(" ")
        k = k.lower()
        if k in ("hostname", "port", "proxyjump", "proxycommand") and k not in out:
            out[k] = v.strip()
    for k in ("proxyjump", "proxycommand"):
        if out.get(k, "").lower() in ("", "none"):
            out.pop(k, None)
    return out


def _hop(spec: str) -> tuple:
    """The first ProxyJump hop, '[ssh://][user@]host[:port][,next...]' → (host, port or '')."""
    spec = spec.split(",")[0].strip()
    if spec.startswith("ssh://"):
        spec = spec[6:]
    spec = spec.rsplit("@", 1)[-1]
    if spec.startswith("["):  # [IPv6]:port
        host, _, rest = spec[1:].partition("]")
        return host, rest.lstrip(":")
    if spec.count(":") == 1:
        host, port = spec.split(":")
        return host, port
    return spec, ""


def ssh_config(alias: str) -> dict:
    ssh = core.which("ssh") or "/usr/bin/ssh"
    rc, out, _ = core.run([ssh, "-G", alias], 3)
    return parse_ssh_g(out) if rc == 0 else {}


def ssh_route(alias: str, timeout: float = 3.0) -> dict:
    """Prove the network route to an ssh alias without logging in: resolve it offline
    (`ssh -G`), then open a TCP connection to its HostName:Port, or to the first
    ProxyJump hop when it has one (that is where the packets go). A ProxyCommand
    cannot be probed from outside, so that route reads as unknown (ok None)."""
    row = {"alias": alias}
    cfg = ssh_config(alias)
    if not cfg.get("hostname"):
        return dict(row, ok=False, error="no ssh config")
    for _ in range(3):  # a hop can itself jump: follow up to three
        if not cfg.get("proxyjump"):
            break
        host, port = _hop(cfg["proxyjump"])
        hop = ssh_config(host)  # a hop is usually an alias too
        row["via"] = host
        cfg = dict(hop, hostname=hop.get("hostname") or host, port=port or hop.get("port") or "22")
    if cfg.get("proxycommand"):
        return dict(row, ok=None, error="proxied")
    port = str(cfg.get("port") or "22")
    row.update(host=cfg["hostname"], port=int(port) if port.isdigit() else 22)
    t0 = time.monotonic()
    try:
        socket.create_connection((row["host"], row["port"]), timeout=timeout).close()
    except OSError as e:
        return dict(row, ok=False, error=core.net_reason(e))
    return dict(row, ok=True, ms=int((time.monotonic() - t0) * 1000))


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        return True
    except (OSError, UnicodeError):
        return False


def get_direct(http, url: str, verify: bool, timeout: float = 6.0) -> tuple:
    """(status, ms, why). Redirects are not followed: a 302 to a login page is up."""
    try:
        r = http(url, timeout=timeout, headers={"Accept": "*/*"}, verify=verify, redirects=False, raw=True)
    except Exception as e:  # noqa: BLE001 — DNS, refused, TLS, timeout
        return 0, 0, core.net_reason(e)
    return r.status, r.ms, None


def get_socks(url: str, verify: bool, proxy, timeout: float = 6.0) -> tuple:
    """The same GET through a SOCKS5 proxy (`ssh -D`); the name resolves at the far
    end, so a VPC-only hostname works too."""
    curl = core.which("curl")
    if not curl:
        return 0, 0, "no curl"
    addr = str(proxy) if ":" in str(proxy) else "127.0.0.1:%s" % proxy
    rc, out, _ = core.run([curl, "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "--max-time",
                           "%g" % timeout, "--socks5-hostname", addr] + ([] if verify else ["-k"]) + [url], timeout + 2)
    parts = out.split()
    if rc == 0 and len(parts) == 2 and parts[0].isdigit() and int(parts[0]) > 0:
        return int(parts[0]), int(float(parts[1]) * 1000), None
    return 0, 0, _CURL_WHY.get(rc, "curl rc %d" % rc)


def endpoint(t: dict, socks, http) -> dict:
    """One endpoint: DNS, a direct GET, and on a transport failure the SOCKS proxy."""
    import urllib.parse
    label, url = str(t.get("label") or t["url"]), str(t["url"])
    verify = bool(t.get("verify", True))
    dns = _resolves(urllib.parse.urlsplit(url).hostname or "")
    status, ms, err = get_direct(http, url, verify) if dns else (0, 0, "no dns")
    via = "direct"
    if not status and socks:
        s2, ms2, e2 = get_socks(url, verify, socks)
        if s2:
            status, ms, via = s2, ms2, "socks"
        else:
            err = err or e2
    return {"label": label, "url": url, "status": status, "ms": ms, "ok": 0 < status < 500, "dns": dns,
            "via": via if status else None, "error": None if status else err}


def socks_from_procs():
    """The port of a live `ssh -D` SOCKS proxy from the procs census, or None."""
    from .. import live
    for t in (registry.Cache().data("procs") or {}).get("tunnels") or []:
        if t.get("kind") == "D" and t.get("ports") and t.get("pid") and live.alive(int(t["pid"])):
            return str(t["ports"][0])
    return None


@source("reach", ttl=600, timeout=20, optin=True, defaults={"ssh": [], "endpoints": [], "socks_port": None})
def reach(ctx):
    """ssh routes (a TCP connect to each alias, no login) and endpoints, direct or through a SOCKS proxy."""
    cfg = ctx.cfg
    aliases = [str(a).strip() for a in cfg.get("ssh") or [] if str(a).strip()]
    eps = []
    for e in cfg.get("endpoints") or []:
        e = {"url": e} if isinstance(e, str) else e
        if isinstance(e, dict) and str(e.get("url") or "").startswith(("http://", "https://")):
            eps.append(e)
    socks = str(cfg.get("socks_port") or "") or socks_from_procs()
    jobs = {"ssh%d" % i: (lambda a=a: ssh_route(a)) for i, a in enumerate(aliases)}
    jobs.update({"ep%d" % i: (lambda e=e: endpoint(e, socks, ctx.http)) for i, e in enumerate(eps)})
    res = core.parallel(jobs, timeout=16) if jobs else {}
    ssh = [res.get("ssh%d" % i) or {"alias": a, "ok": False, "error": "timeout"} for i, a in enumerate(aliases)]
    rows = [res.get("ep%d" % i) or {"label": str(e.get("label") or e["url"]), "url": e["url"], "status": 0, "ms": 0,
                                     "ok": False, "error": "timeout"} for i, e in enumerate(eps)]
    data = {"ssh": ssh, "endpoints": rows, "socks": socks}
    # Two strikes: a 5xx now and on the previous run (a deploy restarts things). No
    # route at all is being off the VPN: state, never an alert.
    was = {r.get("label") for r in (ctx.prev or {}).get("endpoints") or [] if (r.get("status") or 0) >= 500}
    bad = [r for r in rows if r["status"] >= 500 and r["label"] in was]
    if bad:
        data["alerts"] = [{"sev": "warn", "msg": "%s answers HTTP %d" % (r["label"], r["status"])} for r in bad]
    return data

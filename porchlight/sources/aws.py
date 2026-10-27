"""AWS: what runs in the account, what it costs by the hour, and the month's budget.

The aws_account source reads one account, the first profile STS accepts, through the
AWS CLI (no SDK: the credential chain, SSO included, is the CLI's own), with free,
read-only calls only. A test audits this file for exactly these commands:

  sts get-caller-identity           who you are
  ec2 describe-regions              the account's enabled regions, once a day
  ec2 describe-instances            every instance that is not terminated
  ec2 describe-volumes              EBS volumes, attached or loose
  ec2 describe-addresses            Elastic IPs (home and active regions)
  ec2 describe-hosts                dedicated hosts
  ec2 describe-spot-price-history   the current price of each running spot instance
  pricing get-products              list prices (aws_prices.py; cached a week)
  budgets describe-budgets          the account's cost budgets

The home regions, and every region that held something last run, are read every
run; the account's other enabled regions are swept a few per run, round robin, so a
box started anywhere shows up within hours, flagged while it sits outside the home
regions. Each aws process peaks near 130 MB, so at most max_concurrent run at once.

Cost Explorer bills per request; aws_cost.py makes at most one a day.
"""
from __future__ import annotations

import json
import re
import time

from .. import core
from . import aws_prices as P
from . import saas  # _now via the module, so a test pinning saas._now reaches this file too
from .saas import ProbeError, _cred, _d, _dicts, _f, _fail, _num, _pmap, _res, _saas, _scrub, _strs, _verdict

CLI_TIMEOUT = 12.0     # one aws process: its own start-up is most of it
RUN_BUDGET = 38.0      # the whole run, well inside the collector's 50 s deadline
REGIONS_TTL = 86400    # describe-regions, once a day
MAX_PROFILES = 8
IDENTITY_BUDGET = 20.0  # seconds to find a profile STS accepts
PRICE_LOOKUPS = 24     # price keys looked up per run; a large account fills its cache over a few runs
# The first CLI whose budgets model has FilterExpression (2.26.6) and BillingViewArn (2.28.4);
# v1 is judged by the botocore it prints (1.37.38, 1.40.4). An older one drops both from replies.
BUDGET_FILTERS_CLI, BUDGET_FILTERS_BOTOCORE = (2, 28, 4), (1, 40, 4)
LIVE = ("pending", "running")
_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d{1,2}$")
_ACCOUNT = re.compile(r"^\d{12}$")
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_STOPPED_AT = re.compile(r"\((\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) GMT\)")
_NOT_GONE = "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down"
_Q_INST = ("Reservations[].Instances[].{id:InstanceId,n:Tags[?Key=='Name']|[0].Value,t:InstanceType,s:State.Name,"
           "l:LaunchTime,why:StateTransitionReason,life:InstanceLifecycle,os:PlatformDetails,"
           "az:Placement.AvailabilityZone,ten:Placement.Tenancy,ip:PublicIpAddress}")
_Q_VOL = "Volumes[].{id:VolumeId,gb:Size,ty:VolumeType,iops:Iops,mbps:Throughput,att:Attachments[0].InstanceId}"
_Q_EIP = "Addresses[].{ip:PublicIp,inst:InstanceId}"
_Q_HOST = ("Hosts[?State!='released'].{id:HostId,fam:HostProperties.InstanceFamily,ty:HostProperties.InstanceType,"
           "st:State,at:AllocationTime,n:length(Instances || `[]`)}")
_Q_BUDGETS = ("Budgets[?BudgetType=='COST'].{n:BudgetName,u:TimeUnit,lim:BudgetLimit.Amount,cur:BudgetLimit.Unit,"
              "act:CalculatedSpend.ActualSpend.Amount,fc:CalculatedSpend.ForecastedSpend.Amount,"
              "up:LastUpdatedTime,f:CostFilters,fx:FilterExpression,view:BillingViewArn}")


# ── The AWS CLI ─────────────────────────────────────────────────────────────
def aws_bin(ctx):
    return core.which("aws", tuple(ctx.cfg.get("paths") or ()))


def masked(text) -> str:
    """Text fit to keep: any 12-digit account id cut to its last four digits. The AWS SSO
    wizard's default profile names carry one ("AdministratorAccess-123456789012"), and an
    access-denied error quotes the caller's ARN."""
    return _ACCOUNT_ID.sub(lambda m: "…" + m.group(0)[-4:], str(text if text is not None else ""))


def _aws_err(stderr) -> str:
    """A failed aws call in a word (sso-expired, access-denied...) or a short line, with no
    whole account id in it."""
    lines = (stderr or "").strip().splitlines()
    low = (lines[-1] if lines else "").lower()
    if "token has expired" in low or ("sso" in low and "expired" in low):
        return "sso-expired"
    if "unable to locate credentials" in low:
        return "no-credentials"
    if "expiredtoken" in low or ("security token" in low and "expired" in low):
        return "credentials-expired"
    if "accessdenied" in low or "unauthorizedoperation" in low or "not authorized" in low:
        return "access-denied"
    if "could not be found" in low and "profile" in low:
        return "no-such-profile"
    return masked(_scrub(lines[-1] if lines else "aws failed", 80))


def cli(ctx, args, profile, region=None, timeout=CLI_TIMEOUT, env=None, read_timeout=10, raw=False):
    """Every aws call the AWS sources make, as JSON. No pager, no prompt, no instance
    metadata probe, short connect and read timeouts; the child sees the AWS settings
    and no other secret (service bearer tokens included). Raises ProbeError, whose
    text never holds a whole account id. raw: what it printed, as one line of text
    (`--version`, which prints no JSON and calls no API)."""
    from .. import secrets
    b = aws_bin(ctx)
    if not b:
        raise ProbeError("aws CLI not found")
    argv = [b] + args
    if not raw:
        argv += ["--output", "json", "--cli-connect-timeout", "4", "--cli-read-timeout", str(int(read_timeout))]
    if profile:
        argv += ["--profile", profile]
    if region:
        argv += ["--region", region]
    child = secrets.child_env(ctx.env, keep=("AWS_",))
    child.update(AWS_EC2_METADATA_DISABLED="true", AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off")
    child.update(env or {})
    rc, out, err = ctx.run(argv, timeout=timeout, env=child)
    if rc in (124, 126, 127):
        raise ProbeError({124: "timeout", 126: "cannot run aws", 127: "aws CLI not found"}[rc])
    if rc != 0:
        raise ProbeError(_aws_err(err))
    if raw:
        return " ".join(((out or "") + " " + (err or "")).split())[:200]
    try:
        return json.loads(out or "null")
    except ValueError:
        raise ProbeError("aws: bad JSON") from None


def config_profiles(env) -> list:
    """[(profile, region)]: the AWS config file's profiles in its order ([sso-session] and
    [services] blocks are not profiles), then any the credentials file adds. Section
    names only: no key is ever read out of either file."""
    regions, cur = {}, None
    for line in core.read_text(core.expand(env.get("AWS_CONFIG_FILE") or "~/.aws/config")).splitlines():
        m = re.match(r"^\s*\[\s*(default|profile\s+([^\]\s]+))\s*\]\s*$", line)
        if m:
            cur = m.group(2) or "default"
            regions.setdefault(cur, "")
        elif re.match(r"^\s*\[", line):
            cur = None
        elif cur:
            m = re.match(r"^\s*region\s*=\s*([a-z0-9\-]+)\s*$", line)
            if m and not regions[cur]:
                regions[cur] = m.group(1)
    for line in core.read_text(core.expand(env.get("AWS_SHARED_CREDENTIALS_FILE") or "~/.aws/credentials")).splitlines():
        m = re.match(r"^\s*\[\s*([^\]\s]+)\s*\]\s*$", line)
        if m:
            regions.setdefault(m.group(1), "")
    return list(regions.items())


def identity(ctx, cfg, last=None) -> tuple:
    """(profile, its region, account id, principal type, {profile: error}) for the first
    profile STS accepts: `profile` when set (and then only that one: the dashboard never
    falls back to another identity, an admin login, on its own), else AWS_PROFILE, last
    run's profile, then the AWS files' profiles in order. No profile at all: the CLI's
    own chain (environment credentials). `last` is the stored, masked, name. The account
    id is noted as a credential, so the result keeps no more of it than its last four
    digits, and the errors are keyed by masked names."""
    known = dict(config_profiles(ctx.env))
    want = str(cfg.get("profile") or "").strip()
    if want:
        cands = [want]
    else:  # last run's profile, recognized by its masked name, while the AWS files still have it
        first = [ctx.env["AWS_PROFILE"]] if ctx.env.get("AWS_PROFILE") else []
        first += [p for p in known if last and masked(p) == last]
        cands = list(dict.fromkeys(first + list(known)))[:MAX_PROFILES] or [None]
    errs, t0 = {}, time.monotonic()
    for p in cands:
        label = masked(p or "default")
        left = IDENTITY_BUDGET - (time.monotonic() - t0)
        if left < 2:  # expired logins can each take a while to fail: leave the rest of the run its time
            errs[label] = "not tried: out of time"
            continue
        try:
            ident = _d(cli(ctx, ["sts", "get-caller-identity"], p, timeout=min(CLI_TIMEOUT, left)))
        except ProbeError as e:
            errs[label] = str(e)
            continue
        acct, arn = str(ident.get("Account") or ""), str(ident.get("Arn") or "")
        if _ACCOUNT.match(acct):
            _cred(ctx, acct)
            kind = arn.split(":")[5].split("/")[0] if arn.count(":") >= 5 else "?"
            return p, known.get(p or "default", ""), acct, kind, errs
        errs[label] = "no account in the reply"
    return None, "", "", "", errs


def login_fix(errs) -> str:
    """The command that renews an expired SSO login among the profiles tried, or ""."""
    p = next((p for p, e in errs.items() if e == "sso-expired"), None)
    if p is None:
        return ""
    return "aws sso login" + ("" if p == "default" else " --profile %s" % p)  # a masked name: shown, never run


# ── Regions ─────────────────────────────────────────────────────────────────
def home_regions(cfg, env, profile_region="") -> list:
    """home_regions, else the profile's region, else AWS_REGION / AWS_DEFAULT_REGION, else us-east-1."""
    want = [r for r in _strs(cfg.get("home_regions")) if _REGION.match(r)]
    if want:
        return list(dict.fromkeys(want))
    r = profile_region or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1"
    return [r] if _REGION.match(r) else ["us-east-1"]


def enabled_regions(ctx, prof, home, st, now) -> tuple:
    """(the account's enabled regions, when they were read): describe-regions, once a day."""
    known = [r for r in _strs(st.get("enabled")) if _REGION.match(r)]
    if known and now - _num(st, "at", 0) < REGIONS_TTL:
        return known, st["at"]
    try:
        names = cli(ctx, ["ec2", "describe-regions", "--query", "Regions[].RegionName"], prof, home[0])
        names = sorted({str(n) for n in names if _REGION.match(str(n))}) if isinstance(names, list) else []
        if names:
            return names, now
    except ProbeError:
        pass
    return known or sorted(home), _num(st, "at", 0)


def plan(home, enabled, st, sweep: int = 2) -> tuple:
    """(regions read in full this run, regions swept this run, the next sweep cursor). In
    full: the home regions and the ones that held something last run. Swept: the next
    `sweep` of the account's other enabled regions, round robin."""
    full = sorted(set(home) | set(_strs(st.get("active"))))
    rest = [r for r in enabled if r not in full]
    if not rest or sweep <= 0:
        return full, [], 0
    i = int(_num(st, "cursor", 0)) % len(rest)
    picked = (rest[i:] + rest[:i])[:sweep]
    return full, picked, (i + len(picked)) % len(rest)


def read_regions(ctx, prof, full, sweep, workers: int, timeout: float) -> dict:
    """{region: {inst, vols, eips, hosts, err}}. A swept region skips Elastic IPs: a stray
    instance, volume or dedicated host is what the sweep is for."""
    calls = {}
    for r in list(full) + list(sweep):
        calls[(r, "inst")] = lambda r=r: cli(ctx, ["ec2", "describe-instances", "--filters", _NOT_GONE, "--query", _Q_INST], prof, r)
        calls[(r, "vols")] = lambda r=r: cli(ctx, ["ec2", "describe-volumes", "--query", _Q_VOL], prof, r)
        calls[(r, "hosts")] = lambda r=r: cli(ctx, ["ec2", "describe-hosts", "--query", _Q_HOST], prof, r)
        if r in full:
            calls[(r, "eips")] = lambda r=r: cli(ctx, ["ec2", "describe-addresses", "--query", _Q_EIP], prof, r)
    out = {}
    for (r, what), (v, e) in _pmap(calls, timeout=timeout, workers=workers).items():
        reg = out.setdefault(r, {"inst": [], "vols": [], "eips": [], "hosts": [], "err": {}})
        if e:
            reg["err"][what] = e
        else:
            reg[what] = _dicts(v)
    return out


# ── Inventory and money ─────────────────────────────────────────────────────
def _text(v, n: int = 28) -> str:
    """User-set text (a Name tag) made safe for one terminal line."""
    return _CONTROL.sub(" ", str(v or "")).strip()[:n]


def _epoch(stamp) -> int:
    return int(saas._ts(str(stamp or "").replace(" ", "T")) or 0)


def instance(x, region) -> dict:
    state = _text(x.get("s"), 16) or "?"
    if state in LIVE:
        since = _epoch(x.get("l"))
    else:  # "User initiated (2026-10-25 07:07:58 GMT)": when it stopped
        m = _STOPPED_AT.search(str(x.get("why") or ""))
        since = _epoch("%sT%sZ" % m.groups()) if m else 0
    return {"id": _text(x.get("id"), 24), "name": _text(x.get("n")) or _text(x.get("id"), 24),
            "type": _text(x.get("t"), 24), "state": state, "region": region, "az": _text(x.get("az"), 24),
            "spot": x.get("life") == "spot", "platform": _text(x.get("os"), 40),
            "tenancy": _text(x.get("ten"), 12) or "default", "since": since, "_ip": x.get("ip")}


def _od_key(i):
    if i["state"] in LIVE and not i["spot"] and i["platform"] in P.PLATFORMS and i["tenancy"] in P.TENANCY:
        return "od|%s|%s|%s|%s" % (i["region"], i["type"], i["platform"], P.TENANCY[i["tenancy"]])
    return None


def _spot_key(i):
    if i["state"] in LIVE and i["spot"] and i["platform"] in P.PLATFORMS:
        return (i["region"], i["az"], i["type"], P.PLATFORMS[i["platform"]][2])
    return None


def price_keys(instances, vols, hosts) -> set:
    keys = {_od_key(i) for i in instances} - {None}
    keys |= {"ebs|%s|%s" % (r, v.get("ty")) for r, v in vols if v.get("ty") in P.EBS_TYPES}
    keys |= {"host|%s|%s" % (r, h.get("fam")) for r, h in hosts if h.get("fam")}
    return keys


def price_list(ctx, prof):
    """plist(pairs) for aws_prices: the Price List API answers from us-east-1."""
    def plist(pairs):
        out = cli(ctx, ["pricing", "get-products", "--service-code", "AmazonEC2", "--max-items", "10",
                        "--filters"] + P.filters(pairs), prof, "us-east-1")
        return [json.loads(s) for s in _d(out).get("PriceList") or [] if isinstance(s, str)]
    return plist


def spot_prices(ctx, prof, instances, workers: int, timeout: float) -> dict:
    """{(region, az, type, product): $/h} for the running spot instances (spot price history is free)."""
    want = {_spot_key(i) for i in instances} - {None}
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(saas._now()))
    res = _pmap({w: (lambda w=w: cli(ctx, [
        "ec2", "describe-spot-price-history", "--instance-types", w[2], "--product-descriptions", w[3],
        "--availability-zone", w[1], "--start-time", stamp, "--query", "SpotPriceHistory[0].SpotPrice"],
        prof, w[0])) for w in want}, timeout=timeout, workers=workers) if want else {}
    return {w: _f(v) for w, (v, e) in res.items() if not e and _f(v)}


def costs(instances, vols, eips, hosts, prices, spot) -> tuple:
    """Fill each instance's usd_h (compute) and disk_usd_mo; return (burn, idle).
    burn: what accrues per hour now: running instances, every volume, each public IPv4
    address, dedicated hosts. idle: what accrues per month for nothing: stopped instances'
    disks and addresses, loose volumes and addresses, hosts with no instance on them."""
    by_id = {i["id"]: i for i in instances}
    unpriced, idle, disk_mo, loose_mo = [], {}, {}, 0.0
    for r, v in vols:
        usd = P.volume_usd_mo(v, prices.get("ebs|%s|%s" % (r, v.get("ty"))))
        if usd is None:
            unpriced.append(_text(v.get("id"), 24) or "volume")
            continue
        att = by_id.get(v.get("att") or "")
        if att:
            disk_mo[att["id"]] = disk_mo.get(att["id"], 0.0) + usd
        else:
            loose_mo += usd
    eip_insts = {e.get("inst") for _, e in eips if e.get("inst")}
    public = {e.get("ip") for _, e in eips if e.get("ip")}
    compute = 0.0
    for i in instances:
        i["disk_usd_mo"] = round(disk_mo.get(i["id"], 0.0), 2)
        if i["state"] not in LIVE:
            i["usd_h"] = None
            extra = P.IPV4_USD_H * P.MONTH_H if i["id"] in eip_insts else 0.0
            if i["disk_usd_mo"] or extra:
                idle[i["name"]] = idle.get(i["name"], 0.0) + i["disk_usd_mo"] + extra
            continue
        if i["_ip"]:
            public.add(i["_ip"])
        if i["tenancy"] == "host":
            i["usd_h"], i["price"] = 0.0, "host"  # the dedicated host is what bills
        elif i["spot"]:
            i["usd_h"], i["price"] = spot.get(_spot_key(i)), "spot"
        else:
            i["usd_h"], i["price"] = (prices.get(_od_key(i) or "") or {}).get("usd_h"), "od"
        if i["usd_h"] is None:
            unpriced.append(i["name"])
        else:
            compute += i["usd_h"]
    host_h = 0.0
    for r, h in hosts:
        usd = (prices.get("host|%s|%s" % (r, h.get("fam"))) or {}).get("usd_h")
        h["usd_h"] = usd
        if usd is None:
            unpriced.append(_text(h.get("fam")) or "host")
            continue
        host_h += usd
        if not h.get("n"):
            key = "host " + (_text(h.get("fam")) or "?")
            idle[key] = idle.get(key, 0.0) + usd * P.MONTH_H
    if loose_mo:
        idle["unattached volumes"] = loose_mo
    loose_ips = [e for _, e in eips if not e.get("inst")]
    if loose_ips:
        idle["unassociated IPs"] = len(loose_ips) * P.IPV4_USD_H * P.MONTH_H
    disk_h = (sum(disk_mo.values()) + loose_mo) / P.MONTH_H
    ipv4_h = len(public) * P.IPV4_USD_H
    burn = {"compute_h": round(compute, 4), "disk_h": round(disk_h, 4), "ipv4_h": round(ipv4_h, 4),
            "host_h": round(host_h, 4), "total_h": round(compute + disk_h + ipv4_h + host_h, 4),
            "ipv4": len(public), "unpriced": unpriced[:8]}
    items = sorted(({"name": k, "usd_mo": round(v, 2)} for k, v in idle.items()), key=lambda d: -d["usd_mo"])
    return burn, {"usd_mo": round(sum(idle.values()), 2), "items": items[:12]}


def narrows(f) -> bool:
    """Whether a budget's FilterExpression narrows what it counts. The console's default for a
    new budget, {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}},
    only leaves out record types and still covers the whole account; any other filter narrows
    it (a service, a tag, a cost category, a Bedrock model, or a kind this code does not know).
    An empty leaf still filters ({"Tags": {}} selects the untagged costs), and the AWS CLI drops
    a member its model does not know, so a filter of a kind AWS added later arrives as {}: that
    narrows too."""
    if f is None:
        return False
    if isinstance(f, list):
        return any(narrows(v) for v in f)
    if not isinstance(f, dict) or not f:
        return True
    for k, v in f.items():
        if k in ("And", "Or", "Not"):
            if narrows(v):
                return True
        elif not (k == "Dimensions" and isinstance(v, dict) and str(v.get("Key") or "").upper() == "RECORD_TYPE"):
            return True
    return False


def cli_version(line) -> str:
    """ "2.38.6", or "1.33.0 with botocore 1.40.4" for v1, from an `aws --version` line; "" if none."""
    m = re.search(r"aws-cli/(\d+\.\d+\.\d+)", line or "")
    b = re.search(r"botocore/(\d+\.\d+\.\d+)", line or "")
    return (m.group(1) + (" with botocore %s" % b.group(1) if b else "")) if m else ""


def budget_note(line) -> str:
    """The AWS panel's budget row for a CLI that cannot read budget filters: the fix first, so a
    narrow column keeps it."""
    have = cli_version(line)
    need = "aws CLI %s+" % ".".join(map(str, BUDGET_FILTERS_CLI))
    if "botocore" in have:
        need += " or botocore %s+" % ".".join(map(str, BUDGET_FILTERS_BOTOCORE))
    return "needs %s for budget filters (%s)" % (need, "has " + have if have else "version unknown")


def reads_budget_filters(line) -> bool:
    """Whether the CLI that printed this `aws --version` line keeps a budget's FilterExpression and
    BillingViewArn. One that cannot would show every filtered budget as the account's."""
    m = re.search(r"aws-cli/(\d+)\.(\d+)\.(\d+)", line or "")
    if not m:
        return False
    ver = tuple(int(g) for g in m.groups())
    if ver[0] != 1:
        return ver >= BUDGET_FILTERS_CLI
    b = re.search(r"botocore/(\d+)\.(\d+)\.(\d+)", line)
    return bool(b) and tuple(int(g) for g in b.groups()) >= BUDGET_FILTERS_BOTOCORE


def budgets(ctx, prof, acct, filters=True) -> list:
    """The account's COST budgets (the Budgets API is free; AWS refreshes the spend a few
    times a day). The account id goes to the CLI's argv, never into the result. A budget is
    scoped by its legacy CostFilters, by its FilterExpression, or by a billing view other than
    the account's primary one; the view's ARN holds the account id and is not kept.
    filters=False (a CLI too old to return those fields): no budget can be shown to cover the
    whole account, so every one is scoped."""
    out = []
    for b in _dicts(cli(ctx, ["budgets", "describe-budgets", "--account-id", acct, "--query", _Q_BUDGETS],
                        prof, "us-east-1")):
        lim, act, fc = _f(b.get("lim")), _f(b.get("act")), _f(b.get("fc"))
        if lim is None or act is None or str(b.get("cur") or "USD").upper() != "USD":
            continue
        view = str(b.get("view") or "")
        out.append({"name": _text(b.get("n"), 40), "unit": _text(b.get("u"), 12), "limit": round(lim, 2),
                    "actual": round(act, 2), "forecast": None if fc is None else round(fc, 2),
                    "updated": _epoch(b.get("up")),
                    "scoped": (not filters or any(str(k).lower() != "recordtype" for k in _d(b.get("f")))
                               or narrows(b.get("fx")) or (bool(view) and not view.endswith("/primary")))})
    return out[:12]


def account_budget(rows):
    """The budget that covers the whole account: monthly, filtered by nothing but record types
    (credits and refunds left out, say), the largest limit."""
    whole = [b for b in rows or () if isinstance(b, dict) and b.get("unit") == "MONTHLY" and not b.get("scoped")
             and isinstance(b.get("limit"), (int, float)) and isinstance(b.get("actual"), (int, float))]
    return max(whole, key=lambda b: b["limit"]) if whole else None


def alerts(instances, vols, hosts, home, budget, partial) -> list:
    """The warnings: over the account budget, anything outside the home regions, a
    dedicated host with no instance (it bills by the hour regardless), a home region
    that could not be read."""
    warn = []
    if budget and budget["limit"] and budget["actual"] > budget["limit"]:
        warn.append("%s spent this month, %d%% of the %s budget" % (
            core.money(budget["actual"]), round(100 * budget["actual"] / budget["limit"]), core.money(budget["limit"])))
    away = {}
    for r, what in [(i["region"], "instance") for i in instances] + [(r, "volume") for r, _ in vols] + \
            [(r, "dedicated host") for r, _ in hosts]:
        if r not in home:
            away.setdefault(r, {}).setdefault(what, 0)
            away[r][what] += 1
    for r, n in sorted(away.items()):
        bits = ", ".join("%d %s%s" % (c, w, "" if c == 1 else "s") for w, c in sorted(n.items()))
        warn.append("%s in %s, outside the home regions" % (bits, r))
    for r, h in hosts:
        if not h.get("n"):
            warn.append("dedicated host %s in %s has no instance%s" % (
                _text(h.get("fam")) or "?", r, " (%s/h)" % core.money(h["usd_h"]) if h.get("usd_h") else ""))
    if partial:
        warn.append("could not read %s (partial data)" % ", ".join(partial[:3]))
    return warn


@_saas("aws_account", "aws", ttl=3600, timeout=45, binaries=("aws",), defaults={
    "profile": "", "home_regions": [], "sweep": 2, "max_concurrent": 3, "cost_explorer": "off"})
def _aws_account(ctx):
    """AWS: instances in every enabled region at list price, the burn per hour, budgets"""
    if not aws_bin(ctx):
        return _fail("aws CLI not found", "aws CLI not installed")
    t0, cfg, now = time.monotonic(), ctx.cfg, saas._now()

    def left() -> float:
        return RUN_BUDGET - (time.monotonic() - t0)
    prev = _d(_d(ctx.prev).get("details"))
    prof, preg, acct, kind, errs = identity(ctx, cfg, prev.get("profile"))
    if not acct:  # a soft failure: the collector keeps the last inventory and shows why
        fix = login_fix(errs)
        why = ", ".join(sorted(set(errs.values()))) or "no profile"
        return _res("unknown", "no usable AWS credentials", {"profile_errors": errs},
                    "no usable AWS credentials: %s%s" % (why, " (%s)" % fix if fix else ""))
    workers = max(1, min(8, int(_num(cfg, "max_concurrent", 3))))
    st, home = _d(prev.get("regions")), home_regions(cfg, ctx.env, preg)
    enabled, enabled_at = enabled_regions(ctx, prof, home, st, now)
    full, sweep, cursor = plan(home, enabled, st, max(0, int(_num(cfg, "sweep", 2))))
    regs = read_regions(ctx, prof, full, sweep, workers, timeout=max(4.0, left() - 14))
    instances, vols, eips, hosts, errors, active = [], [], [], [], {}, set()
    for r, reg in sorted(regs.items()):
        instances += [instance(x, r) for x in reg["inst"]]
        vols += [(r, v) for v in reg["vols"]]
        eips += [(r, e) for e in reg["eips"]]
        hosts += [(r, h) for h in reg["hosts"]]
        if reg["err"]:
            errors[r] = masked("; ".join("%s: %s" % kv for kv in sorted(reg["err"].items())))
        if reg["inst"] or reg["vols"] or reg["eips"] or reg["hosts"] or (reg["err"] and r in full and r not in home):
            active.add(r)  # an active region that failed this run stays active
    pmap = lambda calls: _pmap(calls, timeout=max(1.0, left() - 8), workers=workers)  # noqa: E731
    prices, perrs = P.refresh(price_list(ctx, prof), prev.get("prices"), price_keys(instances, vols, hosts), now, pmap,
                              limit=PRICE_LOOKUPS if left() > 10 else 0)
    spot = spot_prices(ctx, prof, instances, workers, timeout=max(1.0, left() - 5)) if left() > 6 else {}
    burn, idle = costs(instances, vols, eips, hosts, prices, spot)
    # rows_ok: whether the rows were judged by a CLI that reads budget filters. Fresh rows take this
    # run's check; rows reused (out of time, or the budgets read failed) keep the verdict of the run
    # that made them, None for a record from before the check: trusted no further than that run.
    berr, ver = None, None
    rows_ok = prev.get("budget_filters") if isinstance(prev.get("budget_filters"), bool) else None
    try:
        if left() < 3:
            raise ProbeError("skipped: out of time")
        try:
            ver = cli(ctx, ["--version"], None, timeout=min(CLI_TIMEOUT, left()), raw=True)
        except ProbeError:
            ver = ""
        ok = reads_budget_filters(ver)
        brows, rows_ok = budgets(ctx, prof, acct, ok), ok
    except ProbeError as e:
        brows, berr = [b for b in prev.get("budgets") or [] if isinstance(b, dict)], masked(e)
    if rows_ok is not True:  # rows no capable CLI has judged: none of them is the account's
        brows = [dict(b, scoped=True) for b in brows]
    swept = {str(k): v for k, v in _d(st.get("swept")).items() if k in enabled}
    swept.update({r: int(now) for r in regs if not regs[r]["err"]})
    rank = {"running": 0, "pending": 0, "stopping": 1, "stopped": 2}
    instances.sort(key=lambda i: (rank.get(i["state"], 3), -(i.get("usd_h") or 0), -i["disk_usd_mo"], i["name"]))
    for i in instances:
        i.pop("_ip", None)  # public addresses are counted, never kept
    running = sum(1 for i in instances if i["state"] in LIVE)
    partial = sorted(r for r in errors if r in full)
    twice = [r for r in partial if r in _d(st.get("errors"))]  # two strikes: a single miss is a blip, not news
    d = {"profile": masked(prof or "default"), "account_last4": acct[-4:], "principal_type": kind, "instances": instances[:60],
         "hosts": [{"family": _text(h.get("fam")), "type": _text(h.get("ty")), "region": r, "state": _text(h.get("st"), 16),
                    "since": _epoch(h.get("at")), "instances": int(_num(h, "n", 0)), "usd_h": h.get("usd_h")}
                   for r, h in hosts][:12],
         "volumes": {"count": len(vols), "gb": sum(int(_num(v, "gb", 0)) for _, v in vols),
                     "loose": sum(1 for _, v in vols if not v.get("att"))},
         "burn": burn, "idle": idle, "budgets": brows,
         "regions": {"home": home, "enabled": enabled, "at": enabled_at, "active": sorted(active - set(home)),
                     "cursor": cursor, "swept": swept, "errors": errors},
         "prices": prices}
    if errs:
        d["profile_errors"] = errs  # profiles tried before the one that answered
    if berr:
        d["budget_error"] = berr
    if rows_ok is not None:
        d["budget_filters"] = rows_ok
    old = ver is not None and not reads_budget_filters(ver)  # this run's CLI cannot read them
    note = budget_note(ver) if old else (_text(prev.get("budget_note"), 120) if ver is None and rows_ok is False else "")
    if brows and note:  # this run's finding, or last run's when this run could not check
        d["budget_note"] = note
    if perrs:
        d["price_errors"] = {k: masked(v) for k, v in sorted(perrs.items())[:4]}
    status, al = _verdict("aws", [], alerts(instances, vols, hosts, home, account_budget(brows), twice))
    summary = "%d/%d EC2 running · %s/h%s" % (running, len(instances), core.money(burn["total_h"]), " (partial)" if partial else "")
    return _res(status, summary, d, None, al)

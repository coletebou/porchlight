"""AWS spend by day and by service, from Cost Explorer: one request a day at most.

Cost Explorer bills $0.01 a request, so this source asks AWS at most once per UTC
date and never within MIN_GAP of the last attempt: the first run after DAY_HOUR UTC
(the previous day has posted by then), or any run once MAX_GAP has passed (a machine
that slept through noon). The cap lives here, against the time of the last attempt,
so `porchlight collect --force`, `porchlight collect aws_cost` and a retry after a
failure never add a request. The attempt is noted in the state directory before the
request goes out, so a run the collector abandons at its deadline, or a crash, still
counts, and any failure waits out the gap. The CLI runs with a single attempt (no
retries) and without automatic pagination: a second page would be a second billed
request, so it is never asked for, and the result says partial. In a fleet only the
hub asks: a peer whose hub is alive never does, forced or not, and takes the source
over only after the hub's copy is three days old.

The one request: daily unblended cost by service, from the first of the month (or
two weeks back, early in a month) through today (today is partial). It carries a
second --group-by dimension at no extra charge (one request stays one request):
USAGE_TYPE by default, which breaks the opaque "EC2 - Other" and EC2 compute
buckets into EBS, snapshots, NAT gateways, public IPv4/idle EIPs and data
transfer; set [sources.aws_account] cost_detail = "region" or "linked_account"
to trade that for a top-list by region or account instead ("none" drops the
second dimension). The month's total, the daily trend and the spend by service
render in the AWS panel; the hourly run-rate there is the free aws_account
source's.

cost_forecast = true adds a second, genuinely extra request the same day:
GetCostForecast for the rest of the month, folded into the panel's forecast in
place of the cheaper per_day() estimate. That makes two $0.01 requests on a day
both run (about $0.60/month instead of $0.30); forecast_query()'s own failure
never costs the base reading, already fetched that day.

Off unless [sources.aws_account] cost_explorer = "daily".
"""
from __future__ import annotations

import calendar
import os
import re
import time

from .. import core
from ..registry import SOURCES, source
from . import saas
from .saas import ProbeError, _scrub

DAY_HOUR = 12           # UTC
MIN_GAP = 20 * 3600
MAX_GAP = 36 * 3600
FALLBACK = 3 * 86400    # a peer takes over once the hub's copy is this old
TREND_DAYS = 14
ONE_ATTEMPT = {"AWS_MAX_ATTEMPTS": "1", "AWS_RETRY_MODE": "standard"}  # a retried request is billed too
# The second --group-by dimension: free (one request either way). "none" drops it.
DETAIL_DIMS = {"usage_type": "USAGE_TYPE", "region": "REGION", "linked_account": "LINKED_ACCOUNT"}
# USAGE_TYPE strings carry a region prefix ("USE1-", "EUC1-APS1-", ...) before the part that
# names what is billed; order matters, since a snapshot's usage type also contains "EBS:".
USAGE_BUCKETS = (
    (re.compile(r"EBS:SnapshotUsage", re.I), "snapshot"),
    (re.compile(r"EBS:", re.I), "ebs"),
    (re.compile(r"NatGateway", re.I), "nat_gateway"),
    (re.compile(r"PublicIPv4|ElasticIP", re.I), "public_ipv4"),
    (re.compile(r"DataTransfer|-In-Bytes$|-Out-Bytes$|-Regional-Bytes$", re.I), "data_transfer"),
)
# The two services the USAGE_TYPE detail breaks down (the rest keep only their service total).
USAGE_SERVICES = ("EC2 - Other", "Amazon Elastic Compute Cloud - Compute")
# The buckets the aws_account run-rate models: instances (and dedicated hosts); EBS, inside
# EC2-Other, which also holds snapshots, NAT gateways and transfer; public IPv4, under VPC.
# Everything else is "other". The forecast takes per bucket the larger of the model and CE.
BUCKETS = {"Amazon Elastic Compute Cloud - Compute": "ec2", "EC2 - Other": "ec2_other",
           "Amazon Virtual Private Cloud": "vpc"}
SHORT = {"Amazon Elastic Compute Cloud - Compute": "EC2", "EC2 - Other": "EC2-Other", "Amazon Virtual Private Cloud": "VPC",
         "Amazon Simple Storage Service": "S3", "AWS Key Management Service": "KMS", "AmazonCloudWatch": "CloudWatch",
         "Amazon Bedrock": "Bedrock", "AWS Cost Explorer": "Cost Explorer", "Amazon Route 53": "Route 53",
         "AWS Secrets Manager": "Secrets", "Amazon Simple Notification Service": "SNS",
         "Amazon Simple Queue Service": "SQS", "Amazon Simple Email Service": "SES", "AWS Lambda": "Lambda",
         "Amazon Relational Database Service": "RDS", "Amazon DynamoDB": "DynamoDB", "Amazon CloudFront": "CloudFront",
         "Amazon Elastic Load Balancing": "ELB", "Amazon Elastic File System": "EFS", "Amazon ElastiCache": "ElastiCache",
         "Amazon Elastic Container Service": "ECS", "Amazon Elastic Container Service for Kubernetes": "EKS",
         "Amazon EC2 Container Registry (ECR)": "ECR", "Amazon Elastic Container Registry Public": "ECR Public",
         "AWS CloudTrail": "CloudTrail", "AWS Config": "Config", "AWS Systems Manager": "SSM", "AWS Backup": "Backup",
         "Amazon API Gateway": "API Gateway", "AWS Step Functions": "Step Functions", "Amazon Lightsail": "Lightsail",
         "Amazon OpenSearch Service": "OpenSearch", "Amazon SageMaker": "SageMaker", "AWS WAF": "WAF",
         "Amazon GuardDuty": "GuardDuty", "AWS Security Hub": "Security Hub", "Amazon Kinesis": "Kinesis",
         "AWS Glue": "Glue", "Amazon Athena": "Athena", "AWS Amplify": "Amplify", "AWS App Runner": "App Runner",
         "Amazon Registrar": "Registrar", "AWS Data Transfer": "Data Transfer"}


def mode(host) -> str:
    """"daily" or "off": [sources.aws_account] cost_explorer ("daily", or true from
    configs written before the setting had words)."""
    src = SOURCES.get("aws_account")
    v = host.cfg.source("aws_account", src.defaults if src else {}).get("cost_explorer")
    return "daily" if v is True or (isinstance(v, str) and v.strip().lower() == "daily") else "off"


def _mark_path() -> str:
    return os.path.join(core.STATE_DIR, "aws-cost-attempt.json")


def last_mark() -> float:
    d = core.read_json(_mark_path())
    v = d.get("at") if isinstance(d, dict) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def mark(now: float) -> bool:
    """Note an attempt before it is made. False when it cannot be noted: then the
    request is not made either, because nothing would stop the next run repeating it."""
    try:
        core.ensure_dirs()
        core.write_json_atomic(_mark_path(), {"at": now})
        return True
    except OSError:
        return False


def due(now: float, last: float) -> bool:
    """Whether a run at `now` may ask, the last attempt having been at `last`: never twice
    on one UTC date, never within MIN_GAP, and after DAY_HOUR unless MAX_GAP has passed."""
    if not last or last > now + 86400:  # never asked, or a mark from a clock that has since moved back
        return True
    gap = now - last
    if gap < MIN_GAP or _day(now) == _day(last):
        return False
    return time.gmtime(now).tm_hour >= DAY_HOUR or gap >= MAX_GAP


def hub_copy(host, now: float):
    """The hub's aws_cost record when this machine is a fleet peer whose hub is alive (its
    bundle is under FALLBACK old), else None. Then the request is the hub's to make."""
    if host.hub:
        return None
    b = core.read_json(core.BUNDLE_PATH)
    if not isinstance(b, dict) or not b.get("from") or b.get("from") == host.name:
        return None
    ts = b.get("ts")
    if not isinstance(ts, (int, float)) or now - ts >= FALLBACK:
        return None
    rec = (b.get("sources") or {}).get("aws_cost") if isinstance(b.get("sources"), dict) else None
    return rec if isinstance(rec, dict) else {}


def _day(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def window(now: float) -> tuple:
    """(start, end exclusive, first of the month) as YYYY-MM-DD, in UTC like the bill."""
    month = _day(now)[:8] + "01"
    return min(month, _day(now - (TREND_DAYS - 1) * 86400)), _day(now + 86400), month


def short(name: str) -> str:
    """A service's short name; an unknown one loses its Amazon/AWS prefix and keeps
    whole words up to 18 characters."""
    name = str(name or "?")
    if name in SHORT:
        return SHORT[name]
    words, out = re.sub(r"^(Amazon|AWS)\s+", "", name).split(), ""
    for w in words:
        if len(out) + len(w) + (1 if out else 0) > 18:
            break
        out = (out + " " + w) if out else w
    return out or (words[0][:18] if words else "?")


def usage_bucket(usage_type: str) -> str:
    """A USAGE_TYPE string, classified: ebs, snapshot, nat_gateway, public_ipv4, data_transfer,
    or other (instance-hours and anything porchlight does not know yet)."""
    for pat, name in USAGE_BUCKETS:
        if pat.search(usage_type or ""):
            return name
    return "other"


def month_end_str(now: float) -> str:
    """The first day of next month, UTC, as Cost Explorer wants it (End is exclusive)."""
    y, m = time.gmtime(now)[:2]
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return time.strftime("%Y-%m-%d", time.gmtime(calendar.timegm((ny, nm, 1, 0, 0, 0))))


def query(ask, now: float, detail: str = "usage_type") -> dict:
    """The day's one request. ask(args) runs `aws ce ...`. A second --group-by dimension
    rides along at no extra charge: USAGE_TYPE breaks EC2 and EC2-Other down further
    ("usage_types" in the result), "region"/"linked_account" instead give a top list
    under "group"; "none" (or anything else) drops the second dimension."""
    start, end, month = window(now)
    group_by = ["Type=DIMENSION,Key=SERVICE"]
    dim = DETAIL_DIMS.get(detail)
    if dim:
        group_by.append("Type=DIMENSION,Key=%s" % dim)
    out = ask(["ce", "get-cost-and-usage", "--time-period", "Start=%s,End=%s" % (start, end), "--granularity", "DAILY",
               "--metrics", "UnblendedCost", "--group-by"] + group_by + ["--no-paginate"])
    days, svc, by, usage_types, groups, estimated = {}, {}, {}, {}, {}, False
    results = out.get("ResultsByTime") if isinstance(out, dict) else None
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict):
            continue
        d = str((r.get("TimePeriod") or {}).get("Start") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            continue
        estimated = estimated or bool(r.get("Estimated"))
        days.setdefault(d, 0.0)
        for g in r.get("Groups") or []:
            keys = g.get("Keys") or []
            try:
                usd = float(g["Metrics"]["UnblendedCost"]["Amount"])
                name = str(keys[0] if keys else "?")
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            days[d] += usd
            if d >= month:
                svc[name] = svc.get(name, 0.0) + usd
            bucket = by.setdefault(BUCKETS.get(name, "other"), {})
            bucket[d] = bucket.get(d, 0.0) + usd
            if dim and len(keys) > 1 and d >= month:
                detail_key = str(keys[1])
                if dim == "USAGE_TYPE" and name in USAGE_SERVICES:
                    ub = usage_bucket(detail_key)
                    usage_types[ub] = usage_types.get(ub, 0.0) + usd
                elif dim != "USAGE_TYPE":
                    groups[detail_key] = groups.get(detail_key, 0.0) + usd
    if not days:
        raise ValueError("Cost Explorer returned no days")
    full = [d for d in sorted(days) if d < _day(now)][-7:]  # complete days only
    top = sorted(((short(k), round(v, 2)) for k, v in svc.items() if round(v, 2)), key=lambda kv: -kv[1])
    out_rec = {"month": month[:7], "start": start, "through": max(days), "estimated": estimated,
               "mtd": round(sum(v for d, v in days.items() if d >= month), 2),
               "days": [[d, round(days[d], 2)] for d in sorted(days)][-(TREND_DAYS + 1):],
               "services": [list(kv) for kv in top[:8]],
               "per_day": {b: round(sum(v.get(d, 0.0) for d in full) / len(full), 4) for b, v in sorted(by.items())} if full else {},
               "partial": bool(isinstance(out, dict) and out.get("NextPageToken")),
               "detail": "usage_type" if dim == "USAGE_TYPE" else ("none" if not dim else detail)}
    if dim == "USAGE_TYPE":
        out_rec["usage_types"] = {k: round(v, 2) for k, v in sorted(usage_types.items()) if round(v, 2)}
    elif dim:
        out_rec["group"] = [list(kv) for kv in sorted(((k, round(v, 2)) for k, v in groups.items() if round(v, 2)),
                                                       key=lambda kv: -kv[1])[:8]]
    return out_rec


def forecast_query(ask, now: float):
    """CE's own GetCostForecast for the rest of this month (tomorrow through the
    month's end), MONTHLY UNBLENDED_COST: one more $0.01 request, asked only when
    cost_forecast is on. None on the month's last day (nothing left to forecast)
    or when AWS returns nothing a float can be made of."""
    tomorrow, end = _day(now + 86400), month_end_str(now)
    if tomorrow >= end:
        return None
    out = ask(["ce", "get-cost-forecast", "--time-period", "Start=%s,End=%s" % (tomorrow, end),
               "--metric", "UNBLENDED_COST", "--granularity", "MONTHLY"])
    total = out.get("Total") if isinstance(out, dict) else None
    try:
        return round(float((total or {}).get("Amount")), 2)
    except (TypeError, ValueError):
        return None


@source("aws_cost", ttl=3600, timeout=40, scope="global", fallback=FALLBACK, binaries=("aws",),
        optin=lambda host: mode(host) == "daily")
def aws_cost(ctx):
    """AWS spend by day and service from Cost Explorer: one $0.01 request a day, at most."""
    from . import aws
    if mode(ctx.host) != "daily":
        return {"off": True}
    prev, now = dict(ctx.prev or {}), saas._now()
    if prev.get("off"):
        prev = {}
    hub = hub_copy(ctx.host, now)
    if hub is not None:  # a peer: the hub asks (a forced run here shows the hub's reading)
        return dict(hub.get("data") if isinstance(hub.get("data"), dict) else prev)
    tried = prev.get("tried_at")
    last = max(float(tried) if isinstance(tried, (int, float)) and not isinstance(tried, bool) else 0.0, last_mark())
    if not due(now, last):
        return prev  # nothing new: the reading (and its as_of) stays as it was
    acfg = ctx.host.cfg.source("aws_account", SOURCES["aws_account"].defaults if "aws_account" in SOURCES else {})
    prof, _region, acct, _kind, errs = aws.identity(ctx, acfg, prev.get("profile"))  # stored masked
    if not acct:  # STS is free and nothing was billed: the next run may try again
        why = ", ".join(sorted(set(errs.values()))) or "no profile"
        fix = aws.login_fix(errs)
        return {"status": "unknown", "error": "no usable AWS credentials: %s%s" % (why, " (%s)" % fix if fix else "")}
    if not mark(now):
        return {"status": "unknown", "error": "cannot note the attempt in the state directory: Cost Explorer not asked"}
    ask = lambda args: aws.cli(ctx, args, prof, "us-east-1", timeout=25, env=ONE_ATTEMPT, read_timeout=20)  # noqa: E731
    detail = str(acfg.get("cost_detail") or "usage_type").strip().lower()
    try:
        data = query(ask, now, detail=detail)
    except Exception as e:  # noqa: BLE001 — billed or not, any failure waits out the full gap
        why = str(e) if isinstance(e, ProbeError) else "%s: %s" % (type(e).__name__, e)
        prev.update(tried_at=now, ce_error=_scrub(aws.masked(why), 160))  # an access-denied quotes the caller's ARN
        return prev
    data.update(tried_at=now, as_of=now, profile=aws.masked(prof or "default"))
    if acfg.get("cost_forecast"):  # a second, genuinely extra request the same day
        try:
            fc = forecast_query(ask, now)
            if fc is not None:
                data["ce_forecast"] = round(data["mtd"] + fc, 2)
        except Exception as e:  # noqa: BLE001 — the base reading above is already fetched; never lose it here
            why = str(e) if isinstance(e, ProbeError) else "%s: %s" % (type(e).__name__, e)
            data["forecast_error"] = _scrub(aws.masked(why), 160)
    return data

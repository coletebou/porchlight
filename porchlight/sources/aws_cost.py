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
two weeks back, early in a month) through today (today is partial). The month's
total, the daily trend and the spend by service render in the AWS panel; the hourly
run-rate there is the free aws_account source's. Off unless [sources.aws_account]
cost_explorer = "daily".
"""
from __future__ import annotations

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


def query(ask, now: float) -> dict:
    """The day's one request. ask(args) runs `aws ce ...`."""
    start, end, month = window(now)
    out = ask(["ce", "get-cost-and-usage", "--time-period", "Start=%s,End=%s" % (start, end), "--granularity", "DAILY",
               "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=SERVICE", "--no-paginate"])
    days, svc, by, estimated = {}, {}, {}, False
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
            try:
                usd = float(g["Metrics"]["UnblendedCost"]["Amount"])
                name = str((g.get("Keys") or ["?"])[0])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            days[d] += usd
            if d >= month:
                svc[name] = svc.get(name, 0.0) + usd
            bucket = by.setdefault(BUCKETS.get(name, "other"), {})
            bucket[d] = bucket.get(d, 0.0) + usd
    if not days:
        raise ValueError("Cost Explorer returned no days")
    full = [d for d in sorted(days) if d < _day(now)][-7:]  # complete days only
    top = sorted(((short(k), round(v, 2)) for k, v in svc.items() if round(v, 2)), key=lambda kv: -kv[1])
    return {"month": month[:7], "start": start, "through": max(days), "estimated": estimated,
            "mtd": round(sum(v for d, v in days.items() if d >= month), 2),
            "days": [[d, round(days[d], 2)] for d in sorted(days)][-(TREND_DAYS + 1):],
            "services": [list(kv) for kv in top[:8]],
            "per_day": {b: round(sum(v.get(d, 0.0) for d in full) / len(full), 4) for b, v in sorted(by.items())} if full else {},
            "partial": bool(isinstance(out, dict) and out.get("NextPageToken"))}


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
    try:
        data = query(lambda args: aws.cli(ctx, args, prof, "us-east-1", timeout=25, env=ONE_ATTEMPT, read_timeout=20), now)
    except Exception as e:  # noqa: BLE001 — billed or not, any failure waits out the full gap
        why = str(e) if isinstance(e, ProbeError) else "%s: %s" % (type(e).__name__, e)
        prev.update(tried_at=now, ce_error=_scrub(aws.masked(why), 160))  # an access-denied quotes the caller's ARN
        return prev
    data.update(tried_at=now, as_of=now, profile=aws.masked(prof or "default"))
    return data

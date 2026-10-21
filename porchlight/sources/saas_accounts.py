"""Account sources: Stripe, Resend, Langfuse, AI API spend, the AWS account, Twilio.

Split from sources/saas.py, whose transport and result helpers (and safety
rules: read-only, credentials only in headers, no redirects with credentials,
scrubbed errors) these share.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse

from .. import core
from . import saas  # _now via the module, so a test patching saas._now reaches these too
from .saas import (  # noqa: F401 — the shared transport, parsing and result helpers
    CLI_TIMEOUT,
    ProbeError,
    _api_err,
    _clip,
    _cred,
    _d,
    _dicts,
    _f,
    _fail,
    _get,
    _int,
    _iso,
    _json,
    _missing,
    _month_start,
    _msg,
    _num,
    _pmap,
    _req,
    _res,
    _saas,
    _scrub,
    _strs,
    _ts,
    _verdict,
    _worst,
)

# ── Stripe: balance, subscriptions, an MRR estimate (GET only) ─────────────
STRIPE = "https://api.stripe.com/v1"
_MONTHLY = {"day": 30.0, "week": 52.0 / 12.0, "month": 1.0, "year": 1.0 / 12.0}


def _cents(minor, cur) -> str:
    cur = str(cur or "").lower()
    return "$%.2f" % (minor / 100.0) if cur == "usd" else "%.2f %s" % (minor / 100.0, cur.upper())


@_saas("stripe", "stripe", ttl=1800, timeout=15, needs=("STRIPE_SECRET_KEY",),
       defaults={"subscriptions": True, "max_pages": 3})
def _stripe(ctx):
    """Stripe: balance, subscriptions, an MRR estimate (read-only)"""
    key = ctx.env.get("STRIPE_SECRET_KEY")
    if not key:
        return _missing("STRIPE_SECRET_KEY")
    m = re.match(r"^(sk|rk)_(live|test)_", key)
    mode = "%s_%s" % m.groups() if m else "unknown"  # from the key's prefix; the key itself is never kept
    cfg, h = ctx.cfg, {"Authorization": "Bearer " + key}

    def subs():
        out, more, params = [], False, {"status": "all", "limit": 100}
        for _ in range(max(1, int(_num(cfg, "max_pages", 3)))):
            j = _get(ctx, STRIPE + "/subscriptions", h, params, "stripe ")
            page = _dicts(j.get("data"))
            out += page
            more = bool(j.get("has_more"))
            last = str(page[-1].get("id") or "") if page else ""
            if not more or not re.match(r"^sub_[A-Za-z0-9]+$", last):
                break
            params = dict(params, starting_after=last)
        return {"data": out, "has_more": more}
    calls = {"balance": lambda: _get(ctx, STRIPE + "/balance", h, None, "stripe ")}
    if cfg.get("subscriptions", True):
        calls["subs"] = subs
    res = _pmap(calls)
    errors = {k: e for k, (v, e) in res.items() if e}
    if len(errors) == len(res):
        return _fail("; ".join("%s: %s" % kv for kv in sorted(errors.items())), "Stripe calls failed (%s)" % mode)
    d, warn, parts = {"key_mode": mode}, [], [mode.split("_")[-1]]
    bal = res["balance"][0]
    if bal is not None:
        avail = {str(x.get("currency")): _int(x.get("amount")) for x in _dicts(bal.get("available"))}
        pend = {str(x.get("currency")): _int(x.get("amount")) for x in _dicts(bal.get("pending"))}
        d["available"] = {c: round(v / 100.0, 2) for c, v in avail.items()}
        d["pending"] = {c: round(v / 100.0, 2) for c, v in pend.items()}
        cur = "usd" if "usd" in avail else (next(iter(avail)) if avail else "usd")
        parts.append("%s avail" % _cents(avail.get(cur, 0), cur))
    sub = res.get("subs", (None, None))[0]
    if sub is not None:
        by, mrr = {}, {}
        for s in _dicts(sub.get("data")):
            st = str(s.get("status"))
            by[st] = by.get(st, 0) + 1
            if st != "active":
                continue
            for it in _dicts(_d(s.get("items")).get("data")):
                pr = _d(it.get("price"))
                rec = _d(pr.get("recurring"))
                per = _MONTHLY.get(rec.get("interval"))
                if _f(pr.get("unit_amount")) is None or per is None:
                    continue  # metered and tiered prices have no flat monthly figure
                n = max(_int(rec.get("interval_count")) or 1, 1)
                c = str(pr.get("currency"))
                mrr[c] = mrr.get(c, 0.0) + _f(pr["unit_amount"]) * (_int(it.get("quantity")) or 1) * per / n / 100.0
        bad = by.get("past_due", 0) + by.get("unpaid", 0)
        d["subscriptions"] = {"by_status": by, "list_truncated": bool(sub.get("has_more")),
                              "mrr_est": {c: round(v, 2) for c, v in mrr.items()}}
        parts.append("%d active%s" % (by.get("active", 0), "+" if sub.get("has_more") else ""))
        parts.append("%d past due" % bad)
        if bad:
            warn.append("%d past-due/unpaid subscription(s)" % bad)
    if errors:
        d["partial_errors"] = errors
        warn.append("partial data (%s)" % ", ".join(sorted(errors)))
    status, alerts = _verdict("stripe", [], warn)
    return _res(status, ", ".join(parts), d, None, alerts)


# ── Resend: domains and recent sends; a send-only key is detected, not guessed ──
RESEND = "https://api.resend.com"


@_saas("resend", "resend", ttl=1800, timeout=15, needs=("RESEND_API_KEY",),
       defaults={"bounce_crit_pct": 5, "bounce_min_emails": 20})
def _resend(ctx):
    """Resend: domain verification and the last 24 hours of sends"""
    key = ctx.env.get("RESEND_API_KEY")
    if not key:
        return _missing("RESEND_API_KEY")
    cfg, h = ctx.cfg, {"Authorization": "Bearer " + key}
    try:
        r = _req(ctx, "GET", RESEND + "/domains", h)
    except ProbeError as e:
        return _fail(str(e))
    j = _d(_json(r))
    if r.status in (401, 403) and j.get("name") == "restricted_api_key":
        # Valid (an invalid key answers 400 validation_error) but sending-only: no read endpoint is open to it.
        return _res("unknown", "send-only key: cannot read domains or emails",
                    {"key_valid": True, "can_read": False,
                     "fix": "a full-access key is needed to read status (Resend has no read-only scope)"},
                    "restricted_api_key")
    if not r.ok:
        err = _api_err(r, "resend ")
        rejected = r.status in (400, 401, 403)
        return _res("crit" if rejected else "unknown", "Resend key rejected" if rejected else err, {}, err)
    domains = [{"name": x.get("name"), "status": x.get("status")} for x in _dicts(j.get("data"))]
    d = {"key_valid": True, "can_read": True, "domains": domains[:20]}
    crit, warn = [], []
    for dom in domains:
        if dom["status"] == "failed":
            crit.append("domain %s failed" % dom["name"])
        elif dom["status"] != "verified":
            warn.append("domain %s %s" % (dom["name"], dom["status"]))
    n24 = 0
    try:
        em = _get(ctx, RESEND + "/emails", h, {"limit": 100}, "resend ")
        cutoff = saas._now() - 86400
        events = {}
        for m in _dicts(em.get("data")):  # only the event and the time are read: never a recipient or a subject
            t = _ts(m.get("created_at"))
            if t is None or t < cutoff:
                continue
            n24 += 1
            ev = str(m.get("last_event"))
            events[ev] = events.get(ev, 0) + 1
        bad = sum(events.get(k, 0) for k in ("bounced", "complained", "failed"))
        d["emails_24h"] = {"count": n24, "truncated": bool(em.get("has_more")), "by_last_event": events}
        if n24 >= _num(cfg, "bounce_min_emails", 20) and 100.0 * bad / n24 >= _num(cfg, "bounce_crit_pct", 5):
            crit.append("%d/%d emails bounced or failed" % (bad, n24))
        elif bad:
            warn.append("%d bounced or failed in 24h" % bad)
    except ProbeError as e:
        d["emails_error"] = str(e)
    status, alerts = _verdict("resend", crit, warn)
    ok_domains = sum(1 for x in domains if x["status"] == "verified")
    summary = _msg((crit + warn)[0]) if crit or warn else "%d/%d domains verified, %d emails/24h" % (ok_domains, len(domains), n24)
    return _res(status, summary, d, None, alerts)


# ── Langfuse: health, plus 24h traces and cost per project (one key pair each) ──
def _lf_urls(ctx) -> list:
    v = ctx.cfg.get("host")
    urls = _strs(v) if v else [u for u in (ctx.env.get("LANGFUSE_HOST"), ctx.env.get("LANGFUSE_BASE_URL")) if u]
    return [u.rstrip("/") for u in urls if urllib.parse.urlsplit(u).scheme in ("http", "https")]


def _lf_pairs(ctx) -> list:
    """[(label, public key, secret key)] for every configured pair whose variables are set."""
    out = []
    for k in ctx.cfg.get("keys") or []:
        if not isinstance(k, dict):
            continue
        pk, sk = ctx.env.get(str(k.get("public_env") or "")), ctx.env.get(str(k.get("secret_env") or ""))
        if pk and sk:
            out.append((str(k.get("label") or ""), _cred(ctx, pk), _cred(ctx, sk)))
    return out


def _lf_project(ctx, base, pk, sk, since, until) -> dict:
    auth = (pk, sk)
    query = json.dumps({"view": "traces", "dimensions": [], "filters": [], "fromTimestamp": since, "toTimestamp": until,
                        "metrics": [{"measure": "count", "aggregation": "count"}, {"measure": "totalCost", "aggregation": "sum"}]})
    res = _pmap({"project": lambda: _get(ctx, base + "/api/public/projects", None, None, "langfuse ", basic=auth),
                 "metrics": lambda: _get(ctx, base + "/api/public/metrics", None, {"query": query}, "langfuse ", basic=auth)})
    proj, perr = res["project"]
    mx, _merr = res["metrics"]
    out = {"project": (_dicts(_d(proj).get("data")) or [{}])[0].get("name")}
    if mx is not None:
        row = (_dicts(mx.get("data")) or [{}])[0]
        out["traces_24h"] = _int(row.get("count_count"))
        out["cost_24h_usd"] = round(_f(row.get("sum_totalCost")) or 0.0, 4)
    else:  # servers older than the metrics API: fall back to the traces list's total
        try:
            t = _get(ctx, base + "/api/public/traces", None, {"limit": 1, "fromTimestamp": since}, "langfuse ", basic=auth)
            out["traces_24h"] = _int(_d(t.get("meta")).get("totalItems"))
        except ProbeError as e:
            out["error"] = str(e)
    if perr and "error" not in out:
        out["error"] = perr
    return out


@_saas("langfuse", "langfuse", ttl=900, timeout=20, defaults={
    "host": "", "keys": [{"public_env": "LANGFUSE_PUBLIC_KEY", "secret_env": "LANGFUSE_SECRET_KEY"}]})
def _langfuse(ctx):
    """Langfuse: server health, 24h traces and cost per project"""
    urls = _lf_urls(ctx)
    if not urls:
        return _missing("LANGFUSE_HOST")
    health = _pmap({str(i): (lambda u=u: _req(ctx, "GET", u + "/api/public/health", timeout=4.0)) for i, u in enumerate(urls)})
    base, version, errs = None, None, []
    for i, u in enumerate(urls):  # the first healthy URL, in the order given
        r, e = health[str(i)]
        body = _d(_json(r)) if r is not None else {}
        if r is not None and r.ok and body.get("status") == "OK":
            base, version = u, body.get("version")
            break
        errs.append("url%d: %s" % (i + 1, e or "HTTP %d" % r.status))
    if base is None:
        return _res("crit", "Langfuse unreachable", {"health_errors": errs}, "; ".join(errs),
                    [{"sev": "crit", "msg": "langfuse: unreachable (%s)" % _clip("; ".join(errs), 80)}])
    now = saas._now()
    since, until = _iso(now - 86400), _iso(now)
    pairs = _lf_pairs(ctx)
    res = _pmap({str(i): (lambda p=p: _lf_project(ctx, base, p[1], p[2], since, until)) for i, p in enumerate(pairs)})
    projects, warn = [], []
    for i, (label, _pk, _sk) in enumerate(pairs):
        v, e = res[str(i)]
        v = v if v is not None else {"project": None, "error": e}
        v["project"] = v.get("project") or label or "pair %d" % (i + 1)
        projects.append(v)
        if v.get("error"):
            warn.append("%s: %s" % (v["project"], v["error"]))
    if not pairs:
        warn.append("no key pair set (see [sources.langfuse] keys)")
    # The ai panel reads details.version and details.projects[{project, traces_24h, cost_24h_usd}]: keep that shape.
    d = {"version": version, "used_fallback_url": base != urls[0], "projects": projects}
    status, alerts = _verdict("langfuse", [], warn)
    parts = ["v%s ok" % version]
    for v in projects:
        if "traces_24h" in v:
            parts.append("%s %dtr/$%.2f" % (v["project"], v["traces_24h"], v.get("cost_24h_usd") or 0.0))
    return _res(status, _msg(warn[0]) if warn else "; ".join(parts), d, None, alerts)


# ── AI API spend: OpenRouter credits, Anthropic + OpenAI admin cost reports, xAI ──
def _openrouter(ctx):
    key = ctx.env.get("OPENROUTER_MGMT_KEY") or ctx.env.get("OPENROUTER_API_KEY")
    h = {"Authorization": "Bearer " + key}
    res = _pmap({"credits": lambda: _get(ctx, "https://openrouter.ai/api/v1/credits", h, None, "openrouter "),
                 "key": lambda: _get(ctx, "https://openrouter.ai/api/v1/key", h, None, "openrouter ")})
    credits, err = res["credits"]
    if credits is None:
        return _fail(err or "no credits data")
    data = _d(credits.get("data"))
    total, used = _f(data.get("total_credits")), _f(data.get("total_usage"))
    if total is None or used is None:
        return _fail("unexpected OpenRouter credits payload")
    left = total - used
    k = _d(_d(res["key"][0]).get("data"))  # k["label"] can embed a masked key, so it is never read
    d = {"credits_total_usd": round(total, 2), "credits_used_usd": round(used, 2), "remaining_usd": round(left, 2),
         "remaining_pct": round(100.0 * left / total, 1) if total else None, "key_limit_usd": _f(k.get("limit")),
         "key_limit_remaining_usd": _f(k.get("limit_remaining")), "key_usage_monthly_usd": _f(k.get("usage_monthly")),
         "mgmt_key": bool(k.get("is_management_key"))}
    warn_at, crit_at = _num(ctx.cfg, "openrouter_warn_usd", 10), _num(ctx.cfg, "openrouter_crit_usd", 2)
    status = "crit" if left < crit_at else "warn" if left < warn_at else "ok"
    return _res(status, "OpenRouter $%.2f left of $%.0f" % (left, total), d)


def _anthropic(ctx):
    h = {"x-api-key": ctx.env["ANTHROPIC_ADMIN_KEY"], "anthropic-version": "2023-06-01"}
    params = {"starting_at": _iso(_month_start(saas._now())), "bucket_width": "1d", "limit": 31}
    cents, last_day = 0.0, None
    try:
        for _ in range(5):
            j = _get(ctx, "https://api.anthropic.com/v1/organizations/cost_report", h, params, "anthropic ")
            for b in _dicts(j.get("data")):
                last_day = str(b.get("starting_at") or "")[:10] or last_day
                for r in _dicts(b.get("results")):
                    if str(r.get("currency", "USD")).upper() == "USD":
                        cents += _f(r.get("amount")) or 0.0
            if not (j.get("has_more") and j.get("next_page")):
                break
            params["page"] = j["next_page"]
    except ProbeError as e:
        return _fail(str(e))
    usd = cents / 100.0  # amounts are USD cents as decimal strings ("20.8015" = $0.208)
    return _res("ok", "Anthropic MTD $%.2f (through %s)" % (usd, last_day or "n/a"), {"mtd_usd": round(usd, 2), "last_bucket": last_day})


def _openai(ctx):
    h = {"Authorization": "Bearer " + ctx.env["OPENAI_ADMIN_KEY"]}
    params = {"start_time": _month_start(saas._now()), "bucket_width": "1d", "limit": 31}
    total, last_day = 0.0, None
    try:
        for _ in range(5):
            j = _get(ctx, "https://api.openai.com/v1/organization/costs", h, params, "openai ")
            for b in _dicts(j.get("data")):
                if isinstance(b.get("start_time"), (int, float)):
                    last_day = time.strftime("%Y-%m-%d", time.gmtime(b["start_time"]))
                for r in _dicts(b.get("results")):
                    amt = _d(r.get("amount"))
                    if str(amt.get("currency", "usd")).lower() == "usd":
                        total += _f(amt.get("value")) or 0.0
            if not (j.get("has_more") and j.get("next_page")):
                break
            params["page"] = j["next_page"]
    except ProbeError as e:
        return _fail(str(e))
    return _res("ok", "OpenAI MTD $%.2f (through %s)" % (total, last_day or "n/a"), {"mtd_usd": round(total, 2), "last_bucket": last_day})


def _xai(ctx):
    base, h = "https://management-api.x.ai", {"Authorization": "Bearer " + ctx.env["XAI_MGMT_KEY"]}
    try:
        v = _get(ctx, base + "/auth/management-keys/validation", h, None, "xai ")
    except ProbeError as e:
        return _fail(str(e))
    team = str(v.get("teamId") or "")
    if not re.match(r"^[0-9a-fA-F\-]{36}$", team):  # it goes into a URL path
        return _res("unknown", "xAI key valid but no team id", {"key_valid": True}, "no teamId")
    try:
        r = _req(ctx, "GET", "%s/v1/billing/teams/%s/prepaid/balance" % (base, team), h)
    except ProbeError as e:
        return _fail(str(e))
    if r.status == 403:  # the key validates but carries no BillingRead ACL
        return _res("unknown", "xAI mgmt key lacks BillingRead permission",
                    {"key_valid": True, "billing_read": False, "fix": "grant BillingRead to the management key at console.x.ai"},
                    "HTTP 403 BillingRead")
    if not r.ok:
        return _fail(_api_err(r, "xai "))
    total = _d(_d(_json(r)).get("total")).get("val")  # the API's own units; its schema is not documented
    return _res("ok", "xAI prepaid total (raw units): %s" % _clip(total, 20), {"billing_read": True, "prepaid_total_raw": total})


_AI = (("openrouter", ("OPENROUTER_MGMT_KEY", "OPENROUTER_API_KEY"), _openrouter),
       ("anthropic", ("ANTHROPIC_ADMIN_KEY",), _anthropic),
       ("openai", ("OPENAI_ADMIN_KEY",), _openai),
       ("xai", ("XAI_MGMT_KEY",), _xai))


@_saas("ai_spend", "ai spend", ttl=3600, timeout=25, defaults={"openrouter_warn_usd": 10, "openrouter_crit_usd": 2})
def _ai_spend(ctx):
    """AI API spend: OpenRouter credits, Anthropic, OpenAI and xAI"""
    have = [(n, fn) for n, names, fn in _AI if any(ctx.env.get(v) for v in names)]
    if not have:
        return _missing("OPENROUTER_API_KEY, ANTHROPIC_ADMIN_KEY, OPENAI_ADMIN_KEY or XAI_MGMT_KEY")
    res = _pmap({n: (lambda fn=fn: fn(ctx)) for n, fn in have}, timeout=20)
    parts = {n: (res[n][0] if isinstance(res[n][0], dict) else _res("unknown", "internal error", {}, res[n][1])) for n, _fn in have}
    d = {n: {"status": p["status"], "summary": p["summary"], "error": p["error"], "details": p["details"]} for n, p in parts.items()}
    mtd = [p["details"]["mtd_usd"] for n, p in parts.items() if n in ("anthropic", "openai") and p["details"].get("mtd_usd") is not None]
    if mtd:
        d["mtd_usd"] = round(sum(mtd), 2)
    alerts = [{"sev": p["status"], "msg": "ai spend: " + p["summary"]} for p in parts.values() if p["status"] in ("warn", "crit")]
    bits = []
    for label, n in (("A", "anthropic"), ("O", "openai")):
        if n in parts:
            v = parts[n]["details"].get("mtd_usd")
            bits.append("%s$%.2f" % (label, v) if v is not None else "%s n/a" % label)
    summary = ["MTD " + " ".join(bits)] if bits else []
    if "openrouter" in parts:  # no invented "$0 left" when the reading is missing
        left = parts["openrouter"]["details"].get("remaining_usd")
        summary.append("OR $%.0f left" % left if left is not None else "OR n/a")
    if "xai" in parts:
        summary.append("xAI %s" % ("ok" if parts["xai"]["status"] == "ok" else "n/a"))
    known = any(p["status"] != "unknown" for p in parts.values())
    error = None if known else "; ".join("%s: %s" % (n, p["error"]) for n, p in parts.items() if p["error"])
    return _res(_worst([p["status"] for p in parts.values()]), " | ".join(summary), d, error, alerts)


# ── AWS: STS identity per profile, running EC2, Cost Explorer (opt-in) ─────
# The AWS CLI rather than an SDK: no dependency, and its credential chain (SSO,
# profiles, env) is the user's own. Cost Explorer is opt-in: AWS bills $0.01 a call.
_EC2_QUERY = "Reservations[].Instances[].{n:Tags[?Key=='Name']|[0].Value,t:InstanceType}"


def _aws_bin(ctx):
    return core.which("aws", tuple(ctx.cfg.get("paths") or ()))


def _aws_profiles(ctx) -> list:
    """[(profile, region)]: [sources.aws_account] profiles, else AWS_PROFILE and then every
    profile in the AWS config file ([sso-session] blocks are not profiles)."""
    regions, cur = {}, None
    for line in core.read_text(core.expand(ctx.env.get("AWS_CONFIG_FILE") or "~/.aws/config")).splitlines():
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
    first = [ctx.env["AWS_PROFILE"]] if ctx.env.get("AWS_PROFILE") else []
    names = _strs(ctx.cfg.get("profiles")) or first + [n for n in regions if n not in first]
    return [(n, regions.get(n, "")) for n in names[:8]]


def _aws_err(stderr) -> str:
    lines = (stderr or "").strip().splitlines()
    low = (lines[-1] if lines else "").lower()
    if "token has expired" in low or ("sso" in low and "expired" in low):
        return "sso-expired"
    if "unable to locate credentials" in low:
        return "no-credentials"
    if "expiredtoken" in low or ("security token" in low and "expired" in low):
        return "credentials-expired"
    if "accessdenied" in low or "unauthorizedoperation" in low:
        return "access-denied"
    return _scrub(lines[-1] if lines else "aws failed", 80)


def _aws(ctx, args, profile, region=None):
    b = _aws_bin(ctx)
    if not b:
        raise ProbeError("aws CLI not found")
    argv = [b] + args + ["--output", "json", "--cli-connect-timeout", "4", "--cli-read-timeout", "5"]
    if profile:
        argv += ["--profile", profile]
    if region:
        argv += ["--region", region]
    env = dict(ctx.env, AWS_EC2_METADATA_DISABLED="true", AWS_PAGER="")  # no 2 s IMDS probe when credentials are absent
    rc, out, err = ctx.run(argv, timeout=CLI_TIMEOUT, env=env)
    if rc in (124, 126, 127):
        raise ProbeError({124: "timeout", 126: "cannot run aws", 127: "aws CLI not found"}[rc])
    if rc != 0:
        raise ProbeError(_aws_err(err))
    try:
        return json.loads(out or "null")
    except ValueError:
        raise ProbeError("aws: bad JSON") from None


@_saas("aws_account", "aws", ttl=3600, timeout=30, binaries=("aws",),
       defaults={"profiles": [], "regions": [], "cost_explorer": False})
def _aws_account(ctx):
    """AWS: identity per CLI profile, running EC2, Cost Explorer (opt-in)"""
    if not _aws_bin(ctx):
        return _fail("aws CLI not found", "aws CLI not installed")
    cfg = ctx.cfg
    profs = _aws_profiles(ctx) or [(None, "")]  # no config file: the CLI's own chain (environment credentials)
    sts = _pmap({str(i): (lambda p=p: _aws(ctx, ["sts", "get-caller-identity"], p)) for i, (p, _r) in enumerate(profs)},
                timeout=CLI_TIMEOUT + 2)
    errs, accounts, seen = {}, [], set()
    for i, (p, reg) in enumerate(profs):
        v, e = sts[str(i)]
        label = p or "default"
        acct = str(_d(v).get("Account") or "")
        if e or not acct:
            errs[label] = e or "no account in reply"
            continue
        _cred(ctx, acct)  # only its last four digits are ever kept
        if acct in seen:
            continue  # two profiles into one account: count its instances once
        seen.add(acct)
        arn = str(_d(v).get("Arn") or "")
        want = [r for r in _strs(cfg.get("regions")) if re.match(r"^[a-z0-9\-]{4,30}$", r)]
        accounts.append({"profile": label, "_p": p, "account_last4": acct[-4:],
                         "principal_type": arn.split(":")[5].split("/")[0] if arn.count(":") >= 5 else "?",
                         "_regions": want or [reg or ctx.env.get("AWS_REGION") or ctx.env.get("AWS_DEFAULT_REGION") or "us-east-1"]})
    if not accounts:
        expired = [p for p, e in errs.items() if e == "sso-expired"]
        if expired:
            fix = "aws sso login --profile %s" % expired[0]
            return _res("warn", "SSO token expired: " + fix, {"profile_errors": errs}, "sso-expired",
                        [{"sev": "warn", "msg": "aws: SSO token expired (%s)" % expired[0], "fix": fix}])
        return _res("unknown", "no usable AWS credentials", {"profile_errors": errs}, "; ".join(sorted(set(errs.values()))))
    now = saas._now()
    span = "Start=%s,End=%s" % (time.strftime("%Y-%m-%d", time.gmtime(_month_start(now))),
                                time.strftime("%Y-%m-%d", time.gmtime(now + 86400)))
    calls = {}
    for i, a in enumerate(accounts):
        for r in a["_regions"][:6]:
            calls["ec2:%d:%s" % (i, r)] = (lambda a=a, r=r: _aws(ctx, ["ec2", "describe-instances", "--filters",
                                                                     "Name=instance-state-name,Values=running",
                                                                     "--query", _EC2_QUERY], a["_p"], r))
        if cfg.get("cost_explorer"):
            calls["ce:%d" % i] = (lambda a=a: _aws(ctx, ["ce", "get-cost-and-usage", "--time-period", span, "--granularity",
                                                         "MONTHLY", "--metrics", "UnblendedCost"], a["_p"], "us-east-1"))
    res = _pmap(calls, timeout=CLI_TIMEOUT + 2)
    partial, total = False, 0
    for i, a in enumerate(accounts):
        ec2 = {}
        for r in a.pop("_regions")[:6]:
            v, e = res["ec2:%d:%s" % (i, r)]
            ec2[r] = {"error": e} if e else {"count": len(_dicts(v)), "names": [str(x["n"]) for x in _dicts(v) if x.get("n")][:8]}
            partial = partial or bool(e)
            total += ec2[r].get("count", 0)
        a["ec2_running"] = ec2
        if "ce:%d" % i in res:
            v, e = res["ce:%d" % i]
            try:
                a["mtd_unblended_usd"] = round(float(v["ResultsByTime"][0]["Total"]["UnblendedCost"]["Amount"]), 2)
            except (KeyError, IndexError, ValueError, TypeError):
                a["ce_error"] = e or "unexpected Cost Explorer reply"
        a.pop("_p")
    d = {"accounts": accounts, "profiles": len(profs), "profile_errors": errs}
    status, alerts = _verdict("aws", [], ["EC2 query failed (partial data)"] if partial else [])
    head = ("acct ...%s" % accounts[0]["account_last4"]) if len(accounts) == 1 else "%d accounts" % len(accounts)
    return _res(status, "%s, %d EC2 running%s" % (head, total, " (partial)" if partial else ""), d, None, alerts)


# ── Twilio: balance and today's SMS ─────────────────────────────────────────
# Where the credentials live on one box only: `scope = "local"` and `share = true`
# under [sources.twilio] (honoured by the registry for every source) make that
# box collect it, and the hub read its record through the fleet snapshot.
@_saas("twilio", "twilio", ttl=1800, timeout=20,
       defaults={"env_file": "", "balance_warn": 20, "balance_crit": 5},
       alert_unknown=lambda ctx: bool(ctx.cfg.get("env_file")))  # a named env_file without credentials is news
def _twilio(ctx):
    """Twilio: account balance and today's SMS count"""
    cfg = ctx.cfg
    # Credentials can live in a service's own dotenv file on the one box that has them: read in memory, never copied.
    filed = core.dotenv(cfg["env_file"]) if cfg.get("env_file") else {}
    sid = filed.get("TWILIO_ACCOUNT_SID") or ctx.env.get("TWILIO_ACCOUNT_SID")
    tok = filed.get("TWILIO_AUTH_TOKEN") or ctx.env.get("TWILIO_AUTH_TOKEN")
    if not sid or not tok:
        return _missing("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN" + (" (env_file has neither)" if cfg.get("env_file") else ""))
    _cred(ctx, sid)
    _cred(ctx, tok)
    if not re.match(r"^AC[0-9a-fA-F]{32}$", sid):  # it goes into the URL path
        return _fail("TWILIO_ACCOUNT_SID is not an AC... account SID", "bad TWILIO_ACCOUNT_SID")
    # The account SID is an identifier Twilio's URL scheme requires; the secret travels in the Basic header only.
    base, auth = "https://api.twilio.com/2010-04-01/Accounts/" + sid, (sid, tok)
    res = _pmap({"balance": lambda: _get(ctx, base + "/Balance.json", None, None, "twilio ", basic=auth),
                 "today": lambda: _get(ctx, base + "/Usage/Records/Today.json", None, {"Category": "sms"}, "twilio ", basic=auth)})
    errors = {k: e for k, (v, e) in res.items() if e}
    if len(errors) == len(res):
        return _fail("; ".join("%s: %s" % kv for kv in sorted(errors.items())), "Twilio calls failed")
    d, crit, warn = {}, [], []
    left = _f(_d(res["balance"][0]).get("balance"))
    if left is not None:
        bw, bc = _num(cfg, "balance_warn", 20), _num(cfg, "balance_crit", 5)
        lv = "crit" if left < bc else "warn" if left < bw else "ok"
        cur = str(_d(res["balance"][0]).get("currency") or "USD")
        d["balance"] = {"amount": round(left, 2), "currency": cur, "level": lv}
        if lv != "ok":
            (crit if lv == "crit" else warn).append("balance %.2f %s" % (left, cur))
    today = res["today"][0]
    if today is not None:
        d["sms_today"] = sum(_int(r.get("count")) for r in _dicts(today.get("usage_records")))
    if errors:
        d["partial_errors"] = errors
        warn.append("partial data (%s)" % ", ".join(sorted(errors)))
    status, alerts = _verdict("twilio", crit, warn)
    bits = ["$%.2f balance" % left] if left is not None else []
    if "sms_today" in d:
        bits.append("%d SMS today" % d["sms_today"])
    return _res(status, ", ".join(bits) or "no data", d, None, alerts)

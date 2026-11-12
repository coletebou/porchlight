"""GitHub Actions: month-to-date spend, from the enhanced billing usage API.

One source, `gh_actions_cost`, for the configured user (default: the token's own
login) and any orgs listed. For each target it reads the enhanced usage report
(`GET /users/{u}/settings/billing/usage` or `/organizations/{org}/...`, scoped to
this month) and keeps only the Actions-related rows (product "Actions", or a SKU
that names Actions: a bare "Shared Storage" row, which Packages can also bill, is
left out rather than guessed at). From those rows it keeps gross vs net spend,
minutes by SKU, non-minute usage (storage) by SKU, and the top repositories by
net cost.

A GitHub Enterprise Server or an older personal account has no enhanced billing
platform: that call answers 404, and the target falls back to the legacy
`GET /users/{u}/settings/billing/actions` (`/orgs/{org}/...`), which has no cost
or per-repo figures, only total minutes used against the plan's included minutes.

A token that can reach GitHub but lacks billing read access (the classic `user`
or `admin:org` scope, or a fine-grained token's "Plan: read" permission) gets a
403 on both calls, or, for a classic token, a 404 whose X-OAuth-Scopes header lacks
the scope (GitHub hides these endpoints that way): that target is reported as
needing that scope, a state, never an error alert. Every call is a GET; the token
never appears outside a header (saas._gh_token, shared with the github CI source).

Alerts: only a configured monthly budget exceeded (crit), or any net spend at
all (warn) — a state like "needs scope" or a transient fetch failure never
alerts on its own; see docs/reference/saas.md.
"""
from __future__ import annotations

import time

from . import saas  # _now via the module, so a test pinning saas._now reaches this file too
from .saas import GH, ProbeError, _LOGIN, _api_err, _d, _dicts, _f, _fail, _gh_headers, _gh_token, _int, \
    _json, _msg, _pmap, _req, _res, _saas, _scrub, _verdict

DEFAULT_TOP_REPOS = 8


def _enhanced_path(kind: str, name: str) -> str:
    seg = "users" if kind == "user" else "organizations"
    return "%s/%s/%s/settings/billing/usage" % (GH, seg, name)


def _legacy_path(kind: str, name: str) -> str:
    seg = "users" if kind == "user" else "orgs"
    return "%s/%s/%s/settings/billing/actions" % (GH, seg, name)


# Classic-token scopes that GitHub documents as granting the legacy billing reads.
_SCOPES_FOR = {"user": ("user",), "org": ("admin:org", "repo")}


def _lacks_scope(r, kind: str) -> bool:
    """Whether a 404 is GitHub hiding a billing endpoint from a classic token that lacks the
    scope, rather than the endpoint not existing: GitHub answers 404 (not 403) there, and a
    classic token's reply lists its granted scopes in X-OAuth-Scopes. A fine-grained token
    sends no such header, so its 404 stays a plain failure instead of a guessed scope gap."""
    raw = next((v for k, v in (r.headers or {}).items() if str(k).lower() == "x-oauth-scopes"), None)
    if raw is None:
        return False
    granted = {s.strip() for s in str(raw).split(",") if s.strip()}
    return not granted.intersection(_SCOPES_FOR.get(kind, ()))


def usage(ctx, h, kind: str, name: str, now: float) -> dict:
    """This target's Actions usage this month: the enhanced usage report
    ({"mode": "enhanced", "items": [...]}), or the legacy minutes-only reply
    when enhanced billing is not on for this account ({"mode": "legacy", ...}).
    Raises ProbeError("needs-scope") on a 403 from either call."""
    g = time.gmtime(now)
    r = _req(ctx, "GET", _enhanced_path(kind, name), h, {"year": g.tm_year, "month": g.tm_mon})
    if r.status == 403:
        raise ProbeError("needs-scope")
    if r.status == 404:
        r = _req(ctx, "GET", _legacy_path(kind, name), h)
        if r.status == 403 or (r.status == 404 and _lacks_scope(r, kind)):
            raise ProbeError("needs-scope")
        if not r.ok:
            raise ProbeError(_api_err(r, "github "))
        j = _d(_json(r))
        total = j.get("total_minutes_used")
        if not isinstance(total, (int, float)) or isinstance(total, bool):
            raise ProbeError("github: unexpected legacy billing response")
        breakdown = {str(k): _int(v) for k, v in _d(j.get("minutes_used_breakdown")).items() if _int(v)}
        return {"mode": "legacy", "total_minutes_used": _int(total),
                "included_minutes": _int(j.get("included_minutes")), "breakdown": breakdown}
    if not r.ok:
        raise ProbeError(_api_err(r, "github "))
    j = _json(r)
    if not isinstance(j, dict) or not isinstance(j.get("usageItems"), list):
        raise ProbeError("github: unexpected usage response")
    return {"mode": "enhanced", "items": _dicts(j.get("usageItems"))}


def is_actions(item: dict) -> bool:
    """Whether a usage line item is Actions spend: its product is "Actions", or its
    SKU names Actions. A bare "Shared Storage" row (Packages can bill that SKU too)
    is left out rather than attributed to Actions by a guess."""
    if str(item.get("product") or "") == "Actions":
        return True
    return "actions" in str(item.get("sku") or "").lower()


def _is_minutes(item: dict) -> bool:
    return "minute" in str(item.get("unitType") or "").lower()


def aggregate(items, top_repos: int = DEFAULT_TOP_REPOS) -> dict:
    """Actions-only usage items -> gross/net/discount totals, minutes by SKU,
    non-minute usage (storage) by SKU, and the top repos by net cost."""
    act = [it for it in items if isinstance(it, dict) and is_actions(it)]
    gross = round(sum(_f(it.get("grossAmount")) or 0.0 for it in act), 2)
    discount = round(sum(_f(it.get("discountAmount")) or 0.0 for it in act), 2)
    net = round(sum(_f(it.get("netAmount")) or 0.0 for it in act), 2)
    minutes, storage, repos = {}, {}, {}
    for it in act:
        sku = str(it.get("sku") or "?")
        bucket = minutes if _is_minutes(it) else storage
        row = bucket.setdefault(sku, {"sku": sku, "quantity": 0, "unit": str(it.get("unitType") or ""),
                                     "gross": 0.0, "net": 0.0})
        row["quantity"] += _int(it.get("quantity"))
        row["gross"] += _f(it.get("grossAmount")) or 0.0
        row["net"] += _f(it.get("netAmount")) or 0.0
        repo = str(it.get("repositoryName") or "")
        if repo:
            r = repos.setdefault(repo, {"repo": repo, "gross": 0.0, "net": 0.0})
            r["gross"] += _f(it.get("grossAmount")) or 0.0
            r["net"] += _f(it.get("netAmount")) or 0.0
    for bucket in (minutes, storage, repos):
        for row in bucket.values():
            row["gross"], row["net"] = round(row["gross"], 2), round(row["net"], 2)
    top = sorted(repos.values(), key=lambda r: -r["net"])[:max(0, int(top_repos))]
    return {"gross_usd": gross, "net_usd": net, "discount_usd": discount,
            "discount_pct": round(100.0 * discount / gross, 1) if gross else None,
            "by_sku": sorted(minutes.values(), key=lambda r: -r["gross"]),
            "storage": sorted(storage.values(), key=lambda r: -r["gross"]),
            "top_repos": top}


@_saas("gh_actions_cost", "gh-actions", ttl=3600, timeout=30, defaults={
    "user": "", "orgs": [], "budget_usd": 0, "top_repos": DEFAULT_TOP_REPOS})
def _gh_actions_cost(ctx):
    """GitHub Actions: month-to-date spend (gross vs net), minutes by SKU, storage, top repos by cost"""
    cfg = ctx.cfg
    try:
        tok, _src, login = _gh_token(ctx, want_user=not cfg.get("user"))
    except ProbeError as e:
        return _fail(str(e))
    if not tok:
        return _fail("no valid GitHub token (GH_TOKEN / GITHUB_TOKEN rejected or unset, gh not logged in)",
                     "no valid GitHub token")
    user = str(cfg.get("user") or login or "")
    orgs = [o for o in (str(x).strip() for x in cfg.get("orgs") or []) if o and _LOGIN.match(o)]
    if not user and not orgs:
        return _fail("no GitHub user or orgs to bill: set [sources.gh_actions_cost] user or orgs")
    h, now = _gh_headers(tok), saas._now()  # _gh_token already registered tok for scrubbing
    targets = ([("user", user)] if user else []) + [("org", o) for o in orgs]
    res = _pmap({name: (lambda kind=kind, name=name: usage(ctx, h, kind, name, now)) for kind, name in targets},
               timeout=25, workers=4)
    errors, modes, enhanced_items, legacy = {}, {}, [], []
    ok = 0
    for name, (v, e) in res.items():
        if e == "needs-scope":
            errors[name] = "needs billing scope (classic user/admin:org token, or fine-grained \"Plan: read\")"
            continue
        if e:
            errors[name] = _scrub(e)
            continue
        ok += 1
        modes[name] = v["mode"]
        if v["mode"] == "enhanced":
            enhanced_items += [dict(it, _scope=name) for it in v["items"]]
        else:
            legacy.append(dict(v, _scope=name))
    if not ok:
        if errors and all(e.startswith("needs billing scope") for e in errors.values()):
            return _res("unknown", "needs GitHub billing scope", {"errors": errors}, "needs billing scope on every target")
        return _fail("; ".join("%s: %s" % kv for kv in sorted(errors.items())) or "no usable GitHub token", "all GitHub billing calls failed")
    agg = aggregate(enhanced_items, cfg.get("top_repos", DEFAULT_TOP_REPOS))
    d = {"month": time.strftime("%Y-%m", time.gmtime(now)), "targets": sorted(modes), "modes": modes}
    d.update(agg)
    if legacy:
        lm = sum(t.get("total_minutes_used") or 0 for t in legacy)
        li = sum(t.get("included_minutes") or 0 for t in legacy)
        d["legacy_minutes_used"], d["legacy_included_minutes"] = lm, li or None
        if li:
            d["legacy_included_pct"] = round(100.0 * lm / li, 1)
    if errors:
        d["errors"] = errors
    budget = _f(cfg.get("budget_usd")) or 0.0
    crit, warn = [], []
    if budget and d["net_usd"] > budget:
        crit.append("$%.2f MTD net exceeds the $%.2f budget" % (d["net_usd"], budget))
    elif d["net_usd"] > 0:
        warn.append("$%.2f MTD net (gross $%.2f)" % (d["net_usd"], d["gross_usd"]))
    status, alerts = _verdict("gh-actions", crit, warn)
    if crit or warn:
        summary = _msg((crit + warn)[0])
    elif legacy and not enhanced_items:
        summary = "%d/%s min used" % (d.get("legacy_minutes_used") or 0, d.get("legacy_included_minutes") or "?")
    else:
        summary = "$%.2f net MTD (gross $%.2f)" % (d["net_usd"], d["gross_usd"])
    return _res(status, summary, d, None, alerts)

"""SaaS and cloud sources: Cloudflare, Vercel, UptimeRobot, Hetzner, GitHub,
Sentry, Stripe, Resend, Langfuse, AI API spend and Twilio. The AWS account and its
bill share these helpers from aws.py and aws_cost.py, which keep their own audit.

Every source returns {"status": ok|warn|crit|unknown, "summary": "<= 60 chars",
"details": {...}, "error": None | "short reason"}, plus "alerts" for each warn
or crit issue. "unknown" with an error is a soft failure (no credentials, an
expired login, a send-only key): the collector keeps the last good reading.
The panels are in panels/cloud.py; docs/reference/saas.md lists every setting.

Rules this module keeps (a test audits the first; preserve them all):
  * Read-only. GET everywhere, plus exactly three read-only POST queries:
    Cloudflare GraphQL, GitHub GraphQL and the UptimeRobot v2 getMonitors
    fallback. Nothing creates, changes or deletes remote state; the only
    subprocess is `gh auth token`.
  * Credentials travel in request headers (UptimeRobot v2 alone takes its key
    in the POST body): never in a URL, an error, a summary or the details, and
    every result is scrubbed of credential values once more before it is kept.
  * No redirect is ever followed, so a credential cannot be forwarded to
    another host, and a token only goes to the host it was issued for, never to
    one an API merely names (Sentry region URLs must be Sentry's own hosts, an
    UptimeRobot next page must stay on its API host).
  * Every request and subprocess has a hard timeout, and remote error text is
    scrubbed of anything token-shaped before it is kept.
"""
from __future__ import annotations

import re
import time
import urllib.parse

from .. import core, secrets
from ..registry import source

REQ_TIMEOUT = 6.0   # seconds per HTTP request
SUMMARY_MAX = 60


def _now() -> float:
    return core.now()  # one seam, so tests can pin the clock


# ── Transport ───────────────────────────────────────────────────────────────
class ProbeError(Exception):
    """A transport failure or an unusable answer. The message is secret-free."""


def _req(ctx, method, url, headers=None, params=None, form=None, body=None, basic=None, timeout=None):
    """One request through ctx.http that never follows a redirect. HTTP errors
    come back as a Resp; transport failures raise ProbeError with a short reason."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    try:
        return ctx.http(url, method=method, headers=headers, data=form, json_body=body, basic=basic,
                        timeout=timeout or REQ_TIMEOUT, redirects=False)
    except ValueError as e:  # a non-http(s) scheme, or a URL http.client refuses
        raise ProbeError("unsupported URL scheme" if "scheme" in str(e) else "invalid URL") from None
    except Exception as e:  # noqa: BLE001 — urllib raises a zoo: URLError, socket.timeout, http.client errors
        raise ProbeError(core.net_reason(e)) from None


def _json(r):
    return r.body if isinstance(r.body, (dict, list)) else None


def _api_err(r, label="") -> str:
    """'HTTP 401: message' from a failed response, the message scrubbed."""
    j, msg = r.body, ""
    if isinstance(j, dict):
        for k in ("message", "error", "detail", "msg", "name"):
            v = j.get(k)
            if isinstance(v, str) and v:
                msg = v
                break
            if isinstance(v, dict) and isinstance(v.get("message"), str):
                msg = v["message"]
                break
        if not msg and isinstance(j.get("errors"), list) and j["errors"]:
            e0 = j["errors"][0]
            msg = e0.get("message", "") if isinstance(e0, dict) else e0
    return "%sHTTP %d" % (label, r.status) + (": " + _scrub(msg) if msg else "")


def _get(ctx, url, headers=None, params=None, label="", basic=None, expect=dict):
    """GET and parse JSON. A non-2xx, non-JSON or wrongly shaped (not `expect`) answer raises ProbeError."""
    r = _req(ctx, "GET", url, headers, params, basic=basic)
    if not r.ok:
        raise ProbeError(_api_err(r, label))
    j = _json(r)
    if j is None:
        raise ProbeError("%snon-JSON response" % label)
    if not isinstance(j, expect):
        raise ProbeError("%sunexpected response shape" % label)
    return j


def _pmap(calls, timeout=None, workers=None) -> dict:
    """Run zero-arg callables concurrently → {key: (value, error)}. Nothing escapes.
    Plain daemon threads, as in core.parallel, but the error text is kept. With
    `workers`, at most that many run at once, and one still queued at the deadline
    never starts (it reads as a timeout, like one still running)."""
    import threading
    out = {}
    deadline = time.monotonic() + (timeout or 2 * REQ_TIMEOUT + 1)
    gate = threading.BoundedSemaphore(workers) if workers else None

    def run(key, fn):
        if gate is not None and not gate.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return
        try:
            out[key] = (fn(), None)
        except ProbeError as e:
            out[key] = (None, str(e))
        except Exception as e:  # noqa: BLE001 — a parser bug must not take the whole source down
            out[key] = (None, "%s: %s" % (type(e).__name__, _scrub(e)))
        finally:
            if gate is not None:
                gate.release()
    threads = [threading.Thread(target=run, args=(k, f), daemon=True) for k, f in calls.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return {k: out.get(k, (None, "timeout")) for k in calls}


# ── Small helpers ───────────────────────────────────────────────────────────
_TOKENISH = re.compile(r"[A-Za-z0-9_\-.]{24,}")
_MASKED_KEY = re.compile(r"\b[A-Za-z]{2,6}-\.\.\.[A-Za-z0-9]{2,8}\b")  # 'xai-...54nd' still names a key
_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$")


def _scrub(s, n=90) -> str:
    """Remote error text made safe to keep: whitespace collapsed, key-like runs masked, length capped."""
    text = " ".join(str(s).split())
    text = _MASKED_KEY.sub("<key>", text)
    text = _TOKENISH.sub("<redacted>", text)
    return text if len(text) <= n else text[:n - 3] + "..."


def _clip(s, n=SUMMARY_MAX) -> str:
    text = " ".join(str(s).split())
    return text if len(text) <= n else text[:n - 3] + "..."


def _dicts(v) -> list:
    """The dict elements of a list-valued API field; None, other types and junk elements are dropped."""
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x) -> int:
    v = _f(x)
    return int(v) if v is not None and abs(v) < 1e15 else 0


def _ts(s):
    """RFC 3339-ish text → epoch seconds; None for junk and Go's zero time. Our own
    parser: Python 3.9's fromisoformat rejects the "+00" offsets Postgres-backed APIs send."""
    import datetime
    m = _TS.match(s.strip()) if isinstance(s, str) else None
    if not m:
        return None
    y, mo, d, h, mi, se, frac, tz = m.groups()
    if int(y) <= 1:
        return None  # 0001-01-01T00:00:00Z: never
    try:
        t = datetime.datetime(int(y), int(mo), int(d), int(h), int(mi), int(se), tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, OverflowError):  # well-formed but impossible: month 13, 25:61
        return None
    if tz and tz != "Z":
        digits = tz[1:].replace(":", "")
        off = int(digits[:2]) * 3600 + int(digits[2:] or 0) * 60
        t = t - off if tz[0] == "+" else t + off
    return t + (int((frac or "0")[:6].ljust(6, "0")) / 1e6)


def _iso(t) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _month_start(t) -> int:
    import calendar
    g = time.gmtime(t)
    return calendar.timegm((g.tm_year, g.tm_mon, 1, 0, 0, 0, 0, 0, 0))


def _num(cfg, key, default) -> float:
    v = _f(cfg.get(key, default))
    return float(default) if v is None else v


def _strs(v) -> list:
    """A list setting: a list, or one comma-separated string."""
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(s).strip() for s in v if str(s).strip()] if isinstance(v, (list, tuple)) else []


def _ignored(name, patterns) -> bool:
    """`ignore` settings match case-insensitive substrings of a name."""
    low = str(name or "").lower()
    return any(p.lower() in low for p in patterns)


# ── Results ─────────────────────────────────────────────────────────────────
_VALID = ("ok", "warn", "crit", "unknown")


def _res(status, summary, details=None, error=None, alerts=None) -> dict:
    out = {"status": status, "summary": _clip(summary), "details": details or {}, "error": error}
    if alerts:
        out["alerts"] = alerts
    return out


def _fail(err, summary=None) -> dict:
    """unknown + error: a soft failure, so the collector keeps the last good reading."""
    return _res("unknown", _scrub(summary or err, SUMMARY_MAX), {}, _scrub(err, 200))


def _missing(*names) -> dict:
    msg = "missing env: " + ", ".join(names)
    return _res("unknown", msg, {}, msg)


def _verdict(label, crit, warn):
    """(status, alerts) from issue lists; an issue is a message or (message, fix command)."""
    alerts = []
    for sev, items in (("crit", crit), ("warn", warn)):
        for it in items:
            msg, fix = it if isinstance(it, tuple) else (it, None)
            alerts.append(dict({"sev": sev, "msg": "%s: %s" % (label, msg)}, **({"fix": fix} if fix else {})))
    return ("crit" if crit else "warn" if warn else "ok"), alerts[:6]


def _msg(it) -> str:
    return it[0] if isinstance(it, tuple) else it


def _worst(statuses) -> str:
    """crit > warn > ok; "unknown" only when nothing is known."""
    rank = {"ok": 0, "warn": 1, "crit": 2}
    known = [s for s in statuses if s in rank]
    return max(known, key=lambda s: rank[s]) if known else "unknown"


def _normalize(r) -> dict:
    """Force the documented shape, whatever a fetcher returned."""
    r = r if isinstance(r, dict) else {}
    err = r.get("error")
    out = {"status": r.get("status") if r.get("status") in _VALID else "unknown",
           "summary": _clip(r.get("summary", "")),
           "details": r.get("details") if isinstance(r.get("details"), dict) else {},
           "error": None if err is None else _clip(err, 300)}
    alerts = [a for a in r.get("alerts") or [] if isinstance(a, dict) and a.get("msg")] if isinstance(r.get("alerts"), list) else []
    if alerts:
        out["alerts"] = [dict({"sev": a.get("sev") if a.get("sev") in ("crit", "warn", "info") else "warn",
                               "msg": _clip(a["msg"], 200)}, **({"fix": str(a["fix"])[:200]} if a.get("fix") else {}))
                         for a in alerts[:8]]
    return out


def _scrubbed(obj, vals):
    """obj with every credential value replaced, keys included: the last line of defense."""
    if isinstance(obj, str):
        for v in vals:
            if v in obj:
                obj = obj.replace(v, "<redacted>")
        return obj
    if isinstance(obj, dict):
        return {_scrubbed(k, vals): _scrubbed(v, vals) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrubbed(v, vals) for v in obj]
    return obj


def _cred(ctx, value):
    """Note a credential that did not come from a secret-named variable (gh's token,
    a dotenv file, a login email), so the final scrub knows it too."""
    if value:
        ctx.__dict__.setdefault("creds", []).append(str(value))
    return value


def _finish(ctx, label, res, alert_unknown=False) -> dict:
    """The documented shape, warn/crit always raising an alert, credential values scrubbed."""
    out = _normalize(res)
    if out["status"] in ("warn", "crit") and not out.get("alerts"):
        out["alerts"] = [{"sev": out["status"], "msg": "%s: %s" % (label, out["summary"])}]
    elif out["status"] == "unknown" and alert_unknown and out["error"]:
        out["alerts"] = [{"sev": "warn", "msg": "%s: %s" % (label, _clip(out["error"], 80))}]
    vals = set(secrets.sensitive_values(ctx.env))
    vals.update(v for v in getattr(ctx, "creds", ()) if len(v) >= 6)
    return _scrubbed(out, sorted(vals, key=len, reverse=True)) if vals else out


def _guard(ctx, label, fn, alert_unknown=False) -> dict:
    """Run a fetcher; a bug in it becomes an "internal error" soft failure, never an exception."""
    try:
        res = fn(ctx)
    except Exception as e:  # noqa: BLE001 — the fetchers are hardened, but a parser bug must not cost the panel
        res = _res("unknown", "internal error", {}, "%s: %s" % (type(e).__name__, _scrub(e)))
    return _finish(ctx, label, res, alert_unknown)


def _saas(name, label, alert_unknown=False, **kw):
    """Register a fetcher as a source (global unless `scope` says otherwise) that always
    answers in the documented shape: _guard, then _finish. `label` prefixes its alerts."""
    def deco(fn):
        def run(ctx):
            return _guard(ctx, label, fn, alert_unknown(ctx) if callable(alert_unknown) else alert_unknown)
        run.__doc__ = fn.__doc__
        source(name, **dict({"scope": "global"}, **kw))(run)
        return fn
    return deco


# ── Cloudflare: tunnels, zones, 24h edge traffic (GraphQL), Email Routing ──
CF = "https://api.cloudflare.com/client/v4"
_CF_TRAFFIC = ("query($z:[String!],$s:Time!,$u:Time!){viewer{zones(filter:{zoneTag_in:$z}){zoneTag "
               "httpRequests1hGroups(limit:1,filter:{datetime_geq:$s,datetime_lt:$u}){"
               "sum{requests cachedRequests threats responseStatusMap{edgeResponseStatus requests}} uniq{uniques}}}}}")
_CF_EMAIL = ("query($z:[String!],$s:Time!,$u:Time!){viewer{zones(filter:{zoneTag_in:$z}){zoneTag "
             "emailRoutingAdaptiveGroups(limit:20,filter:{datetime_geq:$s,datetime_lt:$u}){count dimensions{status}}}}}")
_CF_ID = re.compile(r"^[0-9a-f]{32}$")  # zone and account ids: also all that may enter a URL path
_CF_GQL_ZONES = 10  # GraphQL answers "too many zones requested" above ten zones per query
_RANK = {"crit": 2, "warn": 1}


def _cf_auth(env):
    """Headers for an API token (preferred: it can be scoped read-only) or for the
    global key, plus the login email to scrub; (None, None) without credentials."""
    tok = env.get("CLOUDFLARE_API_TOKEN") or env.get("CF_API_TOKEN")
    if tok:
        return {"Authorization": "Bearer " + tok}, None
    email = env.get("CF_EMAIL") or env.get("CLOUDFLARE_EMAIL")
    key = env.get("CF_GLOBAL_API_KEY") or env.get("CLOUDFLARE_API_KEY")
    if email and key:
        return {"X-Auth-Email": email, "X-Auth-Key": key}, email
    return None, None


def _cf_rest(ctx, h, path, params=None):
    r = _req(ctx, "GET", CF + path, h, params)
    j = _json(r)
    if not r.ok or not isinstance(j, dict) or j.get("success") is False:
        raise ProbeError(_api_err(r, "cf "))
    return j.get("result")


def _cf_gql(ctx, h, query, tags, since, until) -> list:
    """A read-only GraphQL query (POST is the only verb the endpoint takes) → its zone nodes."""
    r = _req(ctx, "POST", CF + "/graphql", h, body={"query": query, "variables": {"z": tags, "s": since, "u": until}})
    j = _json(r)
    errs = j.get("errors") if isinstance(j, dict) else None
    if not r.ok or not isinstance(j, dict) or errs:
        e0 = errs[0] if isinstance(errs, list) and errs and isinstance(errs[0], dict) else {}
        raise ProbeError("cf graphql HTTP %d%s" % (r.status, (": " + _scrub(e0.get("message", ""))) if e0 else ""))
    return _dicts(_d(_d(j.get("data")).get("viewer")).get("zones"))


def _cf_select(cfg, env, listed):
    """(zones to judge, explicitly chosen?, configured names that matched nothing).
    [sources.cloudflare] zones (ids or names), else CF_ZONE_ID / CLOUDFLARE_ZONE_ID,
    else every listed zone not matched by `ignore`, up to max_zones."""
    want = _strs(cfg.get("zones")) or _strs(env.get("CF_ZONE_ID") or env.get("CLOUDFLARE_ZONE_ID") or "")
    rows = [z for z in listed or [] if _CF_ID.match(str(z.get("id") or ""))]
    if not want:
        keep = [z for z in rows if not _ignored(z.get("name"), _strs(cfg.get("ignore")))]
        return keep[:max(1, int(_num(cfg, "max_zones", 25)))], False, []
    by_id = {z["id"]: z for z in rows}
    by_name = {str(z.get("name") or "").lower(): z for z in rows}
    out, unknown = [], []
    for w in want:
        z = by_id.get(w) or by_name.get(w.lower())
        if z:
            out.append(z)
        elif _CF_ID.match(w):  # the zone list failed or is paged past it: judge it by id
            out.append({"id": w, "name": "zone " + w[:8], "status": None})
        else:
            unknown.append(w)
    return out, True, unknown


def _cf_tunnels(cfg, tun, now, crit, warn) -> dict:
    """healthy / degraded / down. Only a recent drop is an incident: a tunnel that has
    been down for weeks is retired, not broken."""
    recent_s = _num(cfg, "tunnel_recent_days", 14) * 86400
    ignore = _strs(cfg.get("ignore"))
    healthy = degraded = stale = 0
    down_recent, by_status = [], {}
    for t in tun:
        st, name = str(t.get("status") or "?"), str(t.get("name") or "?")
        if _ignored(name, ignore):
            continue
        by_status.setdefault(st, []).append(name)
        if st == "healthy":
            healthy += 1
        elif st == "degraded":
            degraded += 1
            warn.append("tunnel %s degraded" % name)
        else:  # down / inactive (never connected)
            gone = _ts(t.get("conns_inactive_at"))
            if gone and now - gone < recent_s:
                down_recent.append(name)
                crit.append("tunnel %s down" % name)
            else:
                stale += 1
    return {"total": healthy + degraded + len(down_recent) + stale, "healthy": healthy, "degraded": degraded,
            "down_recent": down_recent, "stale_down": stale, "by_status": {k: v[:12] for k, v in by_status.items()}}


def _cf_traffic(cfg, nodes, names, crit, warn) -> tuple:
    """(account totals, per-zone rows): each zone's 5xx rate is judged on its own, so a
    small broken site is not averaged away by a busy healthy one."""
    wp, cp = _num(cfg, "warn_5xx_pct", 1.0), _num(cfg, "crit_5xx_pct", 5.0)
    wn, cn = _num(cfg, "min_requests_warn", 20), _num(cfg, "min_requests_crit", 50)
    tot = {"requests": 0, "cached": 0, "threats": 0, "http_5xx": 0, "uniques": 0}
    per = []
    for n in nodes:
        g = (_dicts(n.get("httpRequests1hGroups")) or [{}])[0]
        s = _d(g.get("sum"))
        req = _int(s.get("requests"))
        e5 = sum(_int(x.get("requests")) for x in _dicts(s.get("responseStatusMap"))
                 if 500 <= _int(x.get("edgeResponseStatus")) < 600)
        for k, v in (("requests", req), ("cached", _int(s.get("cachedRequests"))), ("threats", _int(s.get("threats"))),
                     ("http_5xx", e5), ("uniques", _int(_d(g.get("uniq")).get("uniques")))):
            tot[k] += v
        pct = 100.0 * e5 / req if req else 0.0
        lv = "crit" if req >= cn and pct >= cp else "warn" if req >= wn and pct >= wp else "ok"
        name = names.get(n.get("zoneTag")) or "?"
        if lv != "ok":
            (crit if lv == "crit" else warn).append("5xx rate %.1f%%%s" % (pct, " on " + name if len(nodes) > 1 else ""))
        per.append({"zone": name, "requests": req, "http_5xx": e5, "pct_5xx": round(pct, 2), "level": lv})
    req = tot["requests"]
    totals = {"requests": req, "cached_pct": round(100.0 * tot["cached"] / req, 1) if req else 0.0,
              "threats": tot["threats"], "http_5xx": tot["http_5xx"],
              "pct_5xx": round(100.0 * tot["http_5xx"] / req, 2) if req else 0.0, "uniques": tot["uniques"], "zones": len(per)}
    per.sort(key=lambda p: (-_RANK.get(p["level"], 0), -p["requests"]))
    return totals, per[:8]


def _cf_email(cfg, live, res, stats, warn):
    """Email Routing per zone. Zones that never set it up are left out unless
    email_routing = "required"; enabled-but-broken is a warning ("unlocked" only
    means its DNS records may be edited)."""
    required = str(cfg.get("email_routing")).lower() == "required"
    rows = []
    for z in live:
        v, _e = res.get("email:" + z["name"], (None, None))
        if v is None:
            continue
        em = _d(v)
        on, st = bool(em.get("enabled")), str(em.get("status") or "?")
        if not on and not required:
            continue
        lv = "ok" if on and st in ("ready", "unlocked") else "warn"
        if lv != "ok":
            warn.append("email routing %s on %s" % (st if on else "off", z["name"]))
        rows.append({"zone": z["name"], "enabled": on, "status": st, "last_24h": stats.get(z["id"]) or {}, "level": lv})
    last = {}
    for counts in stats.values():
        for k, n in counts.items():
            last[k] = last.get(k, 0) + n
    if not rows and not last:
        return None
    return {"zones": rows[:12], "enabled": sum(1 for r in rows if r["enabled"]),
            "ready": sum(1 for r in rows if r["level"] == "ok"), "last_24h": last}


@_saas("cloudflare", "cloudflare", ttl=600, timeout=25, defaults={
    "account_id": "", "zones": [], "max_zones": 25, "ignore": [], "tunnels": True, "tunnel_recent_days": 14,
    "traffic": True, "warn_5xx_pct": 1.0, "crit_5xx_pct": 5.0, "min_requests_warn": 20, "min_requests_crit": 50,
    "email_routing": True})
def _cloudflare(ctx):
    """Cloudflare: tunnels, zones, 24h edge traffic and 5xx, Email Routing"""
    h, email = _cf_auth(ctx.env)
    if not h:
        return _missing("CLOUDFLARE_API_TOKEN (or CF_EMAIL + CF_GLOBAL_API_KEY)")
    _cred(ctx, email)
    cfg, now = ctx.cfg, _now()
    res = _pmap({"zones": lambda: _cf_rest(ctx, h, "/zones", {"per_page": 50})})
    listed = _dicts(res["zones"][0]) if res["zones"][0] is not None else None
    sel, explicit, unknown = _cf_select(cfg, ctx.env, listed)
    acct = str(cfg.get("account_id") or ctx.env.get("CLOUDFLARE_ACCOUNT_ID") or ctx.env.get("CF_ACCOUNT_ID") or "")
    if not acct and listed:  # every zone in one account (the usual case) names it for us
        owners = {str(_d(z.get("account")).get("id") or "") for z in listed} - {""}
        acct = owners.pop() if len(owners) == 1 else ""
    live = [z for z in sel if z.get("status") in ("active", None)]
    tags = [z["id"] for z in live]
    since, until = _iso(now - 86400), _iso(now)
    calls = {}
    if cfg.get("tunnels", True) and _CF_ID.match(acct):
        calls["tunnels"] = lambda: _cf_rest(ctx, h, "/accounts/%s/cfd_tunnel" % acct, {"is_deleted": "false", "per_page": 100})
    for i in range(0, len(tags), _CF_GQL_ZONES):
        chunk = tags[i:i + _CF_GQL_ZONES]
        if cfg.get("traffic", True):
            calls["traffic%d" % i] = lambda c=chunk: _cf_gql(ctx, h, _CF_TRAFFIC, c, since, until)
        if cfg.get("email_routing", True):
            calls["email stats%d" % i] = lambda c=chunk: _cf_gql(ctx, h, _CF_EMAIL, c, since, until)
    if cfg.get("email_routing", True):
        for z in live:
            calls["email:" + z["name"]] = lambda z=z: _cf_rest(ctx, h, "/zones/%s/email/routing" % z["id"])
    res.update(_pmap(calls))
    errors = {k: e for k, (v, e) in res.items() if e}
    if len(errors) == len(res):
        return _fail("; ".join("%s: %s" % kv for kv in sorted(errors.items())), "all Cloudflare calls failed")

    crit, warn, d = [], [], {"auth": "global key" if email else "token"}
    tun = res.get("tunnels", (None, None))[0]
    if tun is not None:
        d["tunnels"] = _cf_tunnels(cfg, _dicts(tun), now, crit, warn)
    if listed is not None:
        # Zone status costs no extra call, so auto mode judges every listed zone, not just the first max_zones.
        pool = sel if explicit else [z for z in listed if not _ignored(z.get("name"), _strs(cfg.get("ignore")))]
        bad = [str(z.get("name")) for z in pool if z.get("status") not in ("active", None)]
        d["zones"] = {"total": len(listed), "judged": len(sel), "not_active": bad[:12]}
        if bad:
            warn.append("zone not active: " + ", ".join(bad[:3]))
        for w in unknown:
            warn.append("zone %s not found" % w)
    names = {z["id"]: str(z.get("name")) for z in sel}
    if any(k.startswith("traffic") and e is None for k, (v, e) in res.items()):
        nodes = [n for k, (v, e) in res.items() if k.startswith("traffic") and v for n in v]
        d["traffic_24h"], d["traffic_zones"] = _cf_traffic(cfg, nodes, names, crit, warn)
    if cfg.get("email_routing", True):
        stats = {}
        for n in [n for k, (v, _e) in res.items() if k.startswith("email stats") for n in v or []]:
            stats[n.get("zoneTag")] = {str(_d(x.get("dimensions")).get("status") or "?"): _int(x.get("count"))
                                       for x in _dicts(n.get("emailRoutingAdaptiveGroups"))}
        em = _cf_email(cfg, live, res, stats, warn)
        if em:
            d["email_routing"] = em
    if errors:
        d["partial_errors"] = errors
        core_calls = sorted({re.sub(r"\d+$", "", k) for k in errors if k in ("tunnels", "zones") or k.startswith("traffic")})
        if core_calls:  # a missing email routing reading is reported per zone, not as an outage
            warn.append("partial data (%s)" % ", ".join(core_calls))
    status, alerts = _verdict("cloudflare", crit, warn)
    parts = []
    tu = d.get("tunnels")
    if tu:
        parts.append("%d/%d tunnels up" % (tu["healthy"], tu["healthy"] + tu["degraded"] + len(tu["down_recent"])))
    tr = d.get("traffic_24h")
    if tr:
        parts.append("%s req/24h" % core.human_num(tr["requests"]))
        parts.append("%d 5xx (%.1f%%)" % (tr["http_5xx"], tr["pct_5xx"]))
    issues = [_msg(i) for i in crit + warn]
    # Tunnel counts already tell an outage; without them the first issue is the headline.
    summary = issues[0] if issues and not tu else (", ".join(parts) or "no data")
    return _res(status, summary, d, None, alerts)


# ── Vercel: one call, /v9/projects carries each latest production deploy ───
VERCEL = "https://api.vercel.com"


@_saas("vercel", "vercel", ttl=900, timeout=20, needs=("VERCEL_TOKEN",), defaults={
    "team_id": "", "projects": [], "ignore": [], "crit_hours": 24, "warn_days": 7})
def _vercel(ctx):
    """Vercel: the latest production deployment of every project"""
    tok = ctx.env.get("VERCEL_TOKEN")
    if not tok:
        return _missing("VERCEL_TOKEN")
    cfg, h = ctx.cfg, {"Authorization": "Bearer " + tok}
    params = {"limit": 100}
    team = str(cfg.get("team_id") or ctx.env.get("VERCEL_TEAM_ID") or "")
    if team:  # optional: a team token's default scope is already the team
        params["teamId"] = team
    projects = []
    try:
        for _ in range(5):
            j = _get(ctx, VERCEL + "/v9/projects", h, params, "vercel ")
            projects += _dicts(j.get("projects"))
            nxt = _d(j.get("pagination")).get("next")
            if not nxt:
                break
            params["until"] = nxt
    except ProbeError as e:
        return _fail(str(e))
    only, ignore = _strs(cfg.get("projects")), _strs(cfg.get("ignore"))
    crit_s, warn_s = _num(cfg, "crit_hours", 24) * 3600, _num(cfg, "warn_days", 7) * 86400
    now = _now()
    states, by_project, err_crit, err_warn, stale_err, building = {}, {}, [], [], [], []
    for p in projects:
        name = str(p.get("name") or "?")
        if (only and name not in only) or _ignored(name, ignore):
            continue
        prod = _d(_d(p.get("targets")).get("production"))
        st = str(prod.get("readyState") or "NONE")
        states[st] = states.get(st, 0) + 1
        by_project[name] = st
        made = _f(prod.get("createdAt"))
        age = now - made / 1000.0 if made else None
        if st == "ERROR":
            if age is not None and age < crit_s:
                err_crit.append(name)
            elif age is not None and age < warn_s:
                err_warn.append(name)
            else:
                stale_err.append(name)  # a long-dead project's last deploy, not news
        elif st in ("BUILDING", "QUEUED", "INITIALIZING"):
            building.append(name)
    # CANCELED is routine (ignored build steps, superseded builds): counted, never alarmed on.
    d = {"projects": len(by_project), "prod_states": states, "failing_24h": err_crit, "failing_7d": err_warn,
         "stale_errors": stale_err, "building": building, "prod_by_project": dict(list(by_project.items())[:40])}
    crit = ["prod deploy ERROR: " + ", ".join(err_crit)] if err_crit else []
    warn = ["prod deploy ERROR (<%dd): %s" % (warn_s // 86400, ", ".join(err_warn))] if err_warn else []
    status, alerts = _verdict("vercel", crit, warn)
    if status != "ok":
        return _res(status, (crit + warn)[0], d, None, alerts)
    return _res("ok", "%d projects, prod ok (%d READY, %d building)" % (len(by_project), states.get("READY", 0), len(building)), d)


# ── UptimeRobot: v3 REST, with the v2 POST getMonitors as a fallback ───────
UR = "https://api.uptimerobot.com"


def _ur_v3(ctx, key) -> list:
    import html
    mons, url, params = [], UR + "/v3/monitors", {"limit": 200}
    h = {"Authorization": "Bearer " + key}
    for _ in range(5):
        r = _req(ctx, "GET", url, h, params)
        j = _json(r)
        if not r.ok or not isinstance(j, dict):
            raise ProbeError(_api_err(r, "v3 "))
        for m in _dicts(j.get("data")):
            st = str(m.get("status", "")).upper()
            state = "up" if st == "UP" else "down" if st in ("DOWN", "SEEMS_DOWN") else "paused" if st == "PAUSED" else "other"
            dur = m.get("currentStateDuration")
            mons.append({"name": html.unescape(str(m.get("friendlyName", "?"))), "state": state,
                         "down_s": int(dur) if state == "down" and isinstance(dur, (int, float)) else None})
        nxt = j.get("nextLink")
        if not nxt:
            break
        # A next page is followed only on UptimeRobot's own API host; anything else is sent back as a cursor.
        if isinstance(nxt, str) and nxt.startswith("https://") and urllib.parse.urlsplit(nxt).hostname == "api.uptimerobot.com":
            url, params = nxt, None
        else:
            params = {"limit": 200, "cursor": nxt}
    return mons


def _ur_v2(ctx, key) -> list:
    mons, offset = [], 0
    for _ in range(5):
        r = _req(ctx, "POST", UR + "/v2/getMonitors", form={"api_key": key, "format": "json", "limit": 50, "offset": offset})
        j = _json(r)
        if not r.ok or not isinstance(j, dict) or j.get("stat") != "ok":
            raise ProbeError(_api_err(r, "v2 ") if not r.ok else "v2 unexpected response (stat=%s)" % _scrub(
                j.get("stat") if isinstance(j, dict) else None, 20))
        batch = _dicts(j.get("monitors"))
        for m in batch:
            code = m.get("status")
            state = "up" if code == 2 else "down" if code in (8, 9) else "paused" if code == 0 else "other"
            mons.append({"name": str(m.get("friendly_name", "?")), "state": state, "down_s": None})
        offset += len(batch)
        if not batch or offset >= _int(_d(j.get("pagination")).get("total")):
            break
    return mons


@_saas("uptimerobot", "uptime", ttl=300, timeout=15, needs=("UPTIMEROBOT_API_KEY",),
       defaults={"ignore": [], "recent_days": 7})
def _uptimerobot(ctx):
    """UptimeRobot: external monitors up, paused and down"""
    key = ctx.env.get("UPTIMEROBOT_API_KEY")
    if not key:
        return _missing("UPTIMEROBOT_API_KEY")
    api, err3, mons = "v3", None, None
    try:
        mons = _ur_v3(ctx, key)
    except ProbeError as e:
        err3 = str(e)
    if mons is None:
        api = "v2"
        try:
            mons = _ur_v2(ctx, key)
        except ProbeError as e:
            return _fail("v3: %s; v2: %s" % (err3, e))
    ignore = _strs(ctx.cfg.get("ignore"))
    kept = [m for m in mons if not _ignored(m["name"], ignore)]
    up = [m for m in kept if m["state"] == "up"]
    paused = [m for m in kept if m["state"] == "paused"]
    down = [m for m in kept if m["state"] == "down"]
    recent_s = _num(ctx.cfg, "recent_days", 7) * 86400
    d = {"api": api, "total": len(kept), "up": len(up), "paused": len(paused), "ignored": len(mons) - len(kept),
         "other": len(kept) - len(up) - len(paused) - len(down),
         "states": dict(list({m["name"]: m["state"] for m in kept}.items())[:60]),
         "down": [{"name": m["name"], "down_days": None if m["down_s"] is None else round(m["down_s"] / 86400.0, 1)}
                  for m in down[:12]]}
    if err3:
        d["v3_error"] = err3
    if not down:
        return _res("ok", "%d/%d monitors up%s" % (len(up), len(kept), ", %d paused" % len(paused) if paused else ""), d)
    crit, warn = [], []
    for m in down:  # a monitor down for weeks is a known, abandoned target; an unknown duration counts as recent
        (crit if m["down_s"] is None or m["down_s"] < recent_s else warn).append(
            "%s down%s" % (m["name"], "" if m["down_s"] is None else " %dd" % (m["down_s"] // 86400)))
    status, alerts = _verdict("uptime", crit, warn)
    first = down[0]
    tail = "" if first["down_s"] is None else " %dd" % (first["down_s"] // 86400)
    return _res(status, "%d/%d up; down: %s%s" % (len(up), len(kept), first["name"], tail), d, None, alerts)


# ── Hetzner: Cloud servers + Storage Boxes (no billing API exists) ─────────
HCLOUD = "https://api.hetzner.cloud/v1"
HETZNER = "https://api.hetzner.com/v1"  # Storage Boxes moved to the new API; the same project token reads them
BACKUP_SURCHARGE = 1.2  # server backups cost 20% of the server's price


def _price(prices, loc):
    for p in _dicts(prices):
        if p.get("location") == loc:
            return _f(_d(p.get("price_monthly")).get("gross"))
    return None


@_saas("hetzner", "hetzner", ttl=1800, timeout=20, needs=("HCLOUD_TOKEN",), defaults={
    "storage_boxes": True, "ignore": [], "traffic_warn_pct": 90, "box_warn_pct": 80, "box_crit_pct": 90})
def _hetzner(ctx):
    """Hetzner: servers and traffic, Storage Box fill, a run-rate"""
    tok = ctx.env.get("HCLOUD_TOKEN")
    if not tok:
        return _missing("HCLOUD_TOKEN")
    cfg, h = ctx.cfg, {"Authorization": "Bearer " + tok}
    calls = {"servers": lambda: _get(ctx, HCLOUD + "/servers", h, {"per_page": 50}, "hcloud "),
             "volumes": lambda: _get(ctx, HCLOUD + "/volumes", h, {"per_page": 50}, "hcloud "),
             "pricing": lambda: _get(ctx, HCLOUD + "/pricing", h, None, "hcloud ")}
    if cfg.get("storage_boxes", True):
        calls["boxes"] = lambda: _get(ctx, HETZNER + "/storage_boxes", h, {"per_page": 50}, "hetzner ")
    res = _pmap(calls)
    errors = {k: e for k, (v, e) in res.items() if e}
    if len(errors) == len(res):
        return _fail("; ".join("%s: %s" % kv for kv in sorted(errors.items())), "all Hetzner calls failed")
    crit, warn = [], []
    ignore = _strs(cfg.get("ignore"))
    tw = _num(cfg, "traffic_warn_pct", 90)
    bw, bc = _num(cfg, "box_warn_pct", 80), _num(cfg, "box_crit_pct", 90)
    d = {"cost_mtd": "not exposed by any Hetzner API; est_monthly is a price-list run-rate"}
    est, est_ok = 0.0, True
    servers = _dicts(_d(res["servers"][0]).get("servers")) if res["servers"][0] is not None else None
    running = 0
    if servers is not None:
        servers = [s for s in servers if not _ignored(s.get("name"), ignore)]
        rows = []
        for s in servers:
            st, name = str(s.get("status") or "?"), str(s.get("name") or "?")
            loc = _d(s.get("location")).get("name") or _d(_d(s.get("datacenter")).get("location")).get("name")
            if st == "running":
                running += 1
                lv = "ok"
            elif st in ("off", "deleting", "unknown"):
                lv = "crit"
                crit.append("server %s %s" % (name, st))
            else:  # starting, stopping, migrating, rebuilding: in motion
                lv = "warn"
                warn.append("server %s %s" % (name, st))
            inc, out = _f(s.get("included_traffic")) or 0.0, _f(s.get("outgoing_traffic")) or 0.0
            pct = round(100.0 * out / inc, 1) if inc else None
            if pct is not None and pct >= tw:
                warn.append("server %s traffic %.0f%%" % (name, pct))
                lv = "warn" if lv == "ok" else lv
            price = _price(_d(s.get("server_type")).get("prices"), loc)
            if price is None:
                est_ok = False
            else:
                est += price * (BACKUP_SURCHARGE if s.get("backup_window") else 1.0)
            rows.append({"name": name, "status": st, "type": _d(s.get("server_type")).get("name"), "location": loc,
                         "traffic_out_pct": pct, "level": lv})
        d["servers"] = rows
    vols, pricing = res["volumes"][0], res["pricing"][0]
    gb = sum(_int(v.get("size")) for v in _dicts(_d(vols).get("volumes")))
    per_gb = _f(_d(_d(_d(_d(pricing).get("pricing")).get("volume")).get("price_per_gb_month")).get("gross"))
    if vols is None or pricing is None or (gb and per_gb is None):
        est_ok = False
    else:
        est += gb * (per_gb or 0.0)
    boxes = _dicts(_d(res.get("boxes", (None, None))[0]).get("storage_boxes"))
    if boxes:
        rows = []
        for b in boxes:
            name, loc = str(b.get("name") or "storage box"), _d(b.get("location")).get("name")
            if _ignored(name, ignore):
                continue
            quota = _f(_d(b.get("storage_box_type")).get("size")) or 0.0
            used = _f(_d(b.get("stats")).get("size")) or 0.0
            p = round(100.0 * used / quota, 1) if quota else None
            lv = "crit" if p is not None and p >= bc else "warn" if (p is not None and p >= bw) or b.get("status") != "active" else "ok"
            if b.get("status") != "active":
                warn.append("storage box %s %s" % (name, b.get("status")))
            if p is not None and p >= bw:
                (crit if p >= bc else warn).append("storage box %s %.0f%% full" % (name, p))
            price = _price(_d(b.get("storage_box_type")).get("prices"), loc)
            est += price or 0.0
            rows.append({"name": name, "status": b.get("status"), "location": loc, "used_pct": p,
                         "used_tib": round(used / 2.0 ** 40, 2), "quota_tib": round(quota / 2.0 ** 40, 2), "level": lv})
        d["storage_boxes"] = rows
    if est_ok and servers is not None:
        d["est_monthly"] = round(est, 2)
        d["currency"] = str(_d(_d(pricing).get("pricing")).get("currency") or "USD").upper()
    if errors:
        d["partial_errors"] = errors
        if "servers" in errors or "boxes" in errors:  # the price list is an extra: its failure is not news
            warn.append("partial data (%s)" % ", ".join(k for k in ("servers", "boxes") if k in errors))
    status, alerts = _verdict("hetzner", crit, warn)
    parts = []
    if servers is not None:
        parts.append("%d/%d servers up" % (running, len(servers)))
    if d.get("storage_boxes"):
        top = max(d["storage_boxes"], key=lambda r: r["used_pct"] or 0)
        parts.append("box %.0f%%" % (top["used_pct"] or 0))
    if "est_monthly" in d:
        parts.append(("~$%.0f/mo" % d["est_monthly"]) if d["currency"] == "USD" else "~%.0f %s/mo" % (d["est_monthly"], d["currency"]))
    summary = ", ".join(parts) or "no data"
    if status != "ok":
        summary = _msg((crit + warn)[0]) + "; " + summary
    return _res(status, summary, d, None, alerts)


# ── GitHub: CI verdict per repo branch, open PRs, changes requested ─────────
GH = "https://api.github.com"
_REPO = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9_./\-]+$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,38})$")
_FAILED = ("failure", "timed_out", "startup_failure", "action_required")
_INERT = ("cancelled", "skipped", "neutral", "stale")
_PENDING = ("queued", "in_progress", "waiting", "pending", "requested")


def _gh_headers(tok) -> dict:
    return {"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _gh_cli_token(ctx):
    """gh's own login, for when the environment holds no token (or a stale one)."""
    gh = core.which("gh", tuple(ctx.cfg.get("paths") or ()))
    if not gh:
        return None
    rc, out, _ = ctx.run([gh, "auth", "token", "--hostname", "github.com"], timeout=5)  # never a GH_HOST enterprise token
    tok = out.strip()
    return tok if rc == 0 and tok and not any(c.isspace() for c in tok) else None


def _gh_token(ctx, want_user):
    """(token, where it came from, its login) for the first candidate GitHub accepts.
    Environment tokens go stale; gh's own login is the fallback."""
    seen = set()
    for src in ("GH_TOKEN", "GITHUB_TOKEN", "gh"):
        tok = _gh_cli_token(ctx) if src == "gh" else ctx.env.get(src)
        if not tok or tok in seen:
            continue
        seen.add(_cred(ctx, tok))
        h = _gh_headers(tok)
        if want_user:
            r = _req(ctx, "GET", GH + "/user", h)
            login = str(_d(_json(r)).get("login") or "")
            if r.ok and _LOGIN.match(login):
                return tok, src, login
            if r.status != 403:  # 401: stale or revoked. 403: an app token, valid but with no user behind it
                continue
        if _req(ctx, "GET", GH + "/rate_limit", h).ok:  # free: not counted against the rate limit
            return tok, src, None
    return None, "", None


def _gh_repos(cfg) -> list:
    """[(owner/name, branch, label)] from `repos` entries "owner/name", "owner/name@branch"
    or {repo, branch, label}. Anything that is not a plain repo or branch name is dropped:
    these go into URL paths."""
    default = str(cfg.get("branch") or "main")
    out = []
    for r in cfg.get("repos") or []:
        if isinstance(r, dict):
            repo, br, label = str(r.get("repo") or ""), str(r.get("branch") or default), str(r.get("label") or "")
        else:
            repo, _, br = str(r).strip().partition("@")
            br, label = br or default, ""
        if _REPO.match(repo) and _BRANCH.match(br) and ".." not in repo + br and repo not in [o[0] for o in out]:
            out.append((repo, br, label))
    shorts = [o[0].split("/")[1] for o in out]  # "api", unless two owners both have an "api"
    return [(repo, br, label or (repo.split("/")[1] if shorts.count(repo.split("/")[1]) == 1 else repo)) for repo, br, label in out]


def _ci_state(runs, stale_days=14, now=None):
    """(failing, stale-failing, pending, newest) from newest-first workflow runs.

    The latest conclusive run of each workflow decides. Cancelled, skipped and
    neutral runs are superseded or inert, so they never decide; a workflow whose
    latest conclusive run failed over stale_days ago is stale, not an incident."""
    now = _now() if now is None else now
    seen, pending, newest = {}, 0, None
    for r in runs:
        c = r.get("conclusion")
        if c is None:
            pending += 1 if r.get("status") in _PENDING else 0
            continue
        if c in _INERT:
            continue
        seen.setdefault(str(r.get("name") or "?"), (c, _ts(r.get("updated_at"))))
        newest = newest or {"workflow": r.get("name"), "conclusion": c, "at": r.get("updated_at")}
    failed = [(n, t) for n, (c, t) in seen.items() if c in _FAILED]
    bad = [n for n, t in failed if t is None or now - t <= stale_days * 86400]
    return bad, [n for n, t in failed if n not in bad], pending, newest


@_saas("github", "github", ttl=600, timeout=25, requires=("repos",), defaults={
    "repos": [], "branch": "main", "user": "", "events": ["push", "repository_dispatch"], "stale_days": 14})
def _github(ctx):
    """GitHub: CI verdict per repo branch, open PRs, changes requested"""
    cfg = ctx.cfg
    repos = _gh_repos(cfg)
    if not repos:
        return _fail("no valid owner/name entries in [sources.github] repos")
    user = str(cfg.get("user") or "")
    if user and not _LOGIN.match(user):
        return _fail("[sources.github] user is not a GitHub login")
    try:
        tok, src, login = _gh_token(ctx, want_user=not user)
    except ProbeError as e:
        return _fail(str(e))
    if not tok:
        return _fail("no valid GitHub token (GH_TOKEN / GITHUB_TOKEN rejected or unset, gh not logged in)",
                     "no valid GitHub token")
    user = user or login or ""
    h = _gh_headers(tok)
    # Repo names and branches travel as GraphQL variables, never spliced into the query text.
    defs, parts, var = [], [], {}
    for i, (repo, br, _label) in enumerate(repos):
        owner, name = repo.split("/", 1)
        base = "repo:%s is:pr is:open" % repo + (" author:%s" % user if user else "")
        var.update({"p%d" % i: base, "c%d" % i: base + " review:changes_requested", "o%d" % i: owner, "n%d" % i: name,
                    "b%d" % i: "refs/heads/" + br})
        defs.append("$p%d:String!,$c%d:String!,$o%d:String!,$n%d:String!,$b%d:String!" % ((i,) * 5))
        parts.append("p%d:search(query:$p%d,type:ISSUE,first:1){issueCount} c%d:search(query:$c%d,type:ISSUE,first:1){issueCount} "
                     "h%d:repository(owner:$o%d,name:$n%d){ref(qualifiedName:$b%d){target{...on Commit{statusCheckRollup{state}}}}}"
                     % ((i,) * 8))
    query = "query(%s){%s}" % (",".join(defs), " ".join(parts))
    gql_errs = []

    def graphql():
        r = _req(ctx, "POST", GH + "/graphql", h, body={"query": query, "variables": var})  # a read-only query
        j = _json(r)
        errs = j.get("errors") if isinstance(j, dict) else None
        e0 = errs[0] if isinstance(errs, list) and errs and isinstance(errs[0], dict) else {}
        if not r.ok or not isinstance(j, dict) or not isinstance(j.get("data"), dict):
            raise ProbeError("github graphql HTTP %d%s" % (r.status, (": " + _scrub(e0.get("message", ""))) if e0 else ""))
        if errs:  # one unknown repo nulls its own fields; the other repos' answers still count
            gql_errs.append(_scrub(e0.get("message", "") or "error", 80))
        return j["data"]
    calls = {"gql": graphql}
    events = _strs(cfg.get("events")) or ["push"]
    for i, (repo, br, _label) in enumerate(repos):
        # Deploy workflows are often re-run by repository_dispatch on the same commit after a failed push run:
        # without those runs a recovered workflow would read "failing" until its next push.
        for ev in events:
            calls["runs:%d:%s" % (i, ev)] = (lambda repo=repo, br=br, ev=ev: _get(
                ctx, GH + "/repos/%s/actions/runs" % repo, h, {"branch": br, "event": ev, "per_page": 30}, "github "))
    res = _pmap(calls)
    errors = {k: e for k, (v, e) in res.items() if e}
    if len(errors) == len(calls):
        return _fail("; ".join("%s: %s" % kv for kv in sorted(errors.items())), "all GitHub calls failed")
    if gql_errs:
        errors["gql"] = gql_errs[0]
    gql = _d(res["gql"][0])
    stale_days = _num(cfg, "stale_days", 14)
    state, confirm = [], {}
    for i, (repo, br, _label) in enumerate(repos):
        got = [res["runs:%d:%s" % (i, ev)] for ev in events]
        runs = sorted([r for v, e in got for r in _dicts(_d(v).get("workflow_runs"))],
                      key=lambda r: _ts(r.get("updated_at")) or 0, reverse=True)
        state.append(list(_ci_state(runs, stale_days)) + [any(e is None for v, e in got)])
        ids = {}
        for r in runs:
            ids.setdefault(str(r.get("name") or "?"), r.get("workflow_id"))
        for name in state[-1][0][:4]:
            wid = ids.get(name)
            if isinstance(wid, int) and not isinstance(wid, bool):
                confirm["wf:%d:%s" % (i, name)] = (lambda repo=repo, br=br, wid=wid: _get(
                    ctx, GH + "/repos/%s/actions/workflows/%d/runs" % (repo, wid), h,
                    {"branch": br, "per_page": 20, "exclude_pull_requests": "true"}, "github "))
    # Each event's page spans a different stretch of time (a busy event's 30 runs reach back
    # less far), so the re-run that recovered a workflow can fall off its page while the old
    # failure is still on another. A workflow that looks red is confirmed from its own run list.
    for key, (v, e) in _pmap(confirm).items():
        if e is None:
            i, name = key.split(":", 2)[1:]
            own = sorted([r for r in _dicts(_d(v).get("workflow_runs")) if r.get("event") in events],
                          key=lambda r: _ts(r.get("updated_at")) or 0, reverse=True)
            bad2, stale2 = _ci_state(own, stale_days)[:2]
            st = state[int(i)]
            if own and name not in bad2:
                st[0] = [n for n in st[0] if n != name]
                st[1] += [n for n in stale2 if n == name and n not in st[1]]
    crit, warn, rows = [], [], []
    for i, (repo, br, label) in enumerate(repos):
        bad, stale, pending, newest, known = state[i]
        roll = _d(_d(_d(_d(gql.get("h%d" % i)).get("ref")).get("target")).get("statusCheckRollup")).get("state")
        lv = "crit" if bad or roll in ("FAILURE", "ERROR") else ("ok" if known or roll else "unknown")
        row = {"repo": repo, "label": label, "branch": br, "open_prs": _d(gql.get("p%d" % i)).get("issueCount"),
               "changes_requested": _d(gql.get("c%d" % i)).get("issueCount"), "head_rollup": roll,
               "ci_failing": bad, "ci_stale_failing": stale, "ci_pending": pending, "last_run": newest, "level": lv}
        rows.append(row)
        if bad:
            crit.append(("%s CI failing: %s" % (label, ", ".join(bad)), "gh run list -R %s -b %s" % (repo, br)))
        elif roll in ("FAILURE", "ERROR"):
            crit.append("%s HEAD checks %s" % (label, roll))
        if row["changes_requested"]:
            warn.append("%s: %d PR(s) need changes" % (label, row["changes_requested"]))
    d = {"token_source": src, "user": user or None, "repos": rows}
    if errors:
        d["partial_errors"] = errors
        warn.append("partial data (%s)" % ", ".join(sorted({k.split(":")[0] for k in errors})))
    status, alerts = _verdict("github", crit, warn)
    if crit or warn:
        summary = _msg((crit + warn)[0])
    else:
        total = sum(_int(r["open_prs"]) for r in rows)
        summary = "CI green x%d; %d open PRs, 0 need changes" % (len(rows), total)
    return _res(status, summary, d, None, alerts)


# ── Sentry: unresolved issues with events in the last 24h, per project ─────
SENTRY = "https://sentry.io"


def _sentry_ok(url, home) -> bool:
    """A URL the token may go to: https on the configured host, or on one of
    sentry.io's own region hosts when that host is sentry.io."""
    try:
        s = urllib.parse.urlsplit(url)
        port = s.port
    except ValueError:
        return False
    host = (s.hostname or "").lower()
    return s.scheme == "https" and port in (None, 443) and bool(host) and (
        host == home or (home == "sentry.io" and host.endswith(".sentry.io")))


@_saas("sentry", "sentry", ttl=900, timeout=20, needs=("SENTRY_AUTH_TOKEN",), defaults={
    "url": SENTRY, "orgs": [], "issues_warn": 1, "issues_crit": 10, "events_crit": 200})
def _sentry(ctx):
    """Sentry: unresolved issues with events in the last 24 hours"""
    tok = ctx.env.get("SENTRY_AUTH_TOKEN")
    if not tok:
        return _missing("SENTRY_AUTH_TOKEN")
    cfg, h = ctx.cfg, {"Authorization": "Bearer " + tok}
    base = str(cfg.get("url") or SENTRY).rstrip("/")
    home = (urllib.parse.urlsplit(base).hostname or "").lower()
    if not _sentry_ok(base, home):
        return _fail("[sources.sentry] url must be an https:// URL")
    try:
        orgs = _get(ctx, base + "/api/0/organizations/", h, None, "sentry ", expect=list)
    except ProbeError as e:
        return _fail(str(e))
    if not orgs:
        return _fail("no Sentry organizations visible to this token")
    want = _strs(cfg.get("orgs"))
    calls, slugs = {}, []
    for o in _dicts(orgs):
        slug = str(o.get("slug") or "")
        if not re.match(r"^[a-z0-9][a-z0-9_\-]*$", slug) or (want and slug not in want) or len(slugs) >= 5:
            continue
        region = str(_d(o.get("links")).get("regionUrl") or base).rstrip("/")
        if not _sentry_ok(region, home):
            continue  # never send the token to a host the API merely told us about
        root = "%s/api/0/organizations/%s" % (region, slug)
        slugs.append(slug)
        calls[slug + ":events"] = (lambda root=root: _get(ctx, root + "/events/", h, {
            "field": ["project", "count_unique(issue)", "count()"], "query": "is:unresolved", "statsPeriod": "24h",
            "dataset": "errors", "project": "-1", "per_page": 50, "sort": "-count_unique(issue)"}, "sentry "))
        calls[slug + ":projects"] = (lambda root=root: _get(ctx, root + "/projects/", h, {"per_page": 100}, "sentry ", expect=list))
    if not calls:
        return _fail("no matching Sentry organization" if want else "no Sentry organization on a Sentry host")
    res = _pmap(calls)
    issues = events = projects = 0
    per_project, seen = {}, False
    errors = {k: e for k, (v, e) in res.items() if e}
    for slug in slugs:
        ev, pr = res[slug + ":events"][0], res[slug + ":projects"][0]
        if isinstance(ev, dict) and isinstance(ev.get("data"), list):
            seen = True
            for row in _dicts(ev["data"]):
                n = _int(row.get("count_unique(issue)"))
                issues += n
                events += _int(row.get("count()"))
                per_project[str(row.get("project"))] = per_project.get(str(row.get("project")), 0) + n
        if isinstance(pr, list):
            projects += len(pr)
    if not seen:
        return _fail("; ".join("%s: %s" % kv for kv in sorted(errors.items())) or "no Sentry data", "Sentry query failed")
    d = {"orgs": slugs, "projects": projects, "unresolved_issues_24h": issues, "events_24h": events,
         "by_project": dict(sorted(per_project.items(), key=lambda kv: -kv[1])[:6])}
    if errors:
        d["partial_errors"] = errors
    iw, ic, ec = _num(cfg, "issues_warn", 1), _num(cfg, "issues_crit", 10), _num(cfg, "events_crit", 200)
    if issues < iw:
        return _res("ok", "%d unresolved issues in 24h (%d projects)" % (issues, projects), d)
    top = max(per_project.items(), key=lambda kv: kv[1])[0] if per_project else "?"
    summary = "%d unresolved issues, %d events/24h (%s)" % (issues, events, top)
    loud = issues >= ic or events >= ec
    status, alerts = _verdict("sentry", [summary] if loud else [], [] if loud else [summary])
    return _res(status, summary, d, None, alerts)

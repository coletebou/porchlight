"""Cloud panels: EDGE (uptime, sites, Cloudflare, Vercel), INFRA (Hetzner,
GitHub CI, Sentry, AWS), COMMS (Resend, Cloudflare Email Routing, Twilio) and
MONEY (Stripe, API spend, credits, run-rates, balances).

They read the records sources/saas.py writes (plus `sites` and `cursor_usage`)
and never touch the network. A service without credentials stores a "missing
env" soft failure, which shows nowhere; one that answered with a reason and no
data (a send-only key, an expired login) shows that reason, dimmed. Every field
is read through a typed accessor: a record kept from another version, or a
hand-edited cache, may hold anything, and must not cost the panel.
"""
from __future__ import annotations

from .. import core, theme as T
from ..ui import SEP, cells, kv, panel
from .ops import endpoint_cells


# ── Typed reads ─────────────────────────────────────────────────────────────
def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    """The dict items of a list field."""
    return [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []


def _s(v) -> list:
    """The text items of a list field."""
    return [str(i) for i in v if isinstance(i, (str, int, float)) and not isinstance(i, bool)] if isinstance(v, list) else []


def _n(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _lv(v, default="ok") -> str:
    return v if v in ("ok", "warn", "crit", "unknown") else default


def _rec(x, name):
    """A source's data worth showing, or None: never collected, or no credentials here."""
    d = x.data(name)
    if not isinstance(d, dict) or str(d.get("error") or "").startswith("missing env"):
        return None
    return d if d.get("details") or d.get("summary") else None


def _det(d) -> dict:
    return _d(_d(d).get("details"))


def _st(d) -> str:
    s = _d(d).get("status")
    return s if s in ("ok", "warn", "crit") else "unknown"


def _up(lvl, level) -> str:
    """The panel's level: unknown is "no reading", not an alarm."""
    return T.worst(lvl, level if level in ("warn", "crit") else "ok")


def _quiet(d) -> str:
    """A record with nothing to show but its reason."""
    return T.dot("unknown") + " " + T.dim(str(d.get("summary") or d.get("error") or "no data"))


def _note(x, names) -> str:
    """The first contributing source that is stale or failing, named when the panel mixes
    several. A record shown by its reason alone (a send-only key) has already said why."""
    def reason_only(n):
        d = x.data(n)
        return isinstance(d, dict) and _st(d) == "unknown" and bool(d.get("error"))
    names = [n for n in dict.fromkeys(names) if not reason_only(n)]
    for n in names:
        s = x.stale_note(n, 4, 1800)
        if s:
            return s if len(names) == 1 else "%s %s" % (n, s)
    return ""


def _money(n, cur="usd") -> str:
    cur = str(cur or "usd").lower()
    sym = "$" if cur == "usd" else (None if T.ASCII else {"eur": "€", "gbp": "£"}.get(cur))
    return core.money(n, sym) if sym else (core.money(n, "") + " " + cur.upper())


def _rows(label, lines, w) -> list:
    return [kv(label if i == 0 else "", ln, w) for i, ln in enumerate(lines)]


def _sites(si) -> list:
    """The `sites` rows, normalized for endpoint_cells (which reads them unguarded)."""
    return [dict(r, label=str(r["label"]), ms=_n(r.get("ms")), info=str(r["info"]) if r.get("info") else None)
            for r in _l(_d(si).get("rows")) if r.get("label")]


# ── EDGE ────────────────────────────────────────────────────────────────────
@panel("edge", title="EDGE")
def p_edge(x):
    rows, lvl, used, W = [], "ok", [], 9
    ur = _rec(x, "uptimerobot")
    if ur:
        used.append("uptimerobot")
        det = _det(ur)
        if "total" in det:
            lvl = _up(lvl, _st(ur))
            txt = T.dot(_st(ur)) + " %d/%d monitors up" % (_n(det.get("up")), _n(det.get("total")))
            for key in ("paused", "ignored"):
                if _n(det.get(key)):
                    txt += SEP + T.dim("%d %s" % (_n(det[key]), key))
            down = _l(det.get("down"))
            if down:
                txt += SEP + T.c(", ".join(str(m.get("name"))[:24] + ("" if _n(m.get("down_days"), None) is None
                                                                       else " %.0fd" % m["down_days"]) for m in down[:3]), "red")
            rows.append(kv("uptime", txt, W))
        else:
            rows.append(kv("uptime", _quiet(ur), W))
    si = x.data("sites")
    sites = _sites(si)
    if sites:
        used.append("sites")
        rows += _rows("sites", endpoint_cells(sites, x.cw - 1 - W), W)
        if any(not r.get("up") for r in sites):
            lvl = T.worst(lvl, "crit" if _d(si).get("alerts") else "warn")
    cf = _rec(x, "cloudflare")
    if cf:
        used.append("cloudflare")
        det = _det(cf)
        tu, tr = _d(det.get("tunnels")), _d(det.get("traffic_24h"))
        if tu:
            down, deg, up = _s(tu.get("down_recent")), _n(tu.get("degraded")), _n(tu.get("healthy"))
            l2 = "crit" if down else "warn" if deg else "ok"
            lvl = _up(lvl, l2)
            txt = T.dot(l2) + " %d/%d up" % (up, up + deg + len(down))
            if down:
                txt += SEP + T.c("down: " + ", ".join(down[:3]), "red")
            if deg:
                txt += SEP + T.c("%d degraded" % deg, "amber")
            if _n(tu.get("stale_down")):
                txt += SEP + T.dim("%d retired" % _n(tu["stale_down"]))
            rows.append(kv("tunnels", txt, W))
        if tr:
            flagged = [z for z in _l(det.get("traffic_zones")) if z.get("level") in ("warn", "crit")]
            l5 = T.worst("ok", *[z["level"] for z in flagged])
            lvl = _up(lvl, l5)
            bits = ["%s req" % core.human_num(_n(tr.get("requests"))),
                    T.sev("%s 5xx (%.1f%%)" % (core.human_num(_n(tr.get("http_5xx"))), _n(tr.get("pct_5xx"))), l5),
                    "%s threats" % core.human_num(_n(tr.get("threats"))),
                    T.dim("%s uniq" % core.human_num(_n(tr.get("uniques"))))]
            if _n(tr.get("requests")):
                bits.append(T.dim("%d%% cached" % round(_n(tr.get("cached_pct")))))
            rows.append(kv("cf 24h", SEP.join(bits), W))
            for z in flagged[:3]:
                rows.append(kv("", T.sev("%s %s" % (T.g(z["level"]), z.get("zone")), z["level"]) +
                               T.dim(" %.1f%% 5xx of %s req" % (_n(z.get("pct_5xx")), core.human_num(_n(z.get("requests"))))), W))
        bad = _s(_d(det.get("zones")).get("not_active"))
        if bad:
            lvl = _up(lvl, "warn")
            rows.append(kv("zones", T.dot("warn") + " not active: " + ", ".join(bad[:4]), W))
        if not tu and not tr and not bad and not det.get("email_routing"):
            rows.append(kv("cf", _quiet(cf), W))
    ve = _rec(x, "vercel")
    if ve:
        used.append("vercel")
        det = _det(ve)
        if "projects" in det:
            l2 = _st(ve)
            lvl = _up(lvl, l2)
            failing = _s(det.get("failing_24h")) + _s(det.get("failing_7d"))
            txt = T.dot(l2) + " %d ready" % _n(_d(det.get("prod_states")).get("READY"))
            if _s(det.get("building")):
                txt += SEP + T.c("%d building" % len(_s(det["building"])), "sky")
            if failing:
                txt += SEP + T.c("failing: " + ", ".join(failing[:3]), "red" if _s(det.get("failing_24h")) else "amber")
            if _s(det.get("stale_errors")):
                txt += SEP + T.dim("old error: " + ", ".join(_s(det["stale_errors"])[:2]))
            rows.append(kv("vercel", txt + SEP + T.dim("%d projects" % _n(det.get("projects"))), W))
        else:
            rows.append(kv("vercel", _quiet(ve), W))
    if not rows:
        return None
    return x.panel("edge", rows, lvl, _note(x, used))


# ── INFRA ───────────────────────────────────────────────────────────────────
@panel("infra", title="INFRA")
def p_infra(x):
    rows, lvl, used, W = [], "ok", [], 9
    hz = _rec(x, "hetzner")
    if hz:
        used.append("hetzner")
        det = _det(hz)
        items = []
        for s in _l(det.get("servers")):
            l2 = _lv(s.get("level"), "ok" if s.get("status") == "running" else "crit")
            lvl = _up(lvl, l2)
            extra = [str(s.get("type") or "")]
            if s.get("status") != "running":
                extra.append(str(s.get("status")))
            if _n(s.get("traffic_out_pct"), None) is not None:
                extra.append("%.0f%% out" % s["traffic_out_pct"])
            items.append(T.dot(l2) + " " + str(s.get("name")) + T.dim(" " + " ".join(e for e in extra if e)))
        rows += _rows("hetzner", cells(items, x.cw - 1 - W, 16), W)
        bw = max(6, min(16, x.cw - 1 - W - 34))
        boxes = _l(det.get("storage_boxes"))
        for b in boxes[:3]:
            p, l2 = _n(b.get("used_pct")), _lv(b.get("level"))
            lvl = _up(lvl, l2)
            rows.append(kv("box", T.dot(l2) + " " + T.pad(T.fit(str(b.get("name")), 12), 12) + " " + T.bar(p, bw, 80, 90) +
                           " " + T.sev("%3d%%" % round(p), l2) +
                           T.dim(" %.2f/%.1f TiB" % (_n(b.get("used_tib")), _n(b.get("quota_tib")))), W))
        if not items and not boxes:
            rows.append(kv("hetzner", _quiet(hz), W))
    gh = _rec(x, "github")
    if gh:
        used.append("github")
        repos = _l(_det(gh).get("repos"))
        items = []
        for r in repos:
            l2 = _lv(r.get("level"), "unknown")
            lvl = _up(lvl, l2)
            fail = _s(r.get("ci_failing"))
            if fail:
                tail = T.c(" %s %s" % (T.g("crit"), ", ".join(fail)[:24]), "red")
            elif r.get("head_rollup") in ("FAILURE", "ERROR"):
                tail = T.c(" HEAD %s" % r["head_rollup"].lower(), "red")
            elif _n(r.get("ci_pending")):
                tail = T.c(" " + T.g("work"), "sky")
            else:
                tail = T.dim(" " + T.g("ok")) if l2 == "ok" else ""
            items.append(T.dot(l2) + " " + str(r.get("label") or r.get("repo")) + tail)
        rows += _rows("github", cells(items, x.cw - 1 - W, 14), W)
        if repos:
            prs, cr = sum(_n(r.get("open_prs")) for r in repos), sum(_n(r.get("changes_requested")) for r in repos)
            who = _det(gh).get("user")
            bits = ["%d open PR%s" % (prs, "" if prs == 1 else "s") + (T.dim(" by " + who) if isinstance(who, str) and who else "")]
            if cr:
                lvl = _up(lvl, "warn")
                bits.append(T.c("%d changes requested" % cr, "amber"))
            running = sum(_n(r.get("ci_pending")) for r in repos)
            if running:
                bits.append(T.c("%d running" % running, "sky"))
            rows.append(kv("", SEP.join(bits), W))
        else:
            rows.append(kv("github", _quiet(gh), W))
    se = _rec(x, "sentry")
    if se:
        used.append("sentry")
        det = _det(se)
        if "unresolved_issues_24h" in det:
            l2 = _st(se)
            lvl = _up(lvl, l2)
            top = sorted(((str(k), _n(v)) for k, v in _d(det.get("by_project")).items()), key=lambda kv_: -kv_[1])[:2]
            top = [t for t in top if t[1]]
            rows.append(kv("sentry", T.dot(l2) + " %d unresolved" % _n(det["unresolved_issues_24h"]) + SEP +
                           "%s events 24h" % core.human_num(_n(det.get("events_24h"))) +
                           (T.dim(" (" + ", ".join("%s %d" % t for t in top) + ")") if top else ""), W))
        else:
            rows.append(kv("sentry", _quiet(se), W))
    aw = _rec(x, "aws_account")
    if aw:
        used.append("aws_account")
        det = _det(aw)
        accts = _l(det.get("accounts"))
        for i, a in enumerate(accts[:3]):
            ec2 = {str(r): v for r, v in _d(a.get("ec2_running")).items() if isinstance(v, dict)}
            n = sum(_n(v.get("count")) for v in ec2.values())
            errs = [r for r, v in ec2.items() if v.get("error")]
            l2 = "warn" if errs else "ok"
            lvl = _up(lvl, l2)
            txt = T.dot(l2) + " " + str(a.get("profile")) + T.dim(" %s%s" % (T.ellipsis(), a.get("account_last4"))) + SEP + \
                "%d EC2 running" % n
            if len(ec2) > 1:
                txt += T.dim(" (%s)" % ", ".join("%s %d" % (r, _n(v.get("count"))) for r, v in ec2.items() if not v.get("error")))
            if errs:
                txt += SEP + T.c("%s %s" % (errs[0], ec2[errs[0]]["error"]), "amber")
            rows.append(kv("aws" if i == 0 else "", txt, W))
        expired = [str(p) for p, e in _d(det.get("profile_errors")).items() if e in ("sso-expired", "credentials-expired")]
        if accts and expired:
            rows.append(kv("", T.dim("%s: login expired" % ", ".join(expired[:3])), W))
        if not accts:
            lvl = _up(lvl, _st(aw))
            rows.append(kv("aws", (T.dot(_st(aw)) + " " + str(aw.get("summary"))) if _st(aw) != "unknown" else _quiet(aw), W))
    if not rows:
        return None
    return x.panel("infra", rows, lvl, _note(x, used))


# ── COMMS ───────────────────────────────────────────────────────────────────
@panel("comms", title="COMMS")
def p_comms(x):
    rows, lvl, used, W = [], "ok", [], 8
    rs = _rec(x, "resend")
    if rs:
        used.append("resend")
        det = _det(rs)
        if det.get("can_read"):
            l2 = _st(rs)
            lvl = _up(lvl, l2)
            doms, em = _l(det.get("domains")), _d(det.get("emails_24h"))
            txt = T.dot(l2) + " %d/%d domains verified" % (sum(1 for d in doms if d.get("status") == "verified"), len(doms))
            if em:
                txt += SEP + "%d%s emails 24h" % (_n(em.get("count")), "+" if em.get("truncated") else "")
                bad = sum(_n(_d(em.get("by_last_event")).get(k)) for k in ("bounced", "complained", "failed"))
                if bad:
                    txt += SEP + T.c("%d bounced" % bad, "amber")
            rows.append(kv("resend", txt, W))
            for d in [d for d in doms if d.get("status") != "verified"][:3]:
                l3 = "crit" if d.get("status") == "failed" else "warn"
                rows.append(kv("", T.sev("%s %s %s" % (T.g(l3), d.get("name"), d.get("status")), l3), W))
        else:
            rows.append(kv("resend", _quiet(rs), W))
    er = _d(_det(_rec(x, "cloudflare")).get("email_routing"))
    if er:  # inbound mail lives here, beside outbound (Resend), not on the EDGE panel
        used.append("cloudflare")
        zones = _l(er.get("zones"))
        broken = [z for z in zones if z.get("level") in ("warn", "crit")]
        l2 = "warn" if broken else "ok"
        lvl = _up(lvl, l2)
        last = {str(k): _n(v) for k, v in _d(er.get("last_24h")).items()}
        if len(zones) == 1:
            txt = T.dot(l2) + " routing %s" % zones[0].get("status")
        else:
            txt = T.dot(l2) + " routing %d/%d ready" % (_n(er.get("ready")), len(zones))
        tail = ["%d delivered" % last.get("delivered", 0)] + [
            T.c("%d %s" % (n, k), "amber" if k in ("rejected", "error", "failed") else "fg")
            for k, n in sorted(last.items()) if k != "delivered" and n]
        rows.append(kv("email", txt + SEP + T.dim("24h: ") + ", ".join(tail), W))
        for z in broken[:3]:
            rows.append(kv("", T.c("%s %s %s" % (T.g("warn"), z.get("zone"), z.get("status") if z.get("enabled") else "off"), "amber"), W))
    tw = _rec(x, "twilio")
    if tw:
        used.append("twilio")
        det = _det(tw)
        if "sms_today" in det or "balance" in det:
            l2 = _st(tw)
            lvl = _up(lvl, l2)
            bits = ["%d SMS today" % _n(det["sms_today"])] if "sms_today" in det else []
            if det.get("partial_errors"):
                bits.append(T.c("partial data", "amber"))
            rows.append(kv("twilio", T.dot(l2) + " " + (SEP.join(bits) or T.dim("balance only (see MONEY)")), W))
        else:
            rows.append(kv("twilio", _quiet(tw), W))
    if not rows:
        return None
    return x.panel("comms", rows, lvl, _note(x, used))


# ── MONEY ───────────────────────────────────────────────────────────────────
@panel("money", title="MONEY")
def p_money(x):
    rows, lvl, used, W = [], "ok", [], 11
    st = _rec(x, "stripe")
    if st:
        used.append("stripe")
        det = _det(st)
        avail, subs = {str(k): _n(v) for k, v in _d(det.get("available")).items()}, _d(det.get("subscriptions"))
        if avail or subs:
            l2 = _st(st)
            lvl = _up(lvl, l2)
            cur = "usd" if "usd" in avail else (next(iter(avail)) if avail else "usd")
            bits = [T.c(_money(avail.get(cur, 0), cur), "fg", bold=True) + T.dim(" available")]
            if subs:
                by = _d(subs.get("by_status"))
                bits.append("%d active%s" % (_n(by.get("active")), "+" if subs.get("list_truncated") else ""))
                mrr = _n(_d(subs.get("mrr_est")).get(cur), None)
                if mrr is not None:
                    bits.append("MRR " + _money(mrr, cur))
                due = _n(by.get("past_due")) + _n(by.get("unpaid"))
                if due:
                    bits.append(T.c("%d past due" % due, "red"))
            if str(det.get("key_mode") or "").endswith("_test"):
                bits.append(T.c("test mode", "amber"))
            rows.append(kv("stripe", T.dot(l2) + " " + SEP.join(bits), W))
        else:
            rows.append(kv("stripe", _quiet(st), W))
    sp = _rec(x, "ai_spend")
    if sp:
        used.append("ai_spend")
        det = _det(sp)
        mtd = [(n, _n(_det(det.get(n)).get("mtd_usd"), None)) for n in ("anthropic", "openai")]
        mtd = [(n, v) for n, v in mtd if v is not None]
        if mtd:
            rows.append(kv("api mtd", SEP.join("%s %s" % (n, T.c(_money(v), "fg")) for n, v in mtd) +
                           (T.dim(" = %s" % _money(sum(v for _n_, v in mtd))) if len(mtd) > 1 else ""), W))
        orr = _d(det.get("openrouter"))
        od = _det(orr)
        left = _n(od.get("remaining_usd"), None)
        if left is not None:
            l2 = _lv(orr.get("status"))
            l2 = l2 if l2 != "unknown" else "ok"
            lvl = _up(lvl, l2)
            rows.append(kv("openrouter", T.dot(l2) + " " + T.sev("%s left" % _money(left), l2) +
                           T.dim(" of %s (%.0f%%)" % (_money(_n(od.get("credits_total_usd"))), _n(od.get("remaining_pct")))), W))
        xa = _d(det.get("xai"))
        if xa.get("status") == "ok" and _det(xa).get("prepaid_total_raw") is not None:
            rows.append(kv("xai", T.dot("ok") + " prepaid " + str(_det(xa)["prepaid_total_raw"])[:16] + T.dim(" (API units)"), W))
        elif xa:
            rows.append(kv("xai", T.dot("unknown") + " " + T.dim(str(xa.get("summary") or "no reading")), W))
        for n in ("anthropic", "openai", "openrouter"):  # a provider whose key failed: say so instead of a silent gap
            p = _d(det.get(n))
            if p.get("status") == "unknown" and p.get("error"):
                rows.append(kv(n if len(n) < W else n[:W - 1], T.dot("unknown") + " " + T.dim(str(p["error"])[:60]), W))
    est = _n(_det(_rec(x, "hetzner")).get("est_monthly"), None)
    if est is not None:
        used.append("hetzner")
        rows.append(kv("hetzner", "~%s/mo" % _money(est, _det(_rec(x, "hetzner")).get("currency")) + T.dim(" price-list run-rate"), W))
    ce = [a for a in _l(_det(_rec(x, "aws_account")).get("accounts")) if _n(a.get("mtd_unblended_usd"), None) is not None]
    if ce:  # Cost Explorer, when opted in
        used.append("aws_account")
        rows += _rows("aws", [_money(a["mtd_unblended_usd"]) + T.dim(" month to date") + SEP + T.dim(str(a.get("profile")))
                              for a in ce[:3]], W)
    od_used = _n(_d(_d(x.data("cursor_usage")).get("credits")).get("on_demand_used_usd"))
    if od_used:
        used.append("cursor_usage")
        rows.append(kv("cursor", T.c(_money(od_used), "amber") + T.dim(" on-demand this cycle"), W))
    bal = _d(_det(_rec(x, "twilio")).get("balance"))
    if _n(bal.get("amount"), None) is not None:
        used.append("twilio")
        l2 = _lv(bal.get("level"))
        lvl = _up(lvl, l2)
        rows.append(kv("twilio", T.dot(l2) + " " + T.sev(_money(bal["amount"], bal.get("currency")), l2) + T.dim(" balance"), W))
    if not rows:
        return None
    return x.panel("money", rows, lvl, _note(x, used))

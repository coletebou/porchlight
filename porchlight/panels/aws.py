"""The AWS panel: the month's spend against the budget, the daily trend, what burns per
hour now, the spend by service, every running instance at its price, and what stopped
boxes still cost.

It reads the aws_account record (hourly, free calls) and the aws_cost record (Cost
Explorer, at most daily); the forecast is computed here from both, and MONEY's AWS
line uses the same month_view(). Nothing here touches the network. Rows are built to
fit the column: a list says how many it left out ("+2") instead of cutting a word.
"""
from __future__ import annotations

import calendar
import time

from .. import core, theme as T
from ..ui import SEP, Panel, fitted, kv, panel, wrap

W = 9
SHOWN_RUNNING = 5
LIVE = ("running", "pending")


# ── Typed reads: a record kept from another version may hold anything ───────
def _d(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []


def _n(v, default=0.0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _s(v, n: int = 40) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _names(v) -> list:
    return [i for i in v if isinstance(i, str)] if isinstance(v, list) else []


def _pairs(v) -> list:
    """[name, number] rows of a list field (Cost Explorer's days and services)."""
    return [(str(p[0]), _n(p[1])) for p in v if isinstance(p, list) and len(p) == 2] if isinstance(v, list) else []


def account(x) -> dict:
    """The aws_account details worth showing, or {}."""
    d = x.data("aws_account")
    return _d(_d(d).get("details")) if isinstance(d, dict) else {}


def cost(x) -> dict:
    """The aws_cost record, when Cost Explorer is on for this machine's config, or {}."""
    from ..sources import aws_cost
    if aws_cost.mode(x.host) != "daily":
        return {}
    c = x.data("aws_cost")
    return c if isinstance(c, dict) and not c.get("off") else {}


def _month_end(now: float) -> float:
    y, m = time.gmtime(now)[:2]
    return float(calendar.timegm((y + (m == 12), m % 12 + 1, 1, 0, 0, 0)))


def per_day(a, c) -> float:
    """The account's spend per day from now on. Per bucket: the hourly model's rate (it moves
    the hour a box stops) or, where the model covers only part of a bucket (EC2-Other also
    holds snapshots, NAT gateways and transfer; VPC more than public addresses), the larger
    of it and Cost Explorer's average over the last full week; plus everything else."""
    burn, pd = _d(a.get("burn")), _d(c.get("per_day"))
    ec2 = (_n(burn.get("compute_h")) + _n(burn.get("host_h"))) * 24 if burn else _n(pd.get("ec2"))
    return ec2 + max(_n(burn.get("disk_h")) * 24, _n(pd.get("ec2_other"))) + \
        max(_n(burn.get("ipv4_h")) * 24, _n(pd.get("vpc"))) + _n(pd.get("other"))


def month_view(x):
    """{"mtd", "month", "budget", "forecast", "end"} or None. MTD is the account budget's
    spend when there is one (AWS refreshes it a few times a day; aws_account reads it
    hourly), else Cost Explorer's for this month. Forecast: MTD plus per_day() carried to
    the end of the month."""
    from ..sources.aws import account_budget
    a, c = account(x), cost(x)
    b = account_budget(_l(a.get("budgets")))
    now = core.now()
    if b:
        mtd = b["actual"]
    elif _n(c.get("mtd"), None) is not None and c.get("month") == time.strftime("%Y-%m", time.gmtime(now)):
        mtd = c["mtd"]
    else:
        return None
    end = _month_end(now)
    return {"mtd": mtd, "month": time.strftime("%b", time.gmtime(now)), "budget": b,
            "forecast": mtd + per_day(a, c) * max(0.0, end - now) / 86400, "end": time.strftime("%b %d", time.gmtime(end - 1))}


def budget_text(b, mtd) -> str:
    if not b or not b.get("limit"):
        return ""
    p = 100.0 * mtd / b["limit"]
    return T.sev("%d%% of %s budget" % (round(p), core.money(b["limit"])), T.level_for(p, 80, 100))


def month_line(mv, room: int, burn=None) -> str:
    """'$212 Oct · 85% of $250 budget · ≈$262 by Oct 31', shortened to fit `room` by dropping
    its least important parts (never cutting one)."""
    head = T.c(core.money(mv["mtd"]), "fg", bold=True) + T.dim(" " + mv["month"])
    bt = budget_text(mv["budget"], mv["mtd"])
    fc = T.dim("%s%s by %s" % (T.g("approx"), core.money(mv["forecast"]), mv["end"]))
    now = T.dim("%s/h now" % core.money(burn)) if burn else ""
    for items in ([head, bt, fc, now], [head, bt, fc], [head, bt, T.dim("%s%s" % (T.g("approx"), core.money(mv["forecast"])))],
                  [head, bt], [head]):
        line = SEP.join(i for i in items if i)
        if T.width(line) <= room:
            return line
    return head


def _line(text: str, room: int) -> str:
    """One line of a message, cut between words; cut inside one only when its first word
    alone is wider than the line (a path, a URL), since the reason still matters."""
    return (wrap(text, [room]) or [T.fit(text, room)])[0]


def _short_type(t: str) -> str:
    return (t or "?").replace("xlarge", "xl").replace("large", "l").replace("medium", "m")


def _ago(t) -> str:
    if not _n(t):
        return "?"
    s = max(0, core.now() - t)
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def _daily(c, room) -> str:
    """A spark of the full days (as many as fit, two weeks at most), the latest of them and
    the week's average. The read's own last day was partial when it was read, so it never
    counts, however old the read is; the latest full day is named by its date unless it
    was yesterday."""
    now = core.now()
    cut = min(time.strftime("%Y-%m-%d", time.gmtime(now)), _s(c.get("through"), 10) or "9999")
    pairs = [(d, v) for d, v in _pairs(c.get("days")) if d < cut][-14:]
    if not pairs:
        return ""
    days, (last, usd) = [v for _, v in pairs], pairs[-1]
    week = days[-7:]
    label = "yesterday"
    if last != time.strftime("%Y-%m-%d", time.gmtime(now - 86400)):
        try:
            label = time.strftime("%b %d", time.strptime(last, "%Y-%m-%d"))
        except ValueError:
            label = last[:10]
    items = ["%s %s" % (label, core.money(usd)), T.dim("%dd avg %s" % (len(week), core.money(sum(week) / len(week))))]
    age = core.now() - _n(c.get("as_of"))
    if age > 30 * 3600:
        items.append(T.c("%s old" % core.human_age(age), "amber"))
    if c.get("partial"):
        items.append(T.dim("partial"))
    text = fitted(items, room - 10)
    n = max(7, min(len(days), room - T.width(text) - T.width(str(SEP))))
    return T.c(T.spark(days[-n:]), "sky") + SEP + fitted(items, room - n - T.width(str(SEP)))


def _burn(b, room) -> str:
    head = T.c("%s/h" % core.money(_n(b.get("total_h"))), "fg", bold=True) + \
        T.dim(" %s$%.0f/day" % (T.g("approx"), _n(b.get("total_h")) * 24))
    parts = [T.dim("ec2 %s" % core.money(_n(b.get("compute_h")))), T.dim("ebs %s" % core.money(_n(b.get("disk_h")))),
             T.dim("ip %s" % core.money(_n(b.get("ipv4_h"))))]
    if _n(b.get("host_h")):
        parts.append(T.dim("hosts %s" % core.money(b["host_h"])))
    unpriced = b.get("unpriced") if isinstance(b.get("unpriced"), list) else []
    if unpriced:
        parts.append(T.c("%d unpriced" % len(unpriced), "amber"))
    return fitted([head] + parts, room)


def _services(c, room) -> str:
    svc = _pairs(c.get("services"))
    if not svc:
        return ""
    items = ["%s %s" % (n, core.money(v)) for n, v in svc]
    return fitted(items, room, more=lambda k: T.dim("+%d %s" % (k, core.money(sum(v for _, v in svc[len(svc) - k:])))))


def _running(insts, home, room) -> list:
    live = [i for i in insts if i.get("state") in LIVE]
    shown = live[:SHOWN_RUNNING]
    if not shown:
        return []
    types = [_short_type(_s(i.get("type"), 24)) for i in shown]
    tw = min(12, max(T.width(t) for t in types))
    fixed = 2 + 1 + tw + 1 + 4 + 1 + 8  # glyph, type, age, price ("$12.34/h")
    nw = max(6, min(max(T.width(_s(i.get("name"), 40)) for i in shown), room - fixed))
    rows = []
    for n, (i, ty) in enumerate(zip(shown, types)):
        price = i.get("usd_h")
        if i.get("price") == "host":
            cell = T.dim("on host")
        elif isinstance(price, (int, float)) and not isinstance(price, bool):
            cell = T.c("%s/h" % core.money(price), "fg")
        else:
            cell = T.dim("?/h")
        line = T.c(T.g("work"), "green") + " " + T.pad(_s(i.get("name"), 40) or "?", nw) + " " + T.pad(T.dim(ty), tw) + " " + \
            T.pad(_ago(i.get("since")), 4, ">") + " " + cell
        for extra in ([T.c("spot", "violet")] if i.get("spot") else []) + \
                ([T.c(_s(i.get("region"), 20), "amber")] if home and _s(i.get("region"), 20) not in home else []):
            if T.width(line) + 1 + T.width(extra) <= room:
                line += " " + extra
        rows.append(kv("running" if n == 0 else "", line, W))
    if len(live) > SHOWN_RUNNING:
        more = live[SHOWN_RUNNING:]
        usd = sum(_n(i.get("usd_h")) for i in more)
        rows.append(kv("", T.dim("+%d more running%s" % (len(more), " · %s/h" % core.money(usd) if usd else "")), W))
    return rows


def _hosts(hosts, room) -> list:
    rows = []
    for h in hosts[:3]:
        n = int(_n(h.get("instances")))
        items = [T.c(T.g("bullet"), "violet") + " %s %s" % (_s(h.get("family"), 24) or "?", _s(h.get("region"), 20)),
                 T.dim("%s old" % _ago(h.get("since"))) if _n(h.get("since")) else "",
                 (T.dim("%d instance%s" % (n, "" if n == 1 else "s")) if n else T.c("no instance", "amber")),
                 "%s/h" % core.money(h["usd_h"]) if _n(h.get("usd_h")) else ""]
        rows.append(kv("host", fitted(items, room), W))
    return rows


def _stopped(insts, idle, room) -> list:
    stopped = [i for i in insts if i.get("state") not in LIVE]
    usd = _n(idle.get("usd_mo"))
    if usd >= 0.5:
        head = ("%d %s " % (len(stopped), T.g("sep")) if stopped else "") + T.c("$%.0f/mo idle" % usd, "amber") + ": "
        items = ["%s $%.0f" % (_s(it.get("name"), 28), _n(it.get("usd_mo"))) for it in _l(idle.get("items"))
                 if _n(it.get("usd_mo")) >= 0.5]
        return [kv("stopped", head + T.dim(fitted(items, room - T.width(head))), W)]
    if stopped:
        lead = "%d stopped: " % len(stopped)
        return [kv("stopped", T.dim(lead + fitted([_s(i.get("name"), 28) for i in stopped], room - len(lead), ", ")), W)]
    return []


def _partial(regions, room) -> list:
    errs = {str(r): _s(e, 120) for r, e in _d(regions.get("errors")).items()}
    if not errs:
        return []
    full = set(_names(regions.get("home")) + _names(regions.get("active")))
    mine = sorted(r for r in errs if r in full)
    rows = []
    if mine:
        r = mine[0]
        line = _line("%s: %s" % (r, errs[r]), room - (4 if len(mine) > 1 else 0))
        rows.append(kv("partial", T.c(line, "amber") + (T.dim(" +%d" % (len(mine) - 1)) if len(mine) > 1 else ""), W))
    swept = len(errs) - len(mine)
    if swept:
        rows.append(kv("" if mine else "partial", T.dim("%d swept region%s unreadable" % (swept, "" if swept == 1 else "s")), W))
    return rows


@panel("aws", title="AWS")
def p_aws(x):
    rec = x.data("aws_account")
    a, c = account(x), cost(x)
    room = x.cw - 1 - W
    inventory = isinstance(a.get("instances"), list)
    if not inventory and not (c.get("days") or c.get("ce_error")):
        # Nothing collected yet: say why only when it is news (an expired login) or AWS was asked for here.
        err = _s(_d(rec).get("error"), 200) if isinstance(rec, dict) else ""
        if not err or ("sso-expired" not in err and not x.cfg.source_configured("aws_account")):
            return None
        lines = wrap(err, [x.cw - 3, x.cw - 3]) or [_line(err, x.cw - 3)]
        return Panel("aws", "AWS", [T.dot("unknown") + " " + T.dim(lines[0])] + ["  " + T.dim(ln) for ln in lines[1:]],
                     "unknown", x.stale_note("aws_account", 4, 3 * 3600))
    rows, lvl = [], "ok"
    err = x.cache.error("aws_account")
    if err:  # the last good reading is kept; say why it is old
        lines = wrap(str(err), [x.cw - 3, x.cw - 3]) or [_line(str(err), x.cw - 3)]
        rows += [T.c(T.g("crit") + " ", "red") + T.dim(lines[0])] + ["  " + T.dim(ln) for ln in lines[1:]]
    mv = month_view(x)
    if mv:
        if mv["budget"] and mv["mtd"] > mv["budget"]["limit"]:
            lvl = "warn"
        rows.append(kv("month", month_line(mv, room), W))
    elif _s(a.get("budget_error"), 80):  # no month to show, and the budgets could not be read: say why
        rows.append(kv("month", T.dim(_line("budgets: " + _s(a["budget_error"], 80), room)), W))
    if _s(a.get("budget_note"), 80) and not (mv and mv["budget"]):  # a CLI that cannot read budget filters
        rows.append(kv("budget", T.c(_line(_s(a["budget_note"], 80), room), "amber"), W))
    if c.get("ce_error"):
        rows.append(kv("daily", T.dim(_line("Cost Explorer: " + _s(c["ce_error"], 160), room)), W))
    else:
        daily = _daily(c, room)
        if daily:
            rows.append(kv("daily", daily, W))
    b = _d(a.get("burn"))
    if b:
        rows.append(kv("burn", _burn(b, room), W))
    svc = _services(c, room)
    if svc:
        rows.append(kv("services", svc, W))
    regions = _d(a.get("regions"))
    home = set(_names(regions.get("home")))
    insts = _l(a.get("instances"))
    rows += _running(insts, home, room) + _hosts(_l(a.get("hosts")), room) + _stopped(insts, _d(a.get("idle")), room)
    rows += _partial(regions, room)
    if isinstance(rec, dict) and rec.get("alerts"):
        lvl = T.worst(lvl, "warn")
    if not rows:
        rows = [T.dot("unknown") + T.dim(" no instances, volumes or hosts")]
    note = x.stale_note("aws_account", 4, 3 * 3600)
    if not note and _s(a.get("account_last4"), 4):
        prof = _s(a.get("profile"), 24)
        note = ((prof + " ") if prof and len(prof) <= 16 else "") + T.ellipsis() + _s(a["account_last4"], 4)
    return Panel("aws", "AWS", rows, lvl, note)

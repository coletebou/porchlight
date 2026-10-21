"""AI LIMITS: plan windows for Claude, Codex, Cursor and Grok, plus a Langfuse line.

One row per provider: its plan, the two main windows as meters, the soonest
reset. A dim line below holds the extras: per-model weekly windows, credit
balances, a Cursor cycle's included, bonus and on-demand dollars. A provider
with no reading gets a one-line hint instead ("login expired · run `grok
login`"); `quiet_errors = true` under [sources.<name>] hides it on machines
where no subscription login is expected.
"""
from __future__ import annotations

import re
import time

from .. import core, registry, theme as T
from ..ui import kv, panel, until

PROVIDERS = (("claude", "claude_usage"), ("codex", "codex_usage"), ("cursor", "cursor_usage"), ("grok", "grok_usage"))
CLI = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent", "grok": "grok"}
LOGIN = {"claude": "claude auth login", "codex": "codex login", "cursor": "cursor-agent login", "grok": "grok login"}
HEAD = 15     # "claude max20x" and a gap
RESET_W = 11  # " ↻Tue 02:36", the widest reset note
# The probe's Keychain problems, which outlive its detail in a failed run's error text.
KEYCHAIN = re.compile(r"Keychain (?:not readable in this session|read timed out|access was refused)")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _f(v, default) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def plan_label(name, plan, width=None) -> str:
    """'max (default_claude_max_20x)' → 'max20x'; otherwise the plan's first word, cut to
    the room the provider's name leaves in the head column."""
    s = str(plan or "")
    m = re.search(r"max_(\d+x)", s)
    if name == "claude" and m:
        return "max" + m.group(1)
    return s.split(" (")[0][:HEAD - 2 - len(name) if width is None else width]


def gone(w, now) -> bool:
    """No current reading for a window: none was reported, or it has reset since."""
    t = _num(w.get("resets_at"))
    return _num(w.get("used_pct")) is None or bool(t and t <= now)


def meter(w, bw, warn, crit, now) -> str:
    label = T.dim(str(w.get("label") or "?"))
    if gone(w, now):
        return label + " " + T.bar(0, bw) + " " + T.dim("   —")
    p = w["used_pct"]
    return label + " " + T.bar(p, bw, warn, crit) + " " + T.sev("%3d%%" % round(p), T.level_for(p, warn, crit))


def auth_state(d):
    """Why a reading is missing, from the probe's detail or the words of its error."""
    det = d.get("detail") if isinstance(d.get("detail"), dict) else {}
    if det.get("auth"):
        return det["auth"]
    err = str(d.get("error") or "")
    if KEYCHAIN.search(err):
        return "keychain"
    for marker, state in (("renews it on its next run", "expired"), ("login expired", "relogin"),
                          ("not signed in", "none"), ("API key", "api_key")):
        if marker in err:
            return state
    return None


def hint(name, d) -> str:
    state = auth_state(d)
    if state == "keychain":  # most likely signed in, but where this session cannot read the login
        det = d.get("detail") if isinstance(d.get("detail"), dict) else {}
        m = KEYCHAIN.search(str(d.get("error") or ""))
        return str(det.get("keychain") or (m.group(0) if m else "Keychain not readable"))
    if state == "expired":
        return "token expired · renews on the next `%s` run" % CLI[name]
    if state == "relogin":
        return "login expired · run `%s`" % LOGIN[name]
    if state == "none":
        return "not signed in · `%s`" % LOGIN[name]
    if state == "api_key":
        return "signed in with an API key · no plan limits"
    return " ".join(str(d.get("error") or "no reading yet").split())


def fit_items(items, room) -> str:
    """Items joined by " · ", as many as fit whole in `room` cells."""
    out = ""
    for it in items:
        cand = it if not out else out + T.sep() + it
        if T.width(cand) > room:
            break
        out = cand
    return out


def extras(name, d, wins, main, warn, crit, now) -> list:
    out = []
    if name != "cursor":  # Cursor's auto/api shares say less than its dollars below
        for w in wins:
            if any(w is m for m in main) or gone(w, now):
                continue
            lv = T.level_for(w["used_pct"], warn, crit)
            txt = "%s %d%%" % (w.get("label"), round(w["used_pct"]))
            out.append(T.sev(txt, lv) if lv != "ok" else T.dim(txt))
    cr = d.get("credits") if isinstance(d.get("credits"), dict) else {}
    if name == "claude" and cr.get("extra_usage_enabled") and _num(cr.get("monthly_limit")):
        used, cap = _num(cr.get("used")) or 0, cr["monthly_limit"]
        txt = "extra usage %s/%s" % (core.money(used), core.money(cap))
        out.append(T.c(txt, "amber") if used >= 0.9 * cap else T.dim(txt))
    if name == "codex":
        if cr.get("unlimited"):
            out.append(T.dim("unlimited credits"))
        elif _num(cr.get("balance")) is not None:
            out.append(T.dim("%s credits" % core.human_num(cr["balance"])))
    if name == "cursor":
        if _num(cr.get("included_limit_usd")):
            out.append(T.dim("$%.0f/$%.0f included" % (_num(cr.get("included_used_usd")) or 0, cr["included_limit_usd"])))
        if _num(cr.get("bonus_usd")):
            out.append(T.dim("$%.0f bonus" % cr["bonus_usd"]))
        if _num(cr.get("on_demand_used_usd")):
            out.append(T.c("$%.0f on-demand" % cr["on_demand_used_usd"], "amber"))
    return out


def reset_note(t, now) -> str:
    """'↻3h06m', '↻Tue 02:36', or a date once the weekday alone would be ambiguous (a monthly cycle)."""
    if t - now < 6 * 86400:
        return until(t)
    return T.g("reset") + time.strftime("%b %d", time.localtime(t))


def main_row(head, main, bw, warn, crit, now, budget) -> str:
    """Plan, meters and the soonest reset; the bars give way first when a label runs long."""
    resets = [t for t in (_num(w.get("resets_at")) for w in main) if t and t > now]
    tail = (" " + T.dim(reset_note(min(resets), now))) if resets else ""

    def build(width, end):
        return head + "  ".join(meter(w, width, warn, crit, now) for w in main) + end
    room = (budget - T.width(build(0, tail))) // len(main)
    if room < 3 and tail:
        tail = ""
        room = (budget - T.width(build(0, tail))) // len(main)
    return build(max(3, min(bw, room)), tail)


def provider_rows(x, name, key, bw, now) -> tuple:
    """(rows, level, stale note) for one provider; no rows when it has no data here."""
    d = x.data(key)
    if not isinstance(d, dict):
        return [], "ok", ""
    src = registry.SOURCES.get(key)
    cfg = src.config(x.host) if src else x.cfg.source(key)
    warn, crit = _f(cfg.get("warn"), 75), _f(cfg.get("crit"), 90)
    budget = x.cw - 1
    wins = [w for w in d.get("windows") or [] if isinstance(w, dict) and w.get("label") != "api"]
    main = ([w for w in wins if not w.get("extra")] or wins)[:1 if name == "cursor" else 2]
    head = T.pad(T.c(name, "fg") + " " + T.dim(plan_label(name, d.get("plan"))), HEAD)
    level = T.worst(*[a.get("sev") for a in d.get("alerts") or [] if isinstance(a, dict)])
    if not main:
        if cfg.get("quiet_errors"):
            return [], "ok", ""
        return [head + T.dim(T.fit(hint(name, d), budget - HEAD))], level, ""
    rows = [main_row(head, main, bw, warn, crit, now, budget)]
    ex = extras(name, d, wins, main, warn, crit, now)
    if ex:
        rows.append(" " * HEAD + fit_items(ex, budget - HEAD))
    rec = x.cache.rec(key) or {}
    if not rec.get("ok") and rec.get("error") and not cfg.get("quiet_errors"):
        # These numbers are the last good reading; when a login is why they are not newer, say so.
        failed = {"error": rec["error"]}
        if auth_state(failed):
            rows.append(" " * HEAD + T.dim(T.fit(hint(name, failed), budget - HEAD)))
    return rows, level, x.stale_note(key, 4, 1800)


def langfuse_row(x, budget) -> str:
    """'langfuse  web 602 traces $2.10 · … · 24h · v3.1', read defensively from the langfuse source."""
    d = x.data("langfuse")
    det = d.get("details") if isinstance(d, dict) else None
    projects = det.get("projects") if isinstance(det, dict) else None
    if not isinstance(projects, list):
        return ""
    bits = []
    for p in projects:
        if not isinstance(p, dict):
            continue
        cost = _num(p.get("cost_24h_usd"))
        bits.append("%s %s" % (str(p.get("project") or "?")[:16],
                               T.dim("%s traces" % core.human_num(_num(p.get("traces_24h")) or 0) +
                                     (" " + core.money(cost) if cost else ""))))
    if not bits:
        return ""
    version = det.get("version")
    tail = T.dim(" · 24h" + (" · v%s" % str(version)[:10] if version else ""))
    return kv("langfuse", fit_items(bits, budget - HEAD - T.width(tail)) + tail, HEAD)


@panel("ai", title="AI LIMITS")
def p_ai(x):
    now = core.now()
    budget = x.cw - 1
    # One bar width for every row, so the 5h and 7d meters line up from provider to provider.
    bw = max(6, min(12, (budget - HEAD - RESET_W - 2 - 16) // 2))
    rows, level, notes = [], "ok", []
    for name, key in PROVIDERS:
        r, lv, note = provider_rows(x, name, key, bw, now)
        rows += r
        level = T.worst(level, lv)
        if r and note:
            notes.append("%s %s" % (name, note))
    lf = langfuse_row(x, budget)
    if lf:
        rows.append(lf)
    note = (notes[0] + (" +%d" % (len(notes) - 1) if len(notes) > 1 else "")) if notes else ""
    return x.panel("ai", rows, level, note)

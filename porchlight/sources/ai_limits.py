"""AI subscription limits as sources: Claude Pro/Max, Codex (ChatGPT plans), Cursor
and Grok Build.

All four are global: in a fleet the hub collects them and peers render the hub's
copy. Each runs only where its tool is set up (its config directory exists) or
where fallback_hosts can answer for it, so a machine without Cursor has no
Cursor source rather than one that fails every run with "not signed in".

The fetchers live in ai_probe.py, which imports nothing from porchlight. When
this machine's login is missing or expired, fallback_hosts pipe that file to
`ssh HOST python3 -I -`: the other machine reads its own login and only the
numbers come back. Nothing here refreshes or writes a credential (ai_probe.py
says why).
"""
from __future__ import annotations

import os
import re
import time

from .. import core
from ..registry import source

# Percent of a window used before it alerts. Cursor's window is a whole billing month.
THRESHOLDS = {"claude": (75, 90), "codex": (75, 90), "cursor": (85, 98), "grok": (75, 90)}
_ROUTINE = re.compile(r"^you(?:'|’)ve used \d+(?:\.\d+)?% of", re.I)


def _defaults(name, **extra) -> dict:
    warn, crit = THRESHOLDS[name]
    d = {"warn": warn, "crit": crit, "fallback_hosts": [], "fallback_timeout": 25, "quiet_errors": False}
    d.update(extra)
    return d


DEFAULTS = {
    "claude": _defaults("claude", config_dir="", keychain=True),
    "codex": _defaults("codex", config_dir=""),
    "cursor": _defaults("cursor", keychain=True),
    "grok": _defaults("grok"),
}


def _probe():
    """ai_probe is imported on first use: subprocess, sqlite3 and urllib stay out of the login render."""
    from . import ai_probe
    return ai_probe


def _f(v, default) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def footprints(name, cfg) -> list:
    """Directories whose presence means the provider's tool is set up on this machine."""
    home = os.path.expanduser("~")
    if name == "claude":
        return [core.expand(cfg.get("config_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")]
    if name == "codex":
        return [core.expand(cfg.get("config_dir") or os.environ.get("CODEX_HOME") or "~/.codex")]
    if name == "cursor":
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        return [os.path.join(home, ".cursor"), os.path.join(home, "Library", "Application Support", "Cursor"),
                os.path.join(xdg, "cursor"), os.path.join(xdg, "Cursor")]
    return [os.path.join(home, ".grok")]


def _installed(name):
    """A hosts= predicate: on where the tool is set up or fallback_hosts can answer for it."""
    def check(host):
        cfg = host.cfg.source(name + "_usage", DEFAULTS[name])
        return bool(cfg.get("fallback_hosts")) or any(os.path.isdir(p) for p in footprints(name, cfg))
    return check


def probe_opts(ctx, name) -> dict:
    """What the probe needs from settings and the collector's environment (never a secret's value)."""
    o = {"http": ctx.http, "keychain": ctx.cfg.get("keychain", True) is not False}
    if name == "claude":
        o["claude_dir"] = str(ctx.cfg.get("config_dir") or ctx.env.get("CLAUDE_CONFIG_DIR") or "")
    elif name == "codex":
        o["codex_dir"] = str(ctx.cfg.get("config_dir") or ctx.env.get("CODEX_HOME") or "")
    elif name == "grok":
        o["xai_api_key"] = bool(ctx.env.get("XAI_API_KEY"))
    return o


def ssh_argv(cfg) -> list:
    """The fleet's ssh options. BatchMode: a background run can never stop at a password prompt."""
    argv = [core.which("ssh") or "/usr/bin/ssh", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=%d" % int(_f(cfg.get("fleet.connect_timeout", 6), 6)), "-o", "LogLevel=ERROR"]
    for opt in cfg.items("fleet.ssh_options"):
        argv += ["-o", str(opt)]
    return argv


def live(res) -> bool:
    """A reading straight from the vendor's API: no error, and not a rollout snapshot."""
    return not res.get("error") and res.get("source") == "api"


def fetch(ctx, name) -> dict:
    """This machine first; then each fallback host, in order, until one answers live."""
    ap = _probe()
    res = ap.fetch(name, probe_opts(ctx, name))
    hosts = [str(h).strip() for h in ctx.cfg.get("fallback_hosts") or [] if str(h).strip()]
    if live(res) or not hosts:
        return res
    ropts = {"keychain": ctx.cfg.get("keychain", True) is not False}
    if ctx.cfg.get("config_dir"):
        ropts[name + "_dir"] = str(ctx.cfg["config_dir"])  # as written: a ~ expands on the other machine
    tried = [res]
    deadline = time.monotonic() + _f(ctx.cfg.get("fallback_timeout"), 25)
    for h in hosts:
        left = deadline - time.monotonic()
        if left < 2:
            break
        r = ap.run_remote(h, [name], ropts, ssh=ssh_argv(ctx.host.cfg), timeout=left)[0]
        tried.append(r)
        if live(r):
            return r
    # Nobody answered live: the freshest reading that has numbers (a rollout snapshot), else ours.
    with_data = [r for r in tried if r.get("windows")]
    best = max(with_data, key=lambda r: r.get("as_of") or 0) if with_data else res
    best.setdefault("detail", {})["tried"] = [
        "%s: %s" % ((r.get("detail") or {}).get("via") or "here", str(r.get("error") or r.get("source"))[:80])
        for r in tried]
    return best


def _when(t) -> str:
    """A reset time for alert text: absolute, because an alert is read long after it is written."""
    return time.strftime("%a %H:%M", time.localtime(t)) if t else "?"


def window_alerts(label, res, warn, crit, skip=()) -> list:
    """One alert per window at or past warn ("Claude 7d at 91% (resets Tue 02:36)")."""
    out = []
    for w in res.get("windows") or []:
        p = w.get("used_pct")
        if not isinstance(p, (int, float)) or p < warn or w.get("label") in skip:
            continue
        out.append({"sev": "crit" if p >= crit else "warn",
                    "msg": "%s %s at %d%% (resets %s)" % (label, w.get("label"), int(p), _when(w.get("resets_at")))})
    return out


def thresholds(ctx, name) -> tuple:
    warn, crit = THRESHOLDS[name]
    return _f(ctx.cfg.get("warn"), warn), _f(ctx.cfg.get("crit"), crit)


def finish(res, alerts) -> dict:
    res.pop("host", None)  # the reading is about an account, not the machine that fetched it
    if alerts:
        res["alerts"] = alerts
    return res


def cursor_alerts(res, warn, crit) -> list:
    """At most one Cursor alert, in Cursor's own words when it has some. Its banner and the
    total window say the same thing, and the auto/api shares are slices of that total.
    A routine "You've used N% of your included usage" below warn is not worth an alert."""
    al = window_alerts("Cursor", res, warn, crit, skip=("auto", "api"))
    msg = " ".join(str((res.get("detail") or {}).get("message") or "").split()).rstrip(".")
    if not msg or (not al and _ROUTINE.match(msg)):
        return al
    total = next((w for w in res.get("windows") or [] if w.get("label") == "total"), {})
    od = (res.get("credits") or {}).get("on_demand_used_usd")
    bits = (["resets %s" % _when(total["resets_at"])] if total.get("resets_at") else []) + (
        ["on-demand $%.0f" % od] if od else [])
    return [{"sev": al[0]["sev"] if al else "warn",
             "msg": "Cursor: %s%s" % (msg[:120], " (%s)" % ", ".join(bits) if bits else "")}]


@source("claude_usage", ttl=600, timeout=40, scope="global", hosts=_installed("claude"), defaults=DEFAULTS["claude"])
def claude_usage(ctx):
    """Claude Pro/Max plan: the 5h and weekly windows, per-model weekly limits, extra usage."""
    res = fetch(ctx, "claude")
    al = window_alerts("Claude", res, *thresholds(ctx, "claude"))
    cr = res.get("credits") or {}
    if cr.get("extra_usage_enabled") and cr.get("limit_reached"):
        al.append({"sev": "warn", "msg": "Claude extra usage hit its monthly spend limit"})
    return finish(res, al)


@source("codex_usage", ttl=600, timeout=30, scope="global", hosts=_installed("codex"), defaults=DEFAULTS["codex"])
def codex_usage(ctx):
    """Codex on a ChatGPT plan: usage windows and credits (a rollout-file snapshot when offline)."""
    res = fetch(ctx, "codex")
    al = window_alerts("Codex", res, *thresholds(ctx, "codex"))
    det = res.get("detail") or {}
    kind = det.get("rate_limit_reached_type")
    if det.get("limit_reached") or kind:
        al.insert(0, {"sev": "crit", "msg": "Codex rate limit reached" + (" (%s)" % str(kind)[:40] if isinstance(kind, str) else "")})
    return finish(res, al)


@source("cursor_usage", ttl=1800, timeout=30, scope="global", hosts=_installed("cursor"), defaults=DEFAULTS["cursor"])
def cursor_usage(ctx):
    """Cursor plan: included usage this billing cycle, bonus and on-demand spend."""
    res = fetch(ctx, "cursor")
    return finish(res, cursor_alerts(res, *thresholds(ctx, "cursor")))


@source("grok_usage", ttl=3600, timeout=20, scope="global", retry=6 * 3600, hosts=_installed("grok"),
        defaults=DEFAULTS["grok"])
def grok_usage(ctx):
    """Grok Build CLI on a grok.com plan: credit use this billing period."""
    res = fetch(ctx, "grok")
    return finish(res, window_alerts("Grok", res, *thresholds(ctx, "grok")))

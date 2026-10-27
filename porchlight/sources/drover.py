"""Drover: the agent sessions this machine's Drover daemon sees, on every host it runs.

Drover (github.com/coletebou/drover) runs Claude Code, Codex and other agents in
tmux and headless sessions across a fleet; its daemon answers small JSON ops on
this machine (`POST <url>api/op/<op>`, `{ok, result}`). This source reads four:

  native.summary        who waits on you and what they ask, who works where, the
                        per-host counts and whether the manager is up
  native.notifications  the alert feed after the last cursor: sessions that finished
                        or crashed. The source keeps that feed itself, so a finish from
                        before your last visit is still there after the daemon's own
                        short ring has dropped it
  fleet.versions        which Drover commit each daemon runs, and which need a
                        restart to load their checkout (on the fleet hub by default)
  credentials.status    the credential pool Drover spreads launches over (hub)

The token files are the daemon's own (<state>/token and <state>/widget-token). A
token only ever travels in the Authorization header of a request to the daemon's
own URL, and is never stored. The two native ops go out with the read-only widget
token whenever it exists (the daemon refuses it for anything else), so nothing
this source sends can message or steer a session; the hub ops need the main token.

Drover text (names, questions, summaries) is masked by pattern only: token shapes
and URI credentials. Never by matching secret values against it: that would tell
whoever reads the dashboard which secret equals which word on the screen.
"""
from __future__ import annotations

import os
import re

from .. import core, secrets
from ..registry import source

DEFAULT_STATE = "~/.local/state/drover"
FEED_KEEP = 20         # finished and crashed sessions kept, newest first
FEED_DAYS = 7
CURSOR_MAX = 16384     # the daemon's cursor lists the ids it delivered recently
WAITING_KEEP = 8
WORKING_KEEP = 40
NATIVE_TIMEOUT = 20.0  # native.summary may rebuild every host's board first: 7-12 s on a busy 6-core host
_FEED_KINDS = ("finished", "error")
_FEED_TITLE = re.compile(r"^(.*?)\s+(finished|exited unexpectedly|exited with code -?\d+|stopped answering|"
                         r"hit an error|failed|errored)$", re.I)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ID = re.compile(r"[^A-Za-z0-9._-]")


def text(v, n: int = 140) -> str:
    """Drover text made safe for one dashboard line: control characters out, whitespace
    collapsed, token shapes and URI credentials masked (by pattern, see the module
    docstring), then cut to n characters. Masking comes first, so a cut can never
    leave a token's head that is too short for the pattern to recognise."""
    s = " ".join(_CONTROL.sub(" ", str(v if v is not None else "")).split())
    return secrets.mask_shapes(s)[:n]


def short_id(v) -> str:
    return _ID.sub("", str(v or ""))[:12]


def session_name(item) -> str:
    """A session's name; "#" and its short id when Drover redacted the name (a name that
    reads like a token comes through as "[redacted]")."""
    name = text(item.get("name"), 40)
    if name in ("", "[redacted]"):
        return "#" + (short_id(item.get("id"))[:8] or "?")
    return name


def ask_of(item) -> str:
    """What a waiting session asks, without the "<name> needs you:" Drover puts first when no
    question is on screen ("demo-api needs you: status: waiting"): the row names the session."""
    ask, name = text(item.get("headline")), text(item.get("name"), 40)
    if name and ask.startswith(name + " needs "):
        ask = re.sub(r"^needs (?:you|a decision|an answer)\b[:\s]*", "", ask[len(name) + 1:])
    return ask


def _when(v) -> int:
    try:
        return core.iso_epoch(v) or 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _host(v) -> str:
    return text(v, 40) or "?"


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError, OverflowError):
        return 0


def _dicts(v, n: int) -> list:
    """The dict items of a list field, at most n: a daemon from another version may send anything."""
    return [i for i in v[:n] if isinstance(i, dict)] if isinstance(v, list) else []


def _want(setting, host) -> bool:
    """A hub-only read: "hub" (the default) on the fleet hub, true everywhere, false never."""
    return setting is True or (setting == "hub" and host.hub)


def find_daemon(cfg, env) -> tuple:
    """(url, main token, widget token) of this machine's Drover daemon; url "" without one."""
    state = core.expand(str(cfg.get("state_dir") or env.get("DROVER_STATE") or DEFAULT_STATE))
    meta = core.read_json(os.path.join(state, "daemon.json"))
    url = str(meta.get("url") or "").strip() if isinstance(meta, dict) else ""

    def token(name):
        tok = core.read_text(os.path.join(state, name), 4096).strip()
        return tok if tok and not any(ch.isspace() for ch in tok) else ""
    return url, token("token"), token("widget-token")


def op(ctx, url: str, tok: str, name: str, args: dict, timeout: float = 4.0) -> tuple:
    """(result, why): the result is None unless the daemon answered ok; why is a short
    reason (a transport failure, an HTTP status, or the daemon's own words)."""
    try:
        r = ctx.http(url.rstrip("/") + "/api/op/" + name, method="POST", json_body=args, timeout=timeout,
                     headers={"Authorization": "Bearer " + tok})
    except ValueError:
        return None, "bad daemon url"
    except Exception as e:  # noqa: BLE001 — refused, reset, DNS, a timeout: the daemon is not there
        return None, core.net_reason(e)
    body = r.body if isinstance(r.body, dict) else {}
    if r.ok and body.get("ok"):
        return body.get("result"), None
    why = text(body.get("error"), 60)
    if r.ok:
        return None, why or "unexpected reply"
    return None, ("HTTP %d" % r.status) + (": " + why if why else "")


def summary(res) -> dict:
    """native.summary → the fields the panel shows, every string cleaned and bounded."""
    tot = res.get("totals") if isinstance(res.get("totals"), dict) else {}
    mgr = res.get("manager") if isinstance(res.get("manager"), dict) else {}
    return {
        "waiting": [{"name": session_name(w), "host": _host(w.get("host")), "engine": text(w.get("engine"), 16),
                     "kind": text(w.get("kind"), 16), "since": _when(w.get("since")),
                     "ask": ask_of(w)} for w in _dicts(res.get("waiting"), WAITING_KEEP)],
        "working": [{"name": session_name(w), "host": _host(w.get("host")), "engine": text(w.get("engine"), 16)}
                    for w in _dicts(res.get("working"), WORKING_KEEP)],
        "hosts": [{"name": _host(h.get("name")), "up": h.get("up") is not False, "waiting": _int(h.get("waiting")),
                   "working": _int(h.get("working")), "idle": _int(h.get("idle"))}
                  for h in _dicts(res.get("hosts"), 24)],
        "totals": {k: _int(tot.get(k)) for k in ("waiting", "working", "idle", "hosts")},
        "manager": {"up": bool(mgr.get("up"))},
    }


def feed(ctx, url: str, tok: str, prev: dict) -> tuple:
    """(finished and crashed sessions, newest first; the cursor; why the read failed or None).
    Only what is new since the stored cursor is asked for; one entry per session and kind
    (its latest); a week of history, FEED_KEEP entries at most."""
    old = [i for i in prev.get("feed") or [] if isinstance(i, dict)]
    cursor = prev.get("feed_cursor") if isinstance(prev.get("feed_cursor"), str) else None
    res, why = op(ctx, url, tok, "native.notifications", dict({"limit": 100}, **({"since": cursor} if cursor else {})),
                  timeout=NATIVE_TIMEOUT)
    if not isinstance(res, dict):
        return old, cursor, why or "no feed"
    items = list(old)
    for it in _dicts(res.get("items"), 200):
        if it.get("test"):
            continue  # a notify.test alert is not news
        kind = it.get("kind") if it.get("kind") in _FEED_KINDS else it.get("category")
        if kind not in _FEED_KINDS:
            continue
        title = text(it.get("title"), 120)
        m = _FEED_TITLE.match(title)
        name, what = (m.group(1), m.group(2).lower()) if m else (title, kind)
        items.append({"at": _when(it.get("at")), "kind": kind, "host": _host(it.get("host")),
                      "name": name[:40] or "?", "what": what[:40],
                      "text": "" if kind == "error" else text(it.get("body")),  # an error's body is a path or an ssh error
                      "sid": short_id(it.get("sessionId")) or name[:24]})
    latest = {}
    for i in items:
        key = (i.get("sid"), i.get("kind"))
        if key not in latest or (i.get("at") or 0) >= (latest[key].get("at") or 0):
            latest[key] = i
    cutoff = core.now() - FEED_DAYS * 86400
    keep = sorted((i for i in latest.values() if (i.get("at") or 0) >= cutoff), key=lambda i: -(i.get("at") or 0))
    new = res.get("cursor")
    if isinstance(new, str) and new and len(new) <= CURSOR_MAX:
        cursor = new
    return keep[:FEED_KEEP], cursor, None


def details(ctx, url: str, main: str, prev: dict, cfg) -> dict:
    """Daemon versions and the credential pool (main token: read ops), every
    recheck_minutes; the last answer is kept through a miss. {} when not wanted here."""
    want_v, want_p = _want(cfg.get("versions", "hub"), ctx.host), _want(cfg.get("pool", "hub"), ctx.host)
    if not main or not (want_v or want_p):
        return {}
    old = prev.get("details") if isinstance(prev.get("details"), dict) else {}
    try:
        every = max(1.0, float(cfg.get("recheck_minutes", 10))) * 60
    except (TypeError, ValueError):
        every = 600.0
    if core.now() - float(old.get("at") or 0) < every:
        return {k: v for k, v in old.items() if k == "at" or (k == "versions" and want_v) or (k in ("pool", "pick") and want_p)}
    jobs = {}
    if want_v:
        jobs["versions"] = lambda: op(ctx, url, main, "fleet.versions", {}, timeout=8.0)[0]
    if want_p:
        jobs["pool"] = lambda: op(ctx, url, main, "credentials.status", {})[0]
    res = core.parallel(jobs, timeout=9.0)
    out = {"at": core.now()}
    v = res.get("versions")
    if isinstance(v, dict):
        out["versions"] = [{"host": _host(h.get("host")), "ok": bool(h.get("ok")),
                            "state": text(h.get("state") or ("unreachable" if not h.get("ok") else "unknown"), 16),
                            "loaded": short_id(h.get("loaded"))[:7],
                            "checkout": short_id((h.get("checkout") or {}).get("commit")
                                                 if isinstance(h.get("checkout"), dict) else "")[:7]}
                           for h in _dicts(v.get("hosts"), 24)]
    elif want_v and old.get("versions"):
        out["versions"] = old["versions"]
    c = res.get("pool")
    if isinstance(c, dict):
        out["pool"] = [{"id": text(e.get("id"), 24) or "?", "available": bool(e.get("available")),
                        "active": _int(e.get("active")), "max": _int(e.get("maxConcurrent")),
                        "throttles": _int(e.get("recentThrottles")),
                        "missing": [text(m, 48) for m in e["missing"][:3]] if isinstance(e.get("missing"), list) else []}
                       for e in _dicts(c.get("pool"), 8)]  # variable names, never values
        out["pick"] = text(c.get("recommendation"), 24)
    elif want_p and old.get("pool"):
        out["pool"], out["pick"] = old["pool"], old.get("pick", "")
    return out


def _words(s: str, n: int) -> str:
    """s cut to n characters at a word boundary, with an ellipsis when cut."""
    if len(s) <= n:
        return s
    head = s[:n - 1].rsplit(" ", 1)[0] if " " in s[:n - 1] else s[:n - 1]
    return head.rstrip(" ,;:") + "…"


def waiting_alert(waiting, total: int, here: str) -> str:
    def where(w):
        return "%s@%s" % (w["name"], here if w["host"] == "local" else w["host"])
    if total == 1 and waiting:
        w = waiting[0]
        return "%s is waiting on you%s" % (where(w), (": " + _words(w["ask"], 90)) if w["ask"] else "")
    shown = ", ".join(where(w) for w in waiting[:3])
    return "%d agents waiting on you (%s%s)" % (total, shown, ", …" if total > 3 else "")


@source("drover", ttl=90, timeout=25, defaults={"state_dir": "", "versions": "hub", "pool": "hub", "recheck_minutes": 10})
def drover(ctx):
    """Drover: who waits on you and why, what finished or crashed, who works on which host."""
    cfg, prev = ctx.cfg, ctx.prev or {}
    url, main, widget = find_daemon(cfg, ctx.env)
    if not url:
        return {"daemon": "none"}  # Drover is not running on this machine: nothing to show
    if not (main or widget):
        return {"daemon": "down", "why": "no token file"}
    read = widget or main  # the read-only token for the native ops whenever there is one
    keep = {k: prev[k] for k in ("feed", "feed_cursor", "details") if k in prev}
    jobs = {"summary": lambda: op(ctx, url, read, "native.summary", {}, timeout=NATIVE_TIMEOUT),
            "feed": lambda: feed(ctx, url, read, prev),
            "details": lambda: details(ctx, url, main, prev, cfg)}
    res = core.parallel(jobs, timeout=NATIVE_TIMEOUT + 2)
    got, why = res.get("summary") or (None, "timeout")
    if not isinstance(got, dict):
        # A daemon that does not answer is this machine's state, as an engine that is not
        # running is Docker's: shown as it is (no last board passed off as current, and no
        # waiting alert nobody can vouch for). The feed and its cursor wait for its return.
        return dict(keep, daemon="down", why=why or "no answer")
    data = dict(summary(got), daemon="up", here=ctx.host.label)  # the machine Drover's "local" is
    data["feed"], data["feed_cursor"], feed_err = res.get("feed") or (keep.get("feed") or [], keep.get("feed_cursor"), "timeout")
    if feed_err:
        data["feed_error"] = feed_err
    det = res.get("details")
    if det is None:
        det = keep.get("details") or {}
    if det:
        data["details"] = det
    total = max(data["totals"]["waiting"], len(data["waiting"]))
    if data["waiting"]:
        data["alerts"] = [{"sev": "warn", "msg": waiting_alert(data["waiting"], total, ctx.host.label)}]
    return data

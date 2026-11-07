"""Knowledge: what you and your agents wrote down, in one dashboard panel.

Contract: docs/contracts/knowledge-v1.md (the record shape and settings).
Four independent, optional parts, each isolated behind its own try/except and
I/O timeout, each keeping its previous data (`ctx.prev`) on failure:

  brain        a shared memory service: health is a public GET, recent entries
               come from a stateless MCP `tools/call` (JSON-RPC 2.0 over HTTP
               POST) with a bearer key read from the environment
  reflections  open reflections from the local Drover daemon (sources.drover's
               own find_daemon/op: the main token only, reflections.list is not
               on the widget token's scope)
  wikis        a bounded local walk of a Markdown checkout (git wiki, vault):
               page count and the most recently modified pages
  links        plain {label, url} pairs: no fetch, no failure mode

Nothing here stores a body: only titles, counts and pointers (ids, paths,
URLs). Every string that could hold a secret shape goes through
`secrets.mask_shapes` before it is kept.
"""
from __future__ import annotations

import os
import re

from .. import core, registry, secrets
from ..registry import source
from . import drover as _drover

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
TITLE_MAX = 160
WIKI_MAX_FILES = 5000   # at most this many files walked per wiki, matched or not
WIKI_RECENT = 3
REFLECTIONS_LIMIT = 200  # "open" is a count of at most this many (a dashboard number, not a promise of all of them)
BRAIN_RECENT_MAX = 50
PARALLEL_TIMEOUT = 12.0  # under the source's own 15s timeout, leaving margin for the collector's own deadline


def _title(v, n: int = TITLE_MAX) -> str:
    """One line, control characters out, whitespace collapsed, token shapes masked, cut to n.
    For freeform text (a title, an error message) that might, by accident, hold something
    secret-shaped."""
    s = " ".join(_CONTROL.sub(" ", str(v if v is not None else "")).split())
    return secrets.mask_shapes(s)[:n]


def _clean_id(v, n: int = 160) -> str:
    """An id/slug/pointer: control characters out, whitespace collapsed, bounded, and only
    known token prefixes and URI credentials masked (`secrets.mask` with no env). Not
    `mask_shapes`: many brain slugs are long, readable strings its run-of-40+-characters
    rule would redact, breaking "copy id"; a token-prefixed string still never survives."""
    s = " ".join(_CONTROL.sub(" ", str(v if v is not None else "")).split())
    return secrets.mask(s, env={})[:n]


def _url(v) -> str:
    s = str(v or "").strip()
    return s if s.startswith(("http://", "https://")) else ""


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError, OverflowError):
        return 0


# ── brain: health (public GET) and recent entries (MCP tools/call) ──────────
def _brain_health(ctx, url: str):
    """(body dict, why): why is None on a clean 2xx JSON reply."""
    try:
        r = ctx.http(url, timeout=6.0)
    except Exception as e:  # noqa: BLE001 — DNS, refused, TLS, a timeout: the brain is not there
        return None, core.net_reason(e)
    if not r.ok:
        return None, "HTTP %d" % r.status
    return (r.body if isinstance(r.body, dict) else {}), None


def _brain_recent(ctx, url: str, tool: str, limit: int, key: str):
    """(items, why): items is the brain's own list of entry dicts (titles only kept later)."""
    try:
        r = ctx.http(url, method="POST", timeout=8.0,
                     headers={"Authorization": "Bearer " + key, "Accept": "application/json, text/event-stream"},
                     json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": tool, "arguments": {"limit": limit}}})
    except Exception as e:  # noqa: BLE001
        return None, core.net_reason(e)
    if not r.ok:
        return None, "HTTP %d" % r.status
    body = r.body if isinstance(r.body, dict) else {}
    if isinstance(body.get("error"), dict):  # a JSON-RPC level error (bad method, bad params)
        return None, _title(body["error"].get("message"), 80) or "mcp error"
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    if result.get("isError"):  # the tool itself refused (MCP error -32602: Tool x not found, ...)
        content = result.get("content") if isinstance(result.get("content"), list) else []
        first = content[0] if content and isinstance(content[0], dict) else {}
        return None, _title(first.get("text"), 80) or "tool error"
    items = None
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and isinstance(sc.get("items"), list):
        items = sc["items"]
    else:  # defensive fallback: some servers answer with only the text block
        content = result.get("content") if isinstance(result.get("content"), list) else []
        first = content[0] if content and isinstance(content[0], dict) else {}
        text = first.get("text")
        if isinstance(text, str):
            import json
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                items = parsed["items"]
    if items is None:
        return None, "bad reply"
    return items, None


def _recent_entry(it: dict) -> dict:
    created = core.iso_epoch(it.get("createdAt") or it.get("created"))
    return {"id": _clean_id(it.get("slug") or it.get("id"), 80), "title": _title(it.get("title")),
            "kind": _title(it.get("kind"), 24) or "memory", "created": created or 0, "url": _url(it.get("url"))}


def _brain(ctx, cfg, prev: dict) -> dict:
    name = _title(cfg.get("brain_name") or "brain", 40) or "brain"
    web_url = _url(cfg.get("brain_web_url"))
    out = {"name": name, "ok": True, "error": "", "ms": _int(prev.get("ms")), "entries": _int(prev.get("entries")),
           "stale": bool(prev.get("stale")), "recent": prev.get("recent") if isinstance(prev.get("recent"), list) else [],
           "web_url": web_url}
    errors = []
    health_url = _url(cfg.get("brain_health_url"))
    if health_url:
        t0 = core.now()
        body, err = _brain_health(ctx, health_url)
        out["ms"] = int((core.now() - t0) * 1000)
        if err:
            errors.append(err)
        else:
            out["entries"] = _int(body.get("entries"))
            out["stale"] = bool(body.get("stale"))
            if body.get("ok") is False:
                errors.append(_title(body.get("reason") or body.get("error"), 60) or "not ok")
    mcp_url = _url(cfg.get("brain_mcp_url"))
    if mcp_url:
        key_env = str(cfg.get("brain_key_env") or "")
        key = str(ctx.env.get(key_env) or "") if key_env else ""
        if not key:
            errors.append("no %s" % (key_env or "key"))
        else:
            tool = str(cfg.get("brain_list_tool") or "brain_list")
            limit = _int(cfg.get("brain_recent")) or 5
            limit = min(limit, BRAIN_RECENT_MAX)
            items, err = _brain_recent(ctx, mcp_url, tool, limit, key)
            if err:
                errors.append(err)
            else:
                out["recent"] = [_recent_entry(it) for it in items[:limit] if isinstance(it, dict)]
    if errors:
        out["ok"], out["error"] = False, "; ".join(errors)[:200]
    return out


# ── reflections: the local Drover daemon (main token; not on the widget scope) ──
def _reflections(ctx, prev: dict) -> dict:
    base = {"open": _int(prev.get("open")), "by_kind": prev.get("by_kind") if isinstance(prev.get("by_kind"), dict) else {},
            "recent": prev.get("recent") if isinstance(prev.get("recent"), list) else []}
    src = registry.SOURCES.get("drover")
    if src is None:
        return dict(base, ok=False, error="drover source not available")
    cfg = src.config(ctx.host)
    url, main, _widget = _drover.find_daemon(cfg, ctx.env)
    if not url:
        return dict(base, ok=False, error="no Drover daemon")
    if not main:  # reflections.list is not on the widget token's scope (server/http.js WIDGET_PATHS)
        return dict(base, ok=False, error="no Drover main token")
    res, why = _drover.op(ctx, url, main, "reflections.list", {"all": True, "limit": REFLECTIONS_LIMIT}, timeout=6.0)
    if not isinstance(res, dict):
        return dict(base, ok=False, error=why or "no answer")
    items = [i for i in res.get("items") or [] if isinstance(i, dict)]
    by_kind = {}
    for it in items:
        k = _title(it.get("kind"), 16) or "other"
        by_kind[k] = by_kind.get(k, 0) + 1
    recent = [{"id": _clean_id(it.get("id"), 24), "title": _title(it.get("title")), "kind": _title(it.get("kind"), 16) or "other",
               "host": _clean_id(it.get("host"), 40), "created": core.iso_epoch(it.get("created_at")) or 0}
              for it in items[:5]]
    return {"ok": True, "error": "", "open": len(items), "by_kind": by_kind, "recent": recent}


# ── wikis: a bounded local Markdown walk ─────────────────────────────────────
def _wiki(w: dict, prev_by_name: dict):
    if not isinstance(w, dict):
        return None
    name = _title(w.get("name"), 60) or "wiki"
    url = _url(w.get("url"))
    path = w.get("path")
    prev_w = prev_by_name.get(name) or {}
    if not path:
        return {"name": name, "url": url, "ok": True, "error": "", "pages": 0, "recent": []}
    root = core.expand(str(path))
    if not os.path.isdir(root):
        return {"name": name, "url": url, "ok": False, "error": "no such directory",
                "pages": _int(prev_w.get("pages")),
                "recent": prev_w.get("recent") if isinstance(prev_w.get("recent"), list) else []}
    pages, walked = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            walked += 1
            if walked > WIKI_MAX_FILES:
                break
            if not fn.lower().endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                mt = os.path.getmtime(full)
            except OSError:
                continue
            pages.append((mt, os.path.relpath(full, root).replace(os.sep, "/"), fn))
        if walked > WIKI_MAX_FILES:
            break
    pages.sort(key=lambda t: -t[0])
    recent = [{"title": _title(os.path.splitext(fn)[0], 80), "path": rel, "mtime": mt}
              for mt, rel, fn in pages[:WIKI_RECENT]]
    return {"name": name, "url": url, "ok": True, "error": "", "pages": len(pages), "recent": recent}


# ── links: a plain passthrough, no fetch ─────────────────────────────────────
def _link(link):
    if not isinstance(link, dict):
        return None
    url = _url(link.get("url"))
    if not url:
        return None
    return {"label": _title(link.get("label"), 40) or "link", "url": url}


@source("knowledge", ttl=900, timeout=15, scope="global", optin=True,
        defaults={"brain_name": "brain", "brain_health_url": "", "brain_mcp_url": "", "brain_key_env": "",
                  "brain_list_tool": "brain_list", "brain_recent": 5, "brain_web_url": "", "reflections": True,
                  "wikis": [], "links": []})
def knowledge(ctx):
    """Knowledge: the brain's health and what was written since your last visit, open
    reflections, wikis and dashboard links."""
    cfg, prev = ctx.cfg, ctx.prev or {}
    jobs = {}
    if cfg.get("brain_health_url") or cfg.get("brain_mcp_url"):
        jobs["brain"] = lambda: _brain(ctx, cfg, prev.get("brain") if isinstance(prev.get("brain"), dict) else {})
    if cfg.get("reflections", True):
        jobs["reflections"] = lambda: _reflections(ctx, prev.get("reflections") if isinstance(prev.get("reflections"), dict) else {})
    wikis_cfg = [w for w in (cfg.get("wikis") or []) if isinstance(w, dict)]
    if wikis_cfg:
        prev_wikis = {w.get("name"): w for w in (prev.get("wikis") or []) if isinstance(w, dict)}
        jobs["wikis"] = lambda: [w for w in (_wiki(x, prev_wikis) for x in wikis_cfg) if w is not None]
    res = core.parallel(jobs, timeout=PARALLEL_TIMEOUT)
    data = {"v": 1}
    if "brain" in jobs:
        b = res.get("brain")
        data["brain"] = b if isinstance(b, dict) else dict(prev.get("brain") or {}, ok=False, error="timeout")
    if "reflections" in jobs:
        r = res.get("reflections")
        data["reflections"] = r if isinstance(r, dict) else dict(prev.get("reflections") or {}, ok=False, error="timeout")
    if "wikis" in jobs:
        w = res.get("wikis")
        data["wikis"] = w if isinstance(w, list) else (prev.get("wikis") if isinstance(prev.get("wikis"), list) else [])
    links = [link for link in (_link(x) for x in (cfg.get("links") or [])) if link is not None]
    if links:
        data["links"] = links
    alerts = []
    b = data.get("brain")
    if isinstance(b, dict) and (b.get("ok") is False or b.get("stale") or _int(b.get("entries")) == 0):
        alerts.append({"sev": "warn", "msg": "%s: %s" % (b.get("name") or "brain",
                                                           b.get("error") or ("stale" if b.get("stale") else "empty index"))})
    if alerts:
        data["alerts"] = alerts
    return data

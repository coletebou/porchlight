"""Todos: open items from every system that tracks Cole's asks, in one normalized list.

Contract: docs/contracts/todo-item-v1.md (the item schema and the record shape).
Providers (each optional, each configured independently, each isolated behind its
own try/except and I/O timeout): badland, drover, reflections, github, file. A
provider that fails keeps its previous items (ctx.prev) and reports a short
`error`, never a response body or a credential; the others are unaffected.

Write handlers (porchlight -i only, read-only otherwise; see actions.py policy)
live here too, next to each provider's client: `todos.done`.
"""
from __future__ import annotations

import os
import re
import time

from .. import core, secrets
from ..actions import Result, action
from ..registry import source

TITLE_MAX = 200
GROUP_MAX = 80
TAG_MAX = 40
MAX_TAGS = 8
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PRIORITIES = ("urgent", "high", "medium", "low", "none")
_PRI_ORDER = {p: i for i, p in enumerate(_PRIORITIES)}
# Request budgets: each provider enforces its own, so one slow API never starves
# the others (the source as a whole still sits under the collector's deadline).
_BADLAND_BUDGET = 15.0
_BADLAND_REQ = 6.0
_MAX_LISTS = 50
_DROVER_REQ = 10.0
_REFLECTIONS_REQ = 10.0
_GITHUB_BUDGET = 15.0
_GITHUB_REQ = 7.0

DEFAULTS = {
    "keep": 200,
    "shown": 6,
    "ask_pattern": "",
    "ask_systems": ["badland", "github", "drover", "brain"],
    "badland": {"base_url": "", "web_url": "", "key_env": "BADLAND_API_KEY", "lists": []},
    "drover": {"enabled": True, "states": ["not-started", "in-progress"]},
    "reflections": {"enabled": True, "kinds": ["action", "question"]},
    "github": {"owner": "", "labels": [], "query": "", "token_env": ["GH_TOKEN", "GITHUB_TOKEN"]},
    "file": {"paths": []},
}


# ── Text safety ───────────────────────────────────────────────────────────────
def _text(v, n: int = TITLE_MAX) -> str:
    """One dashboard-safe line: control characters out, whitespace collapsed, token
    shapes and URI credentials masked (secrets.mask_shapes), cut to n characters."""
    s = " ".join(_CONTROL.sub(" ", str(v if v is not None else "")).split())
    return secrets.mask_shapes(s)[:n]


def _short_error(e) -> str:
    """A provider failure in a few words: never a response body, never a credential."""
    if isinstance(e, OSError):
        return core.net_reason(e)
    return secrets.mask_shapes(str(e))[:120]


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ── The item ──────────────────────────────────────────────────────────────────
def _item(provider, ref, title, group="", state="open", priority="none", due=None, created=None,
          updated=None, url="", tags=(), host="", can=()) -> dict:
    pri = priority if priority in _PRI_ORDER else "none"
    return {"id": "%s:%s" % (provider, ref), "provider": provider, "ref": str(ref), "title": _text(title, TITLE_MAX),
            "group": _text(group, GROUP_MAX), "state": str(state or "open"), "priority": pri,
            "due": due if isinstance(due, (int, float)) else None,
            "created": created if isinstance(created, (int, float)) else None,
            "updated": updated if isinstance(updated, (int, float)) else None,
            "url": str(url or ""), "tags": [_text(t, TAG_MAX) for t in list(tags)[:MAX_TAGS] if t],
            "asks": [], "host": _text(host, 40), "can": [str(c) for c in can]}


def _sort_key(it):
    due = it["due"] if it["due"] is not None else float("inf")
    return (_PRI_ORDER.get(it["priority"], 4), due, -(it["updated"] or 0))


# ── Drover daemon (the lookup is sources/drover.py's, so [sources.drover] state_dir applies) ──
def _find_daemon(env, host=None):
    """(url, main token, widget token) of this machine's Drover daemon; url "" without one."""
    from ..registry import SOURCES
    from .drover import find_daemon
    src = SOURCES.get("drover")
    cfg = src.config(host) if (src is not None and host is not None) else {}
    return find_daemon(cfg, env)


def _drover_op(http, url, tok, name, args, timeout):
    """(result, why): result is None unless the daemon answered ok; why is a short reason."""
    try:
        r = http(url.rstrip("/") + "/api/op/" + name, method="POST", json_body=args, timeout=timeout,
                 headers={"Authorization": "Bearer " + tok})
    except ValueError:
        return None, "bad daemon url"
    except Exception as e:  # noqa: BLE001 — refused, reset, DNS, a timeout: the daemon is not there
        return None, _short_error(e)
    body = r.body if isinstance(r.body, dict) else {}
    if r.ok and body.get("ok"):
        return body.get("result"), None
    why = _text(body.get("error"), 80)
    if r.ok:
        return None, why or "unexpected reply"
    return None, ("HTTP %d" % r.status) + (": " + why if why else "")


# ── GitHub token ──────────────────────────────────────────────────────────────
def _github_token(env, cfg):
    """The first set GH_TOKEN/GITHUB_TOKEN-style var, else `gh auth token`."""
    for k in (cfg.get("token_env") or ["GH_TOKEN", "GITHUB_TOKEN"]):
        if env.get(k):
            return env[k]
    gh = core.which("gh")
    if not gh:
        return None
    rc, out, _err = core.run([gh, "auth", "token"], 5)
    tok = out.strip()
    return tok if rc == 0 and tok and not any(ch.isspace() for ch in tok) else None


# ── Providers (each: (ctx, cfg, prev_items) -> {"items", "ok", "ms", "error"} or None) ──
def _provider_badland(ctx, cfg, prev_items):
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        return None
    web = str(cfg.get("web_url") or "").strip().rstrip("/")
    key_env = str(cfg.get("key_env") or "BADLAND_API_KEY")
    wanted = {str(w) for w in (cfg.get("lists") or [])}
    t0 = time.monotonic()
    deadline = t0 + _BADLAND_BUDGET
    try:
        token = ctx.env.get(key_env)
        if not token:
            raise RuntimeError("no %s" % key_env)
        headers = {"Authorization": "Bearer " + token}
        r = ctx.http(base + "/api/todos/lists", headers=headers, timeout=min(_BADLAND_REQ, max(1.0, deadline - time.monotonic())))
        if not r.ok:
            raise RuntimeError("HTTP %d" % r.status)
        if not isinstance(r.body, dict) or not r.body.get("success"):
            raise RuntimeError("unexpected response")
        lists = [lst_ for lst_ in (r.body.get("data") or []) if isinstance(lst_, dict)]
        items = []
        for lst in lists[:_MAX_LISTS]:
            if lst.get("isArchived"):
                continue
            if lst.get("itemCount") == lst.get("completedCount"):
                continue
            if wanted and str(lst.get("id")) not in wanted and str(lst.get("title")) not in wanted:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError("timed out reading lists")
            lid = lst.get("id")
            ir = ctx.http(base + "/api/todos/lists/%s/items" % lid, headers=headers,
                          timeout=min(_BADLAND_REQ, max(1.0, deadline - time.monotonic())))
            if not ir.ok or not isinstance(ir.body, dict) or not ir.body.get("success"):
                raise RuntimeError("HTTP %d" % ir.status if not ir.ok else "unexpected response")
            title = str(lst.get("title") or "")
            for it in ir.body.get("data") or []:
                if not isinstance(it, dict) or it.get("isCompleted") or it.get("isArchived"):
                    continue
                items.append(_item("badland", it.get("id"), it.get("title"), group=title, state="open",
                                   priority=str(it.get("priority") or "none"), due=core.iso_epoch(it.get("dueDate")),
                                   created=core.iso_epoch(it.get("createdAt")), updated=core.iso_epoch(it.get("updatedAt")),
                                   url=("%s/todos/%s" % (web, lid)) if web else "", tags=it.get("tags") or [],
                                   can=("done",)))
        return {"items": items, "ok": True, "ms": int((time.monotonic() - t0) * 1000), "error": ""}
    except Exception as e:  # noqa: BLE001 — a provider must never take the source down
        return {"items": prev_items, "ok": False, "ms": int((time.monotonic() - t0) * 1000), "error": _short_error(e)}


_DROVER_STATE_MAP = {"not-started": "open", "in-progress": "in-progress"}


def _provider_drover(ctx, cfg, prev_items):
    if cfg.get("enabled") is False:
        return None
    url, main, _widget = _find_daemon(ctx.env, ctx.host)
    if not url:
        return None
    t0 = time.monotonic()
    if not main:
        return {"items": prev_items, "ok": False, "ms": 0, "error": "no main token"}
    try:
        wanted = set(cfg.get("states") or ["not-started", "in-progress"])
        res, why = _drover_op(ctx.http, url, main, "events.state", {}, _DROVER_REQ)
        if res is None:
            raise RuntimeError(why or "no answer")
        features = [f for f in (res.get("features") or []) if isinstance(f, dict)]
        items = []
        for f in features:
            state = str(f.get("state") or "not-started")
            if state not in wanted:
                continue
            links = [lnk for lnk in (f.get("links") or []) if isinstance(lnk, str)]
            url_ = next((lnk for lnk in links if lnk.startswith(("http://", "https://"))), "")
            items.append(_item("drover", f.get("fid"), f.get("title"), group=str(f.get("repo") or ""),
                               state=_DROVER_STATE_MAP.get(state, "open"), priority="none",
                               created=core.iso_epoch(f.get("created")), updated=core.iso_epoch(f.get("updated")),
                               url=url_, can=("done",)))
        return {"items": items, "ok": True, "ms": int((time.monotonic() - t0) * 1000), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"items": prev_items, "ok": False, "ms": int((time.monotonic() - t0) * 1000), "error": _short_error(e)}


def _provider_reflections(ctx, cfg, prev_items):
    if cfg.get("enabled") is False:
        return None
    url, main, _widget = _find_daemon(ctx.env, ctx.host)
    if not url:
        return None
    t0 = time.monotonic()
    if not main:
        return {"items": prev_items, "ok": False, "ms": 0, "error": "no main token"}
    try:
        kinds = [str(k) for k in (cfg.get("kinds") or ["action", "question"])]
        keep = _int(ctx.cfg.get("keep"), 200) if hasattr(ctx, "cfg") else 200
        args = {"all": True, "status": "open", "kind": ",".join(kinds), "limit": keep}
        res, why = _drover_op(ctx.http, url, main, "reflections.list", args, _REFLECTIONS_REQ)
        if res is None:
            raise RuntimeError(why or "no answer")
        rows = [r for r in (res.get("items") or []) if isinstance(r, dict)]
        items = []
        for r in rows:
            # Never store the body (contract: a pointer, not a copy of the reflection).
            items.append(_item("reflections", r.get("id"), r.get("title"), group=str(r.get("repo") or ""),
                               state="open", priority=_reflection_priority(r.get("priority")),
                               created=core.iso_epoch(r.get("created_at")), updated=core.iso_epoch(r.get("updated_at")),
                               tags=r.get("tags") or [], host=str(r.get("host") or ""), can=("done",)))
        return {"items": items, "ok": True, "ms": int((time.monotonic() - t0) * 1000), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"items": prev_items, "ok": False, "ms": int((time.monotonic() - t0) * 1000), "error": _short_error(e)}


def _reflection_priority(v):
    """Drover's reflections keep a 1-5 priority (Drover's ops-meta: "1 (urgent) to 5");
    map it onto our five-step scale, 1 urgent .. 5 none (docs/contracts/todo-item-v1.md)."""
    n = _int(v, 0)
    return {1: "urgent", 2: "high", 3: "medium", 4: "low", 5: "none"}.get(n, "none")


def _provider_github(ctx, cfg, prev_items):
    owner = str(cfg.get("owner") or "").strip()
    labels = [str(lbl).strip() for lbl in (cfg.get("labels") or []) if str(lbl).strip()]
    query = str(cfg.get("query") or "").strip()
    if not labels and not query:
        return None
    t0 = time.monotonic()
    token = _github_token(ctx.env, cfg)
    if not token:
        return {"items": prev_items, "ok": False, "ms": 0,
                "error": "no GitHub token (GH_TOKEN / GITHUB_TOKEN / gh)"}
    deadline = t0 + _GITHUB_BUDGET
    queries = [query] if query else [('user:%s label:"%s"' % (owner, lbl)) if owner else ('label:"%s"' % lbl) for lbl in labels]
    try:
        import urllib.parse
        items = []
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
        for q in queries:
            if time.monotonic() > deadline:
                raise TimeoutError("timed out")
            full = "is:open is:issue " + q
            r = ctx.http("https://api.github.com/search/issues?q=" + urllib.parse.quote(full, safe=""),
                        headers=headers, timeout=min(_GITHUB_REQ, max(1.0, deadline - time.monotonic())))
            if not r.ok:
                raise RuntimeError("HTTP %d" % r.status)
            if not isinstance(r.body, dict):
                raise RuntimeError("unexpected response")
            for it in r.body.get("items") or []:
                if not isinstance(it, dict):
                    continue
                repo_url = str(it.get("repository_url") or "")
                repo = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else "?"
                tags = [str(lbl.get("name")) for lbl in (it.get("labels") or []) if isinstance(lbl, dict) and lbl.get("name")]
                items.append(_item("github", "%s#%s" % (repo, it.get("number")), it.get("title"), group=repo,
                                   state="open", priority="none", created=core.iso_epoch(it.get("created_at")),
                                   updated=core.iso_epoch(it.get("updated_at")), url=str(it.get("html_url") or ""),
                                   tags=tags, can=("done",)))
        return {"items": items, "ok": True, "ms": int((time.monotonic() - t0) * 1000), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"items": prev_items, "ok": False, "ms": int((time.monotonic() - t0) * 1000), "error": _short_error(e)}


_TODOTXT_PRI = {"A": "high", "B": "medium", "C": "low"}
_MD_BOX = re.compile(r"^\s*[-*]\s+\[ \]\s*(.+)$")
_TXT_PRI = re.compile(r"^\(([A-C])\)\s+(.*)$")


def _provider_file(ctx, cfg, prev_items):
    paths = [str(p) for p in (cfg.get("paths") or []) if str(p).strip()]
    if not paths:
        return None
    t0 = time.monotonic()
    try:
        items = []
        for raw in paths:
            p = core.expand(raw)
            if not os.path.isfile(p):
                continue
            label, mt = os.path.basename(p), core.mtime(p)
            markdown = os.path.splitext(p)[1].lower() in (".md", ".markdown")
            for n, line in enumerate(core.read_text(p, 1 << 20).splitlines(), 1):
                if markdown:
                    m = _MD_BOX.match(line)
                    if not m:
                        continue
                    items.append(_item("file", "%s:%d" % (p, n), m.group(1).strip(), group=label,
                                       priority="none", created=mt, updated=mt))
                else:
                    s = line.strip()
                    if not s or s.lower().startswith("x "):
                        continue
                    m = _TXT_PRI.match(s)
                    pri, title = (_TODOTXT_PRI[m.group(1)], m.group(2)) if m else ("none", s)
                    items.append(_item("file", "%s:%d" % (p, n), title, group=label, priority=pri,
                                       created=mt, updated=mt))
        return {"items": items, "ok": True, "ms": int((time.monotonic() - t0) * 1000), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"items": prev_items, "ok": False, "ms": int((time.monotonic() - t0) * 1000), "error": _short_error(e)}


_PROVIDERS = (("badland", _provider_badland), ("drover", _provider_drover), ("reflections", _provider_reflections),
              ("github", _provider_github), ("file", _provider_file))


# ── Asks: the "track every ask" view ─────────────────────────────────────────
def _brain_recent_titles():
    """Recent brain entry titles from the knowledge record, never an extra brain query."""
    from .. import registry
    rec = core.read_json(registry.src_path("knowledge"))
    data = rec.get("data") if isinstance(rec, dict) else None
    brain = (data or {}).get("brain") if isinstance(data, dict) else None
    recent = (brain or {}).get("recent") if isinstance(brain, dict) else None
    return [str(e.get("title") or "") for e in recent if isinstance(e, dict)] if isinstance(recent, list) else []


def _apply_asks(items, pattern):
    """Sets item["asks"] on every item; returns (ids, error). ids: {ask_id: {provider: state}}."""
    if not pattern:
        for it in items:
            it["asks"] = []
        return {}, None
    try:
        rx = re.compile(pattern)
    except re.error as e:
        for it in items:
            it["asks"] = []
        return {}, _short_error(e)
    ids = {}
    for it in items:
        found = sorted({m.group(0) for m in rx.finditer(it["title"])})
        it["asks"] = found
        for aid in found:
            ids.setdefault(aid, {})[it["provider"]] = it["state"]
    return ids, None


def _asks_doc(pattern, ask_systems, ids, err, providers_report):
    if err:
        return {"pattern": pattern, "ids": {}, "systems": [], "error": err}
    answered = {name for name, rep in providers_report.items() if rep.get("ok")}
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = None
    # The brain only marks the asks its few newest entries name ("noted"). It is never
    # counted as a system that answered: a handful of recent titles is not a full read,
    # so an ask missing from them is not a gap ("missing: brain") worth claiming.
    brain_titles = _brain_recent_titles()
    if rx and brain_titles:
        for title in brain_titles:
            for m in rx.finditer(title):
                ids.setdefault(m.group(0), {}).setdefault("brain", "noted")
    answered.discard("brain")
    wanted = [str(s) for s in (ask_systems or [])]
    systems = [s for s in wanted if s in answered] if wanted else sorted(answered)
    return {"pattern": pattern, "ids": ids, "systems": systems}


def _overdue_alerts(items):
    now = core.now()
    out = []
    for it in items:
        if it["priority"] in ("urgent", "high") and it["due"] is not None and (now - it["due"]) > 86400:
            out.append({"sev": "warn", "msg": "overdue (%s): %s [%s]" % (
                core.human_age(now - it["due"]), it["title"][:80], it["provider"])})
    return out[:10]


@source("todos", ttl=600, timeout=20, scope="global", optin=True, defaults=DEFAULTS)
def todos(ctx):
    """Open items from every system that tracks Cole's asks: Badland todos, Drover
    features, Drover reflections, GitHub issues and local todo files, normalized to
    one list (contract: todo-item v1), plus the "track every ask" coverage view."""
    cfg = ctx.cfg
    prev = ctx.prev if isinstance(ctx.prev, dict) else {}
    prev_items = [it for it in (prev.get("items") or []) if isinstance(it, dict)]
    prev_by = {}
    for it in prev_items:
        prev_by.setdefault(it.get("provider"), []).append(it)

    providers_report = {}
    all_items = []
    for name, fn in _PROVIDERS:
        res = fn(ctx, cfg.get(name) or {}, prev_by.get(name, []))
        if res is None:
            continue
        kept_items = res.get("items") or []
        providers_report[name] = {"ok": bool(res.get("ok")), "open": len(kept_items),
                                  "ms": int(res.get("ms") or 0), "error": str(res.get("error") or "")}
        all_items.extend(kept_items)

    total = len(all_items)
    pattern = str(cfg.get("ask_pattern") or "")
    ids, err = _apply_asks(all_items, pattern)
    all_items.sort(key=_sort_key)
    keep = max(0, _int(cfg.get("keep"), 200))
    kept = all_items[:keep]

    record = {"v": 1, "items": kept, "providers": providers_report, "total": total}
    if pattern:
        record["asks"] = _asks_doc(pattern, cfg.get("ask_systems"), ids, err, providers_report)
    alerts = _overdue_alerts(kept)
    if alerts:
        record["alerts"] = alerts
    return record


# ── Write handlers (porchlight -i only; see actions.py for the policy) ──────
# A ref goes into a URL path (badland) or a JSON body (drover, reflections): only an
# id-shaped string, never "../", "?" or "#" from a cached record, reaches the API.
_REF_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _done_badland(host, env, cfg, ref) -> Result:
    if not _REF_OK.match(ref) or ".." in ref:
        return Result(False, "bad badland ref")
    bcfg = cfg.get("badland") or {}
    base = str(bcfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        return Result(False, "badland is not configured")
    token = env.get(str(bcfg.get("key_env") or "BADLAND_API_KEY"))
    if not token:
        return Result(False, "no badland token")
    headers = {"Authorization": "Bearer " + token}
    try:
        r = core.http(base + "/api/todos/items/%s" % ref, headers=headers, timeout=8)
    except Exception as e:  # noqa: BLE001
        return Result(False, _short_error(e))
    if not r.ok or not isinstance(r.body, dict) or not r.body.get("success"):
        return Result(False, "could not read the item (HTTP %d)" % r.status)
    item = r.body.get("data") or {}
    if not isinstance(item, dict):
        return Result(False, "unexpected response")
    if item.get("isCompleted"):
        return Result(True, "already done", refresh=("todos",))
    try:
        r2 = core.http(base + "/api/todos/items/%s/toggle" % ref, method="POST", headers=headers, timeout=8)
    except Exception as e:  # noqa: BLE001
        return Result(False, _short_error(e))
    if not r2.ok:
        return Result(False, "HTTP %d" % r2.status)
    return Result(True, "marked done", refresh=("todos",))


def _done_drover(host, env, cfg, ref) -> Result:
    if not _REF_OK.match(ref):
        return Result(False, "bad drover ref")
    url, main, _widget = _find_daemon(env, host)
    if not url or not main:
        return Result(False, "no drover daemon")
    res, why = _drover_op(core.http, url, main, "features.upsert", {"fid": ref, "state": "shipped"}, 10.0)
    if res is None and why:
        return Result(False, why)
    return Result(True, "marked shipped", refresh=("todos",))


def _done_reflections(host, env, cfg, ref) -> Result:
    if not _REF_OK.match(ref):
        return Result(False, "bad reflection ref")
    url, main, _widget = _find_daemon(env, host)
    if not url or not main:
        return Result(False, "no drover daemon")
    res, why = _drover_op(core.http, url, main, "reflections.mark", {"op": "done", "ids": [ref]}, 10.0)
    if res is None and why:
        return Result(False, why)
    return Result(True, "marked done", refresh=("todos",))


_GH_REF = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})#(\d{1,9})$")


def _done_github(host, env, cfg, ref) -> Result:
    gcfg = cfg.get("github") or {}
    token = _github_token(env, gcfg)
    if not token:
        return Result(False, "no GitHub token")
    m = _GH_REF.match(ref)
    if not m:
        return Result(False, "bad github ref")
    repo, number = m.group(1), m.group(2)
    try:
        r = core.http("https://api.github.com/repos/%s/issues/%s" % (repo, number), method="PATCH",
                      json_body={"state": "closed"}, headers={"Authorization": "Bearer " + token}, timeout=10)
    except Exception as e:  # noqa: BLE001
        return Result(False, _short_error(e))
    if not r.ok:
        return Result(False, "HTTP %d" % r.status)
    return Result(True, "closed", refresh=("todos",))


_DONE = {"badland": _done_badland, "drover": _done_drover, "reflections": _done_reflections, "github": _done_github}


@action("todos.done", writes=True)
def todo_done(host, args):
    """Mark one todo item done in its own system. args: {"id": "<provider>:<ref>"}."""
    full = str(args.get("id") or "")
    if ":" not in full:
        return Result(False, "bad id")
    provider, ref = full.split(":", 1)
    fn = _DONE.get(provider)
    if fn is None:
        return Result(False, "%s: unsupported" % provider) if provider != "file" else Result(False, "file: edit the file directly")
    from .. import secrets
    from ..registry import SOURCES
    env = secrets.load(host.cfg)
    cfg = SOURCES["todos"].config(host)
    return fn(host, env, cfg, ref)

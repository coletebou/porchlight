"""Knowledge: the state of what your agents remember, through this machine's Drover daemon.

Reads, each optional and failing alone:

  brain        brain.status: is the shared memory reachable, how many entries, is its
               index stale; brain.context: the latest decisions recorded in it
  search       search.status: the local search index over transcripts, memory,
               reflections and docs (chunks, sources, how fresh)
  reflections  reflections.repos: open and stale reflections per repo
  notes        markdown folders you keep notes or a wiki in (an Obsidian vault, a
               docs tree): how many notes, and the ones edited last
  links        your wikis and dashboards, by name and URL, so `porchlight -i` can
               open them (Drover's own UI is added when a daemon is found)

Only counts, titles, dates and repo names are stored. Note bodies, decision bodies
and every token stay out of the record; the daemon's token only ever travels in the
Authorization header to its own URL, and links never carry it.
"""
from __future__ import annotations

import os
import re
import time

from .. import core, secrets
from ..registry import source

DEFAULTS = {
    "state_dir": "",          # Drover's state dir (default: the drover source's setting, else ~/.local/state/drover)
    "brain": True,
    "decisions": 5,           # latest brain decisions kept
    "search": True,
    "reflections": True,
    "exclude_repos": [],      # reflection repos never shown; globs work ("scratch-*")
    "exclude_paths": [],      # drop repos whose open reflections all live under one of these ("/identities/work/")
    "notes": [],              # folders of markdown notes: ["~/notes", "~/wiki"]
    "notes_recent": 3,
    "links": [],              # [{name = "wiki", url = "https://wiki.example.com"}]
    "drover_link": True,      # add Drover's UI (its URL, never its token) to the links
    "index_stale_hours": 24,  # warn when the newest indexed chunk is older than this
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NOTES_CAP = 20000             # files looked at per folder, so a huge tree cannot stall the collector


def clean(v, n: int = 140) -> str:
    s = " ".join(_CONTROL.sub(" ", str(v if v is not None else "")).split())
    return secrets.mask_shapes(s)[:n]


def _int(v) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _epoch(v) -> int:
    try:
        return int(core.iso_epoch(v) or 0) if isinstance(v, str) else _int(v)
    except (TypeError, ValueError, OverflowError):
        return 0


def _brain(D, ctx, url, tok, cfg) -> dict:
    res, why = D.op(ctx, url, tok, "brain.status", {}, timeout=10)
    if not isinstance(res, dict):
        raise RuntimeError("brain.status: %s" % (why or "no answer"))
    h = res.get("health") if isinstance(res.get("health"), dict) else {}
    out = {"configured": bool(res.get("keyConfigured")), "ok": bool(h.get("ok")), "stale": bool(h.get("stale")),
           "entries": _int(h.get("entries")), "chunks": _int(h.get("chunks")), "status": _int(h.get("status")),
           "decisions": []}
    want = max(0, min(20, _int(cfg.get("decisions"))))
    if want and out["configured"]:
        ctxres, _why = D.op(ctx, url, tok, "brain.context", {}, timeout=12)
        for d in (ctxres or {}).get("recentDecisions") or [] if isinstance(ctxres, dict) else []:
            if isinstance(d, dict) and d.get("title"):
                out["decisions"].append({"title": clean(d.get("title"), 200), "slug": clean(d.get("slug"), 100),
                                         "at": _epoch(d.get("validFrom"))})
        out["decisions"] = out["decisions"][:want]
    return out


def _search(D, ctx, url, tok) -> dict:
    res, why = D.op(ctx, url, tok, "search.status", {}, timeout=10)
    if not isinstance(res, dict):
        raise RuntimeError("search.status: %s" % (why or "no answer"))
    i = res.get("index") if isinstance(res.get("index"), dict) else {}
    kinds = i.get("kinds") if isinstance(i.get("kinds"), dict) else {}
    return {"enabled": res.get("enabled") is not False, "chunks": _int(i.get("chunks")), "sources": _int(i.get("sources")),
            "newest": _epoch(i.get("newest")), "vectors": _int(i.get("vectors")),
            "kinds": {clean(k, 20): _int((v or {}).get("chunks")) for k, v in list(kinds.items())[:12] if isinstance(v, dict)}}


def repo_excluded(repo, patterns) -> bool:
    """exclude_repos: exact names or globs, case-insensitive."""
    import fnmatch
    r = str(repo or "").lower()
    return any(fnmatch.fnmatchcase(r, str(p).lower()) for p in patterns or [])


def _foreign_repos(D, ctx, url, tok, cfg) -> set:
    """Repos whose open reflections all sit under exclude_paths: reflections.repos carries no
    paths, so the open items (which do) decide. A repo with one item outside stays."""
    from .todos import _excluded_path
    res, why = D.op(ctx, url, tok, "reflections.list", {"all": True, "status": "open", "limit": 1000}, timeout=15)
    if not isinstance(res, dict):
        raise RuntimeError("reflections.list: %s" % (why or "no answer"))
    seen = {}
    for it in res.get("items") or []:
        if isinstance(it, dict):
            repo = str(it.get("repo") or "")
            seen[repo] = seen.get(repo, True) and _excluded_path(cfg, it)
    return {r for r, all_out in seen.items() if all_out}


def _reflections(D, ctx, url, tok, cfg) -> list:
    res, why = D.op(ctx, url, tok, "reflections.repos", {}, timeout=10)
    if not isinstance(res, list):
        raise RuntimeError("reflections.repos: %s" % (why or "no answer"))
    excl = list(cfg.get("exclude_repos") or [])
    foreign = _foreign_repos(D, ctx, url, tok, cfg) if cfg.get("exclude_paths") else set()
    rows = []
    for r in res:
        if not isinstance(r, dict) or repo_excluded(r.get("repo"), excl) or str(r.get("repo") or "") in foreign:
            continue
        if cfg.get("exclude_paths") and not _int(r.get("open")):
            continue  # no open items to say which identity it belongs to: leave it out
        rows.append({"repo": clean(r.get("repo"), 40), "open": _int(r.get("open")), "stale": _int(r.get("stale")),
                     "done": _int(r.get("done")), "last": _epoch(r.get("last"))})
    rows.sort(key=lambda r: (-r["open"], r["repo"]))
    return rows[:20]


def _notes(cfg) -> list:
    out = []
    keep = max(0, min(10, _int(cfg.get("notes_recent"))))
    for folder in cfg.get("notes") or []:
        root = core.expand(str(folder))
        if not os.path.isdir(root):
            out.append({"name": clean(os.path.basename(root.rstrip("/")) or folder, 40), "error": "not found"})
            continue
        n, seen, recent = 0, 0, []
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__")]
            for f in files:
                seen += 1
                if seen > NOTES_CAP:
                    break
                if f.endswith(".md"):
                    n += 1
                    recent.append((core.mtime(os.path.join(dirpath, f)), f[:-3]))
            if seen > NOTES_CAP:
                break
        recent.sort(reverse=True)
        out.append({"name": clean(os.path.basename(root.rstrip("/")), 40), "notes": n, "capped": seen > NOTES_CAP,
                    "recent": [{"title": clean(t, 80), "at": int(m)} for m, t in recent[:keep]]})
    return out


def _links(cfg, daemon_url: str) -> list:
    out = []
    for ln in cfg.get("links") or []:
        if isinstance(ln, dict) and str(ln.get("url") or "").startswith(("https://", "http://")):
            out.append({"name": clean(ln.get("name") or ln.get("url"), 30), "url": secrets.mask_shapes(str(ln["url"]))[:300]})
    if daemon_url and cfg.get("drover_link", True):
        out.append({"name": "drover", "url": daemon_url.split("?")[0].split("#")[0][:200]})
    return out


@source("knowledge", ttl=900, timeout=25, scope="global", optin=True, defaults=DEFAULTS)
def knowledge(ctx):
    """The shared brain, the search index, reflections per repo, note folders and your wiki links."""
    from . import drover as D
    cfg = ctx.cfg
    dcfg = dict(ctx.host.cfg.source("drover", {}))
    if cfg.get("state_dir"):
        dcfg["state_dir"] = cfg["state_dir"]
    url, tok, _w = D.find_daemon(dcfg, ctx.env)
    data, errors = {}, {}
    jobs = {}
    if url and tok:
        if cfg.get("brain", True):
            jobs["brain"] = lambda: _brain(D, ctx, url, tok, cfg)
        if cfg.get("search", True):
            jobs["search"] = lambda: _search(D, ctx, url, tok)
        if cfg.get("reflections", True):
            jobs["reflections"] = lambda: _reflections(D, ctx, url, tok, cfg)
    elif cfg.get("brain", True) or cfg.get("search", True) or cfg.get("reflections", True):
        errors["drover"] = "no Drover daemon here" if not url else "no Drover token"
    if cfg.get("notes"):
        jobs["notes"] = lambda: _notes(cfg)

    def guarded(fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — one part's failure is its own row
            return e
    res = core.parallel({k: (lambda f=f: guarded(f)) for k, f in jobs.items()}, timeout=20.0)
    for k in jobs:
        v = res.get(k)
        if isinstance(v, Exception) or v is None:
            errors[k] = clean(secrets.redact(str(v), ctx.env) if v is not None else "timed out", 100)
        else:
            data[k] = v
    data["links"] = _links(cfg, url or "")
    if errors:
        data["errors"] = errors
    if not jobs and not data["links"]:
        return {"error": errors.get("drover") or "nothing configured", "errors": errors}

    alerts = []
    b = data.get("brain")
    if b and b["configured"]:
        if not b["ok"]:
            alerts.append({"sev": "warn", "msg": "brain unreachable or unhealthy (HTTP %s)" % (b["status"] or "?"),
                           "fix": "drover op brain.status"})
        elif not b["entries"]:
            alerts.append({"sev": "warn", "msg": "brain index is empty: brain search finds nothing", "fix": "drover op brain.status"})
        elif b["stale"]:
            alerts.append({"sev": "info", "msg": "brain index is stale (a reindex is due)"})
    s = data.get("search")
    hours = float(cfg.get("index_stale_hours") or 0)
    if s and s["enabled"] and hours and s["newest"] and time.time() - s["newest"] > hours * 3600:
        alerts.append({"sev": "info", "msg": "search index newest chunk is %s old" % core.human_age(time.time() - s["newest"]),
                       "fix": "drover op search.reindex"})
    if alerts:
        data["alerts"] = alerts
    return data

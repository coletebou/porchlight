"""Todos: open items from every system you track work in, in one cached list.

One source, several providers. Each provider is on only when its settings say so,
reads with its own timeout, and fails alone (its error shows in the panel; the
others still render). The collector only ever reads. Writing (marking an item
done) happens in `porchlight todos done <key>` or the interactive dashboard,
behind `actions = true` and a confirmation, through `act()` below.

  files       todo.txt-style files: one item per line, "x " marks done
  github      issues from a GitHub search query (GITHUB_TOKEN / GH_TOKEN, or `gh`)
  drover      Drover features that are not shipped or abandoned (the daemon's main token)
  reflections open Drover reflections (actions, questions) across repos (main token)
  badland     Badland todos (GET /api/mcp-context/tasks with a read-scoped key)
  command     any command that prints a JSON list of items

Ask tracking: set `ask_pattern` (a regex such as "V-\\d{2,3}") and `ask_systems`
(the providers every ask should appear in). The source then groups items by the
ask ids their titles carry and reports, per ask, which systems have it and which
do not: the dashboard for a "record every ask everywhere" habit.

Every title is cleaned and masked by pattern (token shapes, URI credentials)
before it is stored. Records never hold a credential: tokens travel only in
request headers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from .. import core, secrets
from ..registry import source

PROVIDERS = ("files", "github", "drover", "reflections", "badland", "command")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_OPEN_FEATURE = ("not-started", "in-progress")
_PRIO = {"urgent": 1, "high": 2, "medium": 3, "normal": 3, "low": 4, "none": 5}

DEFAULTS = {
    "files": [],
    "github_query": "",            # e.g. "is:open is:issue label:voice-gateway user:octocat"
    "github_api": "https://api.github.com",
    "github_token_env": ["GITHUB_TOKEN", "GH_TOKEN"],   # the first one set is used; else `gh` and its own login
    "drover": False,
    "drover_states": list(_OPEN_FEATURE),
    "reflections": False,
    "reflections_kinds": ["action", "question"],
    "badland": False,
    "badland_url": "https://api.badland.ai",
    "badland_key_env": "BADLAND_BRAIN_KEY",       # read: a key with an mcp:brain / context-read scope
    "badland_write_key_env": "BADLAND_API_KEY",   # actions only: the full todo API
    "command": "",
    "done_command": "",            # actions for command items: {id} is replaced, run under /bin/sh -c
    "exclude_repos": [],           # drop items from these repos (keep work items off a personal box)
    "limit": 60,                   # items kept per provider
    "ask_pattern": "",
    "ask_systems": [],
    "actions": False,              # allow `porchlight todos done` and the interactive dashboard to write
    "state_dir": "",               # Drover's state dir (default: the drover source's setting, else ~/.local/state/drover)
    "overdue_alert": True,
    "shown": 6,                    # rows the TODOS panel shows (the rest: porchlight todos, or porchlight -i)
}


def clean(v, n: int = 160) -> str:
    """One display line: control characters out, whitespace collapsed, token shapes and
    URI credentials masked (by pattern, never by matching secret values), then cut."""
    s = " ".join(_CONTROL.sub(" ", str(v if v is not None else "")).split())
    return secrets.mask_shapes(s)[:n]


def _prio(v) -> int:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return max(1, min(5, int(v)))
    return _PRIO.get(str(v or "").lower(), 5)


def _epoch(v) -> int:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    try:
        return int(core.iso_epoch(v) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def item(provider, ident, title, url="", status="open", prio=5, due=0, repo="", labels=(), kind="", updated=0, ref=None,
         match=""):
    """One todo in the shape every provider returns. `match` is extra text (a feature's notes,
    a reflection's body) searched for ask ids and then dropped: it is never stored."""
    ident = clean(ident, 80)
    out = {"key": "%s:%s" % (provider, ident), "provider": provider, "id": ident, "title": clean(title),
           "url": clean(url, 300) if str(url or "").startswith(("https://", "http://")) else "",
           "status": clean(status, 20) or "open", "prio": _prio(prio), "due": _epoch(due), "repo": clean(repo, 60),
           "labels": [clean(x, 30) for x in list(labels or [])[:8]], "kind": clean(kind, 20), "updated": _epoch(updated)}
    if ref:
        out["ref"] = ref
    if match:
        out["_match"] = str(match)[:4000]
    return out


# ── providers ──────────────────────────────────────────────────────────────────
def p_files(ctx, cfg):
    out = []
    for path in cfg.get("files") or []:
        p = core.expand(str(path))
        text = core.read_text(p, 1 << 20)
        if not text and not os.path.exists(p):
            raise RuntimeError("%s not found" % path)
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("x "):
                continue
            prio, title = 5, s
            m = re.match(r"^\(([A-Z])\)\s+(.*)$", s)
            if m:
                prio, title = min(5, ord(m.group(1)) - 64), m.group(2)
            due = re.search(r"\bdue:(\d{4}-\d{2}-\d{2})\b", title)
            labels = re.findall(r"(?:^|\s)[+@]([\w.-]+)", title)
            out.append(item("files", hashlib.sha1(s.encode("utf-8")).hexdigest()[:10], title, prio=prio,
                            due=(due.group(1) + "T00:00:00Z") if due else 0, labels=labels,
                            ref={"path": str(path), "line": s}))
    return out


def _gh_token(env, cfg=None) -> str:
    names = (cfg or {}).get("github_token_env") or ["GITHUB_TOKEN", "GH_TOKEN"]
    for n in [names] if isinstance(names, str) else names:
        if env.get(str(n)):
            return env[str(n)]
    return ""


def p_github(ctx, cfg):
    q = str(cfg.get("github_query") or "").strip()
    limit = max(1, min(100, int(cfg.get("limit") or 60)))
    tok = _gh_token(ctx.env, cfg)
    rows, why = None, "no GitHub token (github_token_env)"
    if tok:
        import urllib.parse
        url = "%s/search/issues?per_page=%d&sort=updated&q=%s" % (str(cfg.get("github_api")).rstrip("/"), limit,
                                                                urllib.parse.quote(q))
        r = ctx.http(url, headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"}, timeout=10)
        if r.ok and isinstance(r.body, dict):
            rows = r.body.get("items") or []
        elif r.status in (401, 403):
            why = "GitHub search HTTP %s (token rejected)" % r.status  # a stale token: try gh's own login
        else:
            raise RuntimeError("GitHub search HTTP %s" % r.status)
    if rows is None:
        gh = core.which("gh")
        if not gh:
            raise RuntimeError(why + " and no gh")
        rc, out, err = ctx.run([gh, "api", "-X", "GET", "search/issues", "-f", "q=" + q, "-f", "per_page=%d" % limit,
                                "-f", "sort=updated"], timeout=15)
        if rc != 0:
            raise RuntimeError("%s; gh api: %s" % (why, clean(err or "exit %d" % rc, 80)) if tok else
                               "gh api: %s" % clean(err or "exit %d" % rc, 120))
        rows = (json.loads(out or "{}") or {}).get("items") or []
    out = []
    for it in rows[:limit]:
        if not isinstance(it, dict):
            continue
        repo = "/".join(str(it.get("repository_url") or "").split("/")[-2:])
        num = it.get("number")
        labels = [lb.get("name") for lb in it.get("labels") or [] if isinstance(lb, dict)]
        prio = 2 if any(re.search(r"\b(p0|p1|urgent|high)\b", str(lb), re.I) for lb in labels) else 4
        out.append(item("github", "%s#%s" % (repo, num), it.get("title"), url=it.get("html_url"), repo=repo,
                        labels=labels, prio=prio, updated=it.get("updated_at"),
                        kind="pr" if it.get("pull_request") else "issue",
                        ref={"repo": repo, "number": num}))
    return out


def _drover(ctx, cfg):
    from . import drover as D
    dcfg = dict(ctx.host.cfg.source("drover", {}))
    if cfg.get("state_dir"):
        dcfg["state_dir"] = cfg["state_dir"]
    url, main, _w = D.find_daemon(dcfg, ctx.env)
    if not url:
        raise RuntimeError("no Drover daemon here")
    if not main:
        raise RuntimeError("no Drover token")
    return D, url, main


def p_drover(ctx, cfg):
    D, url, tok = _drover(ctx, cfg)
    res, why = D.op(ctx, url, tok, "events.state", {}, timeout=12)
    if not isinstance(res, dict):
        raise RuntimeError("events.state: %s" % (why or "no answer"))
    states = set(cfg.get("drover_states") or _OPEN_FEATURE)
    out = []
    for f in res.get("features") or []:
        if not isinstance(f, dict) or f.get("state") not in states:
            continue
        fid = f.get("fid")
        out.append(item("drover", fid, f.get("title"), status=f.get("state"), repo=f.get("repo"),
                        labels=[x for x in (f.get("links") or []) if isinstance(x, str)][:8],
                        prio=3 if f.get("state") == "in-progress" else 4, updated=f.get("updated"),
                        kind="feature", ref={"fid": fid}, match=f.get("notes") or ""))
    out.sort(key=lambda i: -i["updated"])
    return out


def p_reflections(ctx, cfg):
    D, url, tok = _drover(ctx, cfg)
    kinds = ",".join(str(k) for k in cfg.get("reflections_kinds") or [])
    args = {"all": True, "status": "open", "limit": int(cfg.get("limit") or 60)}
    if kinds:
        args["kind"] = kinds
    res, why = D.op(ctx, url, tok, "reflections.list", args, timeout=12)
    if not isinstance(res, dict):
        raise RuntimeError("reflections.list: %s" % (why or "no answer"))
    out = []
    for r in res.get("items") or []:
        if not isinstance(r, dict):
            continue
        out.append(item("reflections", r.get("id"), r.get("title"), status=r.get("status"), repo=r.get("repo"),
                        prio=r.get("priority") or 5, kind=r.get("kind"), labels=r.get("tags") or [],
                        updated=r.get("ts") or r.get("updated"), ref={"id": r.get("id")},
                        match=r.get("body") or ""))
    return out


def p_badland(ctx, cfg):
    key = ctx.env.get(str(cfg.get("badland_key_env") or "BADLAND_BRAIN_KEY")) or ""
    if not key:
        raise RuntimeError("no %s" % cfg.get("badland_key_env"))
    base = str(cfg.get("badland_url") or "").rstrip("/")
    r = ctx.http("%s/api/mcp-context/tasks?status=open&limit=%d" % (base, max(1, min(100, int(cfg.get("limit") or 60)))),
                 headers={"Authorization": "Bearer " + key}, timeout=10)
    if not r.ok or not isinstance(r.body, dict):
        raise RuntimeError("tasks HTTP %s" % r.status)
    out = []
    for t in ((r.body.get("data") or {}).get("items") or []):
        if not isinstance(t, dict) or t.get("isCompleted"):
            continue
        out.append(item("badland", t.get("id"), t.get("title"), prio=t.get("priority"), due=t.get("dueDate"),
                        labels=[x for x in (t.get("tags") or []) if isinstance(x, str)],
                        repo=t.get("list") if isinstance(t.get("list"), str) else "", kind="todo",
                        ref={"id": t.get("id")}, match=t.get("description") or ""))
    return out


def p_command(ctx, cfg):
    cmd = cfg.get("command")
    argv = cmd if isinstance(cmd, list) else ["/bin/sh", "-c", str(cmd)]
    rc, out, _err = ctx.run([str(a) for a in argv], timeout=15, env=secrets.child_env(ctx.env))
    if rc != 0:
        raise RuntimeError("command exit %d" % rc)
    rows = json.loads(out or "[]")
    if isinstance(rows, dict):
        rows = rows.get("items") or []
    return [item("command", r.get("id") or hashlib.sha1(str(r.get("title")).encode()).hexdigest()[:10], r.get("title"),
                 url=r.get("url"), status=r.get("status") or "open", prio=r.get("priority") or 5, due=r.get("due"),
                 repo=r.get("repo"), labels=r.get("labels") or [], kind=r.get("kind") or "",
                 ref={"id": r.get("id")}) for r in rows if isinstance(r, dict) and r.get("title")]


FETCH = {"files": p_files, "github": p_github, "drover": p_drover, "reflections": p_reflections,
         "badland": p_badland, "command": p_command}


def enabled(cfg) -> list:
    on = []
    if cfg.get("files"):
        on.append("files")
    if str(cfg.get("github_query") or "").strip():
        on.append("github")
    for name in ("drover", "reflections", "badland"):
        if cfg.get(name) is True:
            on.append(name)
    if cfg.get("command"):
        on.append("command")
    return on


def asks_of(title: str, pattern) -> list:
    return sorted(set(pattern.findall(title or ""))) if pattern else []


def coverage(items, pattern, systems) -> dict:
    """{ask id: {"in": [providers], "missing": [expected providers without it], "open": n}}."""
    asks = {}
    for it in items:
        for a in it.get("asks") or []:
            row = asks.setdefault(a, {"in": set(), "open": 0})
            row["in"].add(it["provider"])
            row["open"] += 1
    out = {}
    for a, row in sorted(asks.items(), key=lambda kv: _ask_sort(kv[0])):
        have = sorted(row["in"])
        out[a] = {"in": have, "missing": [s for s in systems if s not in row["in"]], "open": row["open"]}
    return out


def _ask_sort(a: str):
    m = re.search(r"(\d+)$", a)
    return (re.sub(r"\d+$", "", a), int(m.group(1)) if m else 0)


@source("todos", ttl=300, timeout=25, scope="global", optin=True, defaults=DEFAULTS)
def todos(ctx):
    """Open todos from files, GitHub issues, Drover features and reflections, Badland, a command."""
    cfg = ctx.cfg
    on = enabled(cfg)
    if not on:
        return {"error": "no providers configured", "items": [], "providers": {}}
    prev = ctx.prev or {}
    prev_items = {}
    for it in prev.get("items") or []:
        if isinstance(it, dict):
            prev_items.setdefault(it.get("provider"), []).append(it)
    def guarded(n):
        try:
            return FETCH[n](ctx, cfg)
        except Exception as e:  # noqa: BLE001 — one provider's failure is its own row, not the source's
            return e
    jobs = {name: (lambda n=name: guarded(n)) for name in on}
    t0 = time.monotonic()
    results = core.parallel(jobs, timeout=20.0)
    excl = {str(r).lower() for r in cfg.get("exclude_repos") or []}
    try:
        pattern = re.compile(str(cfg.get("ask_pattern"))) if cfg.get("ask_pattern") else None
    except re.error:
        pattern = None
    limit = int(cfg.get("limit") or 60)
    items, provs = [], {}
    for name in on:
        val = results.get(name)
        if isinstance(val, list):
            got = [i for i in val if (i.get("repo") or "").lower() not in excl][:limit]
            provs[name] = {"ok": True, "n": len(got)}
        else:
            # A failed read keeps the provider's last items, marked kept, so one outage does not empty the list.
            err = clean(secrets.redact(str(val), ctx.env) if isinstance(val, Exception) else "timed out", 120)
            got = prev_items.get(name, [])
            provs[name] = {"ok": False, "n": len(got), "error": err or "failed", "kept": bool(got)}
        items += got
    for it in items:
        extra = it.pop("_match", "")
        found = asks_of("%s %s %s" % (it.get("title"), " ".join(it.get("labels") or []), extra), pattern)
        # A kept item (its provider failed this run) has no body to search again: keep what it had.
        it["asks"] = sorted(set(found) | set(it.get("asks") or [])) if pattern else []
    now = core.now()
    items.sort(key=lambda i: (i["due"] == 0, i["due"] or 0, i["prio"], -(i["updated"] or 0)))
    data = {"items": items, "providers": provs, "total": len(items), "ms": int((time.monotonic() - t0) * 1000),
            "overdue": sum(1 for i in items if i["due"] and i["due"] < now)}
    systems = [str(s) for s in cfg.get("ask_systems") or []]
    if pattern:
        data["asks"] = coverage(items, pattern, systems)
        data["ask_systems"] = systems
    alerts = []
    if cfg.get("overdue_alert", True) and data["overdue"]:
        alerts.append({"sev": "warn", "msg": "%d todo%s overdue" % (data["overdue"], "" if data["overdue"] == 1 else "s"),
                       "fix": "porchlight todos"})
    bad = [n for n, p in provs.items() if not p["ok"]]
    if bad and len(bad) == len(provs):
        alerts.append({"sev": "warn", "msg": "todos: every provider failed (%s)" % ", ".join(bad), "fix": "porchlight doctor"})
    if alerts:
        data["alerts"] = alerts
    return data


# ── actions (never called by the collector) ───────────────────────────────────
class ActionError(Exception):
    pass


def find(items, key: str):
    """An item by its full key, or by a unique prefix of the key or of its id."""
    exact = [i for i in items if i.get("key") == key]
    if exact:
        return exact[0]
    cands = [i for i in items if str(i.get("key", "")).startswith(key) or str(i.get("id", "")).startswith(key)]
    if len(cands) == 1:
        return cands[0]
    raise ActionError("no todo %s" % key if not cands else "%s matches %d todos" % (key, len(cands)))


def act(host, it: dict, verb: str, env=None, runner=None, http=None) -> str:
    """Do `verb` ("done") to one item through its own system. Returns what happened, or
    raises ActionError. Only when [sources.todos] actions = true."""
    from ..registry import Ctx
    cfg = host.cfg.source("todos", DEFAULTS)
    if verb != "done":
        raise ActionError("unknown action %s" % verb)
    if cfg.get("actions") is not True:
        raise ActionError("todo actions are off: set [sources.todos] actions = true")
    env = env if env is not None else secrets.load(host.cfg)
    ctx = Ctx(host, env, {}, cfg)
    if runner:
        ctx.run = runner
    if http:
        ctx.http = http
    ref = it.get("ref") or {}
    prov = it.get("provider")
    if prov == "files":
        return _done_file(ref)
    if prov == "github":
        tok = _gh_token(env, cfg)
        repo, num = ref.get("repo"), ref.get("number")
        if tok:
            r = ctx.http("%s/repos/%s/issues/%s" % (str(cfg.get("github_api")).rstrip("/"), repo, num), method="PATCH",
                         json_body={"state": "closed", "state_reason": "completed"},
                         headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"}, timeout=10)
            if not r.ok:
                raise ActionError("GitHub HTTP %s" % r.status)
        else:
            gh = core.which("gh")
            if not gh:
                raise ActionError("no GitHub token (github_token_env) and no gh")
            rc, _out, err = ctx.run([gh, "issue", "close", str(num), "-R", str(repo), "--reason", "completed"], timeout=20)
            if rc != 0:
                raise ActionError("gh issue close: %s" % clean(err or "exit %d" % rc, 120))
        return "closed %s#%s" % (repo, num)
    if prov in ("drover", "reflections"):
        try:
            D, url, tok = _drover(ctx, cfg)
        except RuntimeError as e:
            raise ActionError(str(e)) from None
        if prov == "drover":
            res, why = D.op(ctx, url, tok, "features.upsert", {"fid": ref.get("fid"), "state": "shipped",
                                                                "actor": "porchlight"}, timeout=10)
        else:
            res, why = D.op(ctx, url, tok, "reflections.mark", {"op": "done", "ids": [ref.get("id")],
                                                                 "note": "done from porchlight", "actor": "porchlight"},
                            timeout=10)
        if res is None:
            raise ActionError("drover: %s" % (why or "no answer"))
        return "marked %s done in Drover" % it.get("id")
    if prov == "badland":
        return _done_badland(ctx, cfg, env, ref.get("id"))
    if prov == "command":
        tmpl = str(cfg.get("done_command") or "")
        if not tmpl:
            raise ActionError("set [sources.todos] done_command to complete command items")
        import shlex
        rc, _out, _err = ctx.run(["/bin/sh", "-c", tmpl.replace("{id}", shlex.quote(str(ref.get("id"))))], timeout=20,
                                 env=secrets.child_env(env))
        if rc != 0:
            raise ActionError("done_command exit %d" % rc)
        return "done_command ran for %s" % ref.get("id")
    raise ActionError("no action for provider %s" % prov)


def _done_file(ref) -> str:
    path, line = core.expand(str(ref.get("path") or "")), str(ref.get("line") or "")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError as e:
        raise ActionError("%s: %s" % (ref.get("path"), e.strerror)) from None
    for n, ln in enumerate(lines):
        if ln.strip() == line:
            lines[n] = ln[: len(ln) - len(ln.lstrip())] + "x %s %s" % (time.strftime("%Y-%m-%d"), line)
            tmp = path + ".porchlight.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            try:
                os.chmod(tmp, os.stat(path).st_mode & 0o7777)
            except OSError:
                pass
            os.replace(tmp, path)
            return "marked done in %s" % ref.get("path")
    raise ActionError("the line is no longer in %s" % ref.get("path"))


def _done_badland(ctx, cfg, env, tid) -> str:
    """Badland's endpoint toggles, so confirm the item is still open right before: a toggle on
    an item someone already completed would reopen it."""
    rkey = env.get(str(cfg.get("badland_key_env") or "")) or ""
    wkey = env.get(str(cfg.get("badland_write_key_env") or "")) or ""
    if not wkey:
        raise ActionError("no %s (the todo write key)" % cfg.get("badland_write_key_env"))
    base = str(cfg.get("badland_url") or "").rstrip("/")
    r = ctx.http("%s/api/mcp-context/tasks?status=open&limit=100" % base,
                 headers={"Authorization": "Bearer " + (rkey or wkey)}, timeout=10)
    if not r.ok or not isinstance(r.body, dict):
        raise ActionError("could not re-check the item (HTTP %s)" % r.status)
    still = [t for t in ((r.body.get("data") or {}).get("items") or []) if isinstance(t, dict) and t.get("id") == tid]
    if not still or still[0].get("isCompleted"):
        raise ActionError("already completed (or not among the 100 newest open items); nothing changed")
    r = ctx.http("%s/api/todos/items/%s/toggle" % (base, tid), method="POST", json_body={},
                 headers={"Authorization": "Bearer " + wkey}, timeout=10)
    if not r.ok:
        raise ActionError("toggle HTTP %s" % r.status)
    return "completed in Badland"

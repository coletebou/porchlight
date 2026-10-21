"""[[custom]] sources: any command becomes a source and a panel, no Python needed.

    [[custom]]
    name = "restic"                 # source and panel key
    title = "BACKUP"                # panel title (default: NAME in capitals)
    command = "~/bin/check-backup"  # a string runs under /bin/sh -c; a list is argv
    ttl = 3600

Output formats (format = "auto" picks json when stdout is a JSON object):
  json    {"status": "ok|warn|crit|unknown", "summary": "...",
           "rows": [{"label": "...", "value": "...", "status": "ok"} or "plain text", ...],
           "alerts": [{"sev": "warn", "msg": "..."}], "badge": "3/3"}
  nagios  monitoring-plugins conventions: exit 0 OK, 1 WARNING, 2 CRITICAL,
          3 UNKNOWN; the first line (before any "|" perfdata) is the summary and
          later lines are rows. Every check_* plugin works unchanged.
  lines   every stdout line is a row; status from the exit code as for nagios.
"""
from __future__ import annotations

import json
import re

from .. import core, registry
from ..registry import source

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_NAGIOS = {0: "ok", 1: "warn", 2: "crit"}


def entries(host) -> list:
    out = []
    for i, c in enumerate(host.cfg.items("custom")):
        if not isinstance(c, dict) or not c.get("command"):
            continue
        name = str(c.get("name") or "custom%d" % (i + 1)).lower()
        if not _NAME.match(name) or name in registry.SOURCES and not getattr(registry.SOURCES[name], "custom", False):
            continue  # never shadow a built-in source
        e = dict(c)
        e.update(name=name, panel=str(c.get("panel") or name), title=str(c.get("title") or name.upper()))
        out.append(e)
    return out


def parse(out: str, rc: int, fmt: str = "auto") -> dict:
    text = out.strip()
    if fmt in ("auto", "json") and text.startswith("{"):
        try:
            d = json.loads(text)
        except ValueError:
            d = None
        if isinstance(d, dict):
            st = d.get("status") if d.get("status") in ("ok", "warn", "crit", "unknown") else "ok"
            rows = d.get("rows") if isinstance(d.get("rows"), list) else []
            res = {"status": st, "summary": str(d.get("summary") or "")[:200], "rows": rows[:40]}
            al = [a for a in d.get("alerts") or [] if isinstance(a, dict) and a.get("msg")]
            if al:
                res["alerts"] = [{"sev": a.get("sev", "warn"), "msg": str(a["msg"])[:200]} for a in al[:10]]
            if d.get("badge"):
                res["badge"] = str(d["badge"])[:12]
            return res
    if fmt == "json":
        return {"status": "unknown", "summary": "", "rows": [], "error": "output is not a JSON object"}
    lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    st = _NAGIOS.get(rc, "unknown")
    if fmt == "lines":
        return {"status": st, "summary": "", "rows": lines[:40], "rc": rc}
    first = lines[0].split("|", 1)[0].strip() if lines else ""
    return {"status": st, "summary": first[:200], "rows": lines[1:40], "rc": rc}


def _make(e):
    def fn(ctx):
        cmd = e["command"]
        argv = [core.expand(a) for a in cmd] if isinstance(cmd, list) else ["/bin/sh", "-c", str(cmd)]
        env = dict(ctx.env)
        env.update({str(k): str(v) for k, v in (e.get("env") or {}).items()} if isinstance(e.get("env"), dict) else {})
        rc, out, err = core.run(argv, float(e.get("timeout", 20)), env=env, cwd=core.expand(e["cwd"]) if e.get("cwd") else None)
        if rc in (124, 126, 127):
            raise RuntimeError(err.strip()[:160] or "rc %d" % rc)
        d = parse(out, rc, str(e.get("format", "auto")))
        if not d.get("alerts") and d["status"] in ("warn", "crit") and e.get("alert", True):
            d["alerts"] = [{"sev": d["status"], "msg": "%s: %s" % (e["name"], d.get("summary") or "status %s" % d["status"])}]
        if rc not in (0, 1, 2, 3) and not out.strip():
            raise RuntimeError("exit %d: %s" % (rc, err.strip()[:160]))
        return d
    fn.__doc__ = str(e.get("desc") or "custom: %s" % (e["command"] if isinstance(e["command"], str) else " ".join(e["command"])))[:100]
    return fn


def register(host) -> None:
    from ..ui import BUILDERS, panel
    for e in entries(host):
        source(e["name"], ttl=int(e.get("ttl", 600)), timeout=float(e.get("timeout", 20)),
               scope=str(e.get("scope", "local")), hosts=e.get("hosts"),
               needs=tuple(e.get("needs") or ()), share=bool(e.get("share", False)))(_make(e))
        registry.SOURCES[e["name"]].custom = True
        if e["panel"] not in BUILDERS:
            panel(e["panel"], title=e["title"])(_builder(e))


def _builder(e):
    def build(x):
        from .. import theme as T
        from ..ui import kv
        d = x.data(e["name"])
        if not d:
            return x.panel(e["panel"], [T.dim("waiting for the first run" + T.ellipsis())], "unknown", title=e["title"])
        rows = []
        st = d.get("status", "unknown")
        if d.get("summary"):
            rows.append(T.dot(st) + " " + d["summary"])
        w = int(e.get("label_width", 12))
        for r in d.get("rows") or []:
            if isinstance(r, dict):
                lv = r.get("status")
                val = (T.dot(lv) + " " if lv else "") + str(r.get("value", ""))
                rows.append(kv(str(r.get("label", ""))[:w - 1], val, w))
            else:
                rows.append(str(r))
        return x.panel(e["panel"], rows, st if st in ("warn", "crit") else "ok", x.stale_note(e["name"], 4, 900),
                       title=e["title"])
    return build

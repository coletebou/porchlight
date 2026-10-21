# Custom commands and plugins

Two ways to add your own panels: any command, with no Python, or a Python
plugin with the full API.

## Custom commands

```toml
[[custom]]
name = "restic"                    # source and panel key: [a-z][a-z0-9_-]*
title = "BACKUP"                   # panel title (default: NAME)
command = "~/bin/check-restic"     # a string runs under /bin/sh -c; a list is argv
ttl = 3600                         # seconds between runs
timeout = 20
format = "auto"                    # auto | json | nagios | lines
scope = "local"                    # or "global": the hub runs it and ships the result to peers
hosts = ["#server"]                # optional host filter
needs = ["RESTIC_PASSWORD"]        # env vars that must be present (from secrets files)
env = { RESTIC_REPOSITORY = "b2:acme-backups" }
share = false                      # true: ride along in the fleet snapshot
show = true                        # add the panel to the layout
alert = true                       # warn/crit status raises an alert
```

The command runs in the collector, never at login, with the merged
environment (your shell plus secrets files) and its own timeout.

### JSON

When stdout is a JSON object:

```json
{
  "status": "warn",
  "summary": "last snapshot 26h ago",
  "rows": [
    {"label": "repository", "value": "b2:acme-backups", "status": "ok"},
    "plain text rows work too"
  ],
  "alerts": [{"sev": "warn", "msg": "restic: no snapshot in 26h", "fix": "systemctl start restic-backup"}],
  "badge": "3/3"
}
```

### Nagios plugins

Every existing monitoring plugin works unchanged: exit `0` OK, `1` WARNING,
`2` CRITICAL, `3` UNKNOWN; the first line (before any `|` perfdata) is the
summary, later lines are rows.

```toml
[[custom]]
name = "disk-home"
title = "DISK /home"
command = ["/usr/lib/nagios/plugins/check_disk", "-w", "15%", "-c", "8%", "-p", "/home"]

[[custom]]
name = "web-tls"
command = ["/usr/lib/nagios/plugins/check_http", "-H", "example.com", "-S", "-C", "21,10"]
```

### Lines

`format = "lines"`: every stdout line is a row, status from the exit code.

## Python plugins

Files in `~/.config/porchlight/plugins/*.py` are imported by both the
collector and the renderer (names starting with `_` are skipped; a broken
plugin is reported by `porchlight doctor` and never takes the dashboard down).

```python
from porchlight import source, panel, kv, theme as T


@source("todo", ttl=300, defaults={"path": "~/todo.txt", "warn_at": 20})
def todo(ctx):
    """Open items in my todo file."""
    import os
    path = os.path.expanduser(ctx.cfg["path"])
    items = [l for l in open(path) if l.strip() and not l.startswith("x ")]
    data = {"open": len(items), "top": [l.strip() for l in items[:3]]}
    if len(items) >= ctx.cfg["warn_at"]:
        data["alerts"] = [{"sev": "warn", "msg": "%d open todos" % len(items)}]
    return data


@panel("todo", title="TODO")
def todo_panel(x):
    d = x.data("todo")
    if not d:
        return None
    rows = [kv("open", T.c(str(d["open"]), "fg", bold=True))] + [T.dim("· ") + t for t in d["top"]]
    return x.panel("todo", rows, note=x.stale_note("todo"))
```

Settings come from `[sources.todo]`, merged over `defaults`. With `layout.panels
= "auto"` (the default) plugin panels appear after the built-in ones whenever
they have data; with an explicit list, place them yourself:

```toml
[layout]
panels = ["system", "services", "todo", "agents"]
```

### The source contract

`@source(name, ...)` registers a function that receives a `Ctx` and returns a
JSON-serializable dict. The collector stores it as one record.

| Parameter | Meaning |
|---|---|
| `ttl` | seconds between successful runs |
| `timeout` | the expected worst case; `doctor` flags overshoots. Enforce your own I/O timeouts: threads cannot be killed, and the collector's deadline abandons stragglers |
| `scope` | `"local"` (this machine) or `"global"` (an account: the hub collects it in a fleet) |
| `retry` | seconds before retrying after a failure (default `min(ttl, 180)`) |
| `needs` | env vars that must all be set; otherwise a "missing env" skip, re-checked every 6 h |
| `defaults` | settings, overridable under `[sources.NAME]` |
| `requires` | settings that must be non-empty (`"a|b"`: either) |
| `optin` | off until `[sources.NAME]` exists |
| `binaries` | executables that must be on PATH |
| `hosts` | machine names, `#tags`, or `callable(host)` |
| `identities` | `"*"` or a tuple of identity names |
| `share` | ship the record to the hub in the snapshot |

`ctx.cfg` (settings), `ctx.env` (environment plus secrets), `ctx.prev` (your
previous data: for deltas, two-strike flap guards, history), `ctx.host`
(`name`, `label`, `identity`, `role`, `tags`, `cfg`), `ctx.http(url, ...)`
(returns `status`, parsed `body`, `ms`; does not follow redirects for
credentialed requests), `ctx.run(argv, timeout)` (never raises; `(rc, out,
err)`).

Results:

- `"alerts": [{"sev": "crit"|"warn"|"info", "msg": "...", "fix": "optional command"}]`
  join the attention strip, ranked, deduplicated and marked NEW since the last login.
- Raise to fail hard: the last good data is kept, marked stale, with the error
  (scrubbed of secrets) in `doctor`.
- Return `{"error": "...", "status": "unknown"}` for a *soft* failure (an
  expired login, a missing permission): normal cadence, last good data kept.
- Never put a credential in the result.

### The panel contract

`@panel(key, title)` registers a builder `fn(x) -> Panel | None`. `x` is the
render context: `x.data(name)`, `x.cw` (column width), `x.v` (live vitals),
`x.lv` (all live probes), `x.alerts`, `x.cfg`, `x.host`, `x.stale_note(name)`
("12m" when older than expected, "✖ 3h" after a failure), and
`x.panel(key, rows, level, note)`. Helpers: `kv(label, value, width)`,
`cells(items, width)` (as many columns as fit), `SEP`, and the `theme` module
(`c`, `dim`, `dot`, `sev`, `bar`, `pad`, `fit`, `width`, `g`, `worst`,
`level_for`). Builders read the cache only, never the network; lines are
truncated to the column, ANSI-aware.

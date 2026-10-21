# Architecture

porchlight splits into two programs that share a cache:

```
                 ┌──────────────── porchlight collect ────────────────┐
 timer (5 min) ─►│ flock: one collector per machine                    │
 login spawn  ─► │ due sources run in parallel threads, each with its │
                 │ own TTL, I/O timeouts and failure isolation; a run  │
                 │ deadline abandons stragglers (os._exit)             │
                 └──────┬──────────────────────────────────────────────┘
                        ▼ one JSON record per source (0600, write-then-rename)
            ~/.cache/porchlight/src/<source>.json · snapshot.json · hub/bundle.json
                        ▲
                 ┌──────┴──────────── porchlight (login) ─────────────┐
 login shell ──► │ reads records + bundle, runs cheap live probes in   │
                 │ parallel (vitals, units, launchd, tmux, ports, git) │
                 │ → alerts → panels → layout → ~10-40 ms              │
                 │ stale data? spawn one background collector          │
                 └─────────────────────────────────────────────────────┘
```

## Records

```json
{"v": 1, "name": "weather", "host": "atlas", "ts": 1792600000.1, "ms": 214,
 "ok": true, "data": {...}, "last_ok": 1792600000.1}
```

- `ok: false` with `error`: the run raised. `data` is the last good data (the
  panel shows it with a "✖ 3h" stale note), `fails` counts consecutive failures,
  and the next attempt comes after `retry` seconds.
- `soft: true`: the source reported its own failure (`{"error", "status":
  "unknown"}`, or `{"error", "windows": []}`): an expired login, a missing
  permission. Normal cadence; the last good reading is kept and marked `kept`.
- `skip: true`: a variable in `needs` is missing. Re-checked every 6 hours.

## Scheduling

A source is due when its record is older than `ttl` (after success or a soft
failure) or `retry` (after a hard failure). A *global* source on a machine
that is not the hub is never due while the hub's copy in `bundle.json` is
younger than `fallback` (default `max(3 × ttl, 45 min)`).

The `fleet` source runs last in each collection, after this run's records and
`snapshot.json` are written, so the bundle it pushes is fresh.

## Rendering

1. `alerts.probe_all`: every live probe in parallel threads (procfs/statvfs on
   Linux, `sysctl`/`vm_stat`/`launchctl` on macOS, `systemctl show` once per
   scope, `tmux`, TCP connects to your ports, `git status --porcelain=v2 -uno`).
   Disks are statted in daemon threads with a 0.8 s budget.
2. `alerts.gather`: thresholds on the vitals, failed units, down units and
   launchd jobs, every applicable source's `alerts`, and peers' critical alerts
   from the fleet table; ranked crit → warn → info, deduplicated, filtered by
   `alerts.ignore`, and marked NEW against the previous login's set (digits
   folded, so "swap 89%" → "swap 90%" is not new).
3. Panels: each builder reads the cache and returns a `Panel` or `None`. A
   builder that raises renders as a one-line error panel; the rest of the
   dashboard is unaffected.
4. Layout: masonry columns (the shortest column takes the next panel), every
   line fitted to its column with ANSI-aware width math (wide emoji, variation
   selectors, combining marks), then the header (banner beside the facts) and
   the attention strip above, the footer below.

## The cache picks the best copy

For each source the renderer considers the local record, the hub's copy in
the bundle, and copies shared in peers' snapshots, and picks: successful
first, then without a soft error, then newest. That is what keeps a peer's
"token expired" from shadowing the hub's live numbers.

## Files

| Path | Written by | Contents |
|---|---|---|
| `src/<name>.json` | collector | one record per source |
| `snapshot.json` | collector | this machine's vitals, agents, top alerts, badge, shared records |
| `hub/bundle.json` | the hub (over ssh) | the hub's global records and the fleet table |
| `state/last_visit.json` | login render | the previous login's time and alert set |
| `state/last_spawn` | login render | rate limit for background collectors |
| `state/banner-*.txt` | renderer | cached figlet/chafa output |
| `collect.log` | collector | one line per run (rotates at 256 KB) |
| `collect.lock` | collector | the flock |

## Code map

| Module | Role |
|---|---|
| `cli.py` | commands and flags |
| `config.py` | TOML loading (tomllib, or the vendored tomli on 3.9/3.10), defaults, `[hosts.*]` merging, validation |
| `hosts.py` | this machine's profile: name, label, identity, role, tags, what to show |
| `registry.py` | `@source`, identities and `state()`, the `Cache` view, plugin loading, settings checks |
| `collect.py` | the collector |
| `live.py` | render-time probes |
| `alerts.py` | one ranked alert list |
| `snapshot.py` | the compact status a hub pulls |
| `render.py` | header, attention strip, grid, footer |
| `ui.py` | `Panel`, the render context, `@panel`, layout helpers |
| `theme.py` | palettes, color depth, gradients, meters, width math, glyphs, ASCII mode |
| `font.py` | built-in banner fonts, figlet |
| `secrets.py` | secrets files and commands, redaction |
| `sources/*` | the built-in sources |
| `panels/*` | the built-in panels |
| `demo.py`, `wizard.py`, `install.py`, `top.py`, `doctor.py` | the other commands |

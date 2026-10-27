# Drover

[Drover](https://github.com/coletebou/drover) is a control plane for coding
agents: it runs Claude Code, Codex and other agents in tmux and headless sessions
across a fleet of machines, and its daemon answers small JSON operations over
HTTP. The `drover` source reads this machine's daemon, and the DROVER panel
shows what it sees on every host:

```text
 DROVER ● ───────────────────────── 3 hosts · 5 working · 4 idle
 waiting  ⏸ demo-review atlas 7m · Push the branch now, or wait
            for the second review?
          ⏸ demo-migrate gpu-box 1m · Run the migration on the
            staging database? 1. Yes / 2. Yes, and do not ask…
 done     ✔ demo-docs web-1 12m · Done: the README section is…
          ✔ demo-build atlas 40m · Pushed 3 commits to…
 crashed  ✖ demo-bench gpu-box 1h · exited unexpectedly
 working  ▶ atlas 3  demo-tests, demo-api, #5ffd86aa
          ▶ gpu-box 1  demo-train
          ▶ web-1 1  demo-lint
 daemons  ● 2/3 live · gpu-box needs a restart · manager ●
 pool     main 3/12 · backup ✖ BACKUP_API_TOKEN unset
```

| Row | Shows |
| --- | --- |
| waiting | the sessions waiting on you, with the question each one asks (wrapped onto a second line between words) |
| done | sessions that finished since your last visit, in bright text; with none, the last day's, dimmed |
| crashed | sessions that exited unexpectedly, and hosts that stopped answering, since your last visit or in the last six hours |
| working | the sessions at work, per host, this machine first |
| daemons | which Drover commit each host's daemon runs, and which ones need a restart to load their checkout (fleet hub) |
| pool | the credential pool Drover spreads launches over: sessions out of each entry's limit, recent throttles, and the variable an unavailable entry is missing (fleet hub) |

`local` in Drover's answers is this machine, shown by its label; `[fleet] labels`
shorten the other host names. A session whose name Drover redacted (a name that
reads like a token comes through as `[redacted]`) shows its short id
(`#5ffd86aa`). The note is the totals, or how old the reading is when the daemon
has not answered for a while. With no daemon on the machine the panel is hidden;
with one that does not answer it says so, dimmed, with `drover serve`.

## What it reads

The daemon's state directory holds `daemon.json` (its URL) and two token files,
`token` (the main token) and `widget-token` (read-only: the daemon accepts it
only for `native.summary` and `native.notifications`, and answers 403 to anything
else). The source sends four operations, each a `POST <url>api/op/<op>`:

| Operation | Token | When | For |
| --- | --- | --- | --- |
| `native.summary` | the widget token, else the main token | every run | who waits, who works, per-host counts, the manager |
| `native.notifications` | the widget token, else the main token | every run, from the last cursor | sessions that finished or crashed |
| `fleet.versions` | the main token | on the fleet hub, every `recheck_minutes` | each daemon's commit and state |
| `credentials.status` | the main token | on the fleet hub, every `recheck_minutes` | the credential pool |

All four only read. Because the native operations go out with the read-only
widget token whenever one exists, nothing this source sends can message or steer
a session.

The source keeps the finish feed itself: Drover's own alert ring is short, so the
record is what still knows about a session that finished before your last visit.
It asks only for what is new since the stored cursor, keeps one entry per session
and kind (its latest), a week of history and twenty entries at most.

A daemon that does not answer (not running, or slower than six seconds) is shown
as it is, like a container engine that is not running: the last board is not
passed off as current, and its waiting alert goes with it. The feed and its
cursor are kept for the daemon's return.

```toml
[sources.drover]
state_dir = ""          # default: $DROVER_STATE, else ~/.local/state/drover
versions = "hub"        # fleet.versions: "hub" (the fleet hub only), true (every machine) or false
pool = "hub"            # credentials.status: the same choices
recheck_minutes = 10    # how often those two are read
```

The source is `scope = "local"`: every machine reads its own daemon, and one
without a daemon shows nothing. When only the hub runs Drover, set `scope =
"global"` and the hub ships its record to the peers in the bundle
([fleet.md](../fleet.md)).

## Tokens and text

- A token only ever travels in the `Authorization` header of a request to the
  URL in `daemon.json`, and is never stored, logged or put in argv. A
  credentialed request never follows a redirect.
- Only what the panel shows is kept: names, questions, counts, short commits and
  the names of missing variables. Paths, the credential pool's environment map
  and its state file are dropped.
- Drover already strips escapes, paths and secrets from what the widget token
  sees. Porchlight removes control characters once more and masks anything
  token-shaped or a credential in a URI, **by pattern only**. It never matches
  secret values from your environment against Drover's text, as it does for
  error messages: a session named after a host whose name happens to be a
  password would otherwise show `<redacted>` exactly where that word is, telling
  whoever reads the screen which secret it is.

## Alerts

| Severity | When |
| --- | --- |
| warn | agents are waiting on you: `demo-review@web-1 is waiting on you: Push the branch now, or wait for the second review?`, or `3 agents waiting on you (…)` for several |

Deliberately not alerts: a daemon that does not answer (shown, dimmed), sessions
that finished or crashed (shown in the panel), daemons that need a restart (the
panel turns amber), an unavailable credential-pool entry.

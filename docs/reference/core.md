# Host sources

The sources that describe the machine itself and the things you run on it.
Every setting lives under `[sources.<name>]`; the values shown are the
defaults. Any source also accepts the common settings:

```toml
[sources.NAME]
enabled = true            # false turns it off here; opt-in sources need a table or true
identities = ["personal"] # only on machines of these identities (default: every identity)
hosts = ["#server"]       # only on these machine names or #tags
```

`porchlight sources` lists every source with its state on this machine (on,
or why not), and `porchlight doctor` shows each one's freshness and last error.

| Source | Scope | Default cadence | Shows |
|---|---|---|---|
| [`procs`](#procs) | local | 2 min | AI coding agents, orphans, MCP servers, zombies, ssh tunnels, top memory, watched processes |
| [`tailscale`](#tailscale) | local | 5 min | backend state, this node's IP, peers online, key expiry |
| [`repos`](#repos) | local | 2 min | untracked files in your configured repos (branch and dirty state are live) |
| [`weather`](#weather) | global | 30 min | current conditions and today's range for the header |
| [`quotes`](#quotes) | global | 7 days | the footer's quote pool |
| [`sites`](#sites-and-health) | global | 5 min | public HTTP endpoints: status and latency |
| [`health`](#sites-and-health) | local | 2 min | this machine's own health routes |
| [`certs`](#certs) | local | 6 h | TLS certificate expiry and validity |
| [`backups`](#backups) | local | 10 min | backup jobs: systemd results, timer cadence, file freshness |
| [`files`](#files) | local | 30 min | size and line budgets for files that must stay small |
| [`drift`](#drift) | local | 10 min | deployed revision vs a git ref |
| [`mongodb`](#mongodb) | local | 15 min | MongoDB ping, primary, dbStats (opt-in) |
| [`fleet`](../fleet.md) | hub | 4 min | swaps the bundle and snapshots with every peer |
| [`[[custom]]`](../plugins.md#custom-commands) | either | you choose | any command's JSON, lines or Nagios-plugin output |

*Global* sources describe an account or the whole fleet. On a standalone
machine they run like any other; in a [fleet](../fleet.md) only the hub runs
them and peers render the hub's copy.

Live probes that run at render time (no source, no cache): CPU load, memory,
swap and disks (with a guard against hung network mounts), systemd units and
failed units, launchd jobs, tmux, your listening `ports`, and the tracked state
of your `repos`.

---

## procs

A census of every process, once per run (`/proc` on Linux, one `ps` on macOS).

```toml
[sources.procs]
agents = ["claude", "codex", "aider", "gemini", "opencode", "goose", "cursor-agent", "amp", "copilot", "qwen", "crush", "grok"]
extra_agents = {}        # name = "regex" for agents porchlight does not know yet
orphan_age = 300         # seconds without a terminal, reparented to init, before a process counts as orphaned
orphan_alert = 1         # alert at this many orphans per agent (0 = never)
mcp_orphan_alert = 5     # orphaned MCP servers (playwright-mcp, *-mcp-server, @modelcontextprotocol/*)
zombie_alert = 25
watch = []               # processes that must be running, see below
```

- **Agents** are matched on the command before its first flag, plus the first
  three words for interpreter launches (`python3 -m aider`, `node …/claude`).
  Desktop apps (`Claude.app`) are not CLI agents. Codex is split by kind:
  `app-server`, `exec`, `mcp-server`, `proto`, `login`, `resume`, else `tui`.
- **Orphans**: no controlling terminal, parent is init/launchd, older than
  `orphan_age`. A Codex app-server is never an orphan (they are short-lived
  readers by design). The alert carries a `kill` command as its fix.
- **Tunnels**: every `ssh`/`autossh` with `-L`, `-R` or `-D`, with its ports and
  host. The NETWORK panel re-checks each PID at render, and a `-D` SOCKS proxy is
  what [`reach`](dev.md) uses to get through when you are off the VPN.
- **Watch** processes that must exist:

  ```toml
  watch = [{ name = "worker", match = "queue-worker --prod", min = 2, sev = "crit" }]
  ```

  `match` is a substring of the full command line. Fewer than `min` alerts at
  `sev` (default `warn`); the count shows in SERVICES.

## tailscale

`tailscale status --json`, read locally. On WSL with mirrored networking the
Windows `tailscale.exe` answers (WSL shares the Windows node). On macOS the App
Store build's CLI is found too.

```toml
[sources.tailscale]
required = false         # true: crit alert when the backend is not Running
expiry_warn_days = 14    # warn before this node's or a peer's key expires
```

`status --json` exits 0 even when tailscaled is stopped, so the verdict comes
from `BackendState`. The fleet table trusts "offline" only when a peer has also
not been seen for 10 minutes: the `Online` bit flaps during control-plane
reconnects.

## repos

Lists the untracked files of every path in the top-level `repos` setting.
Tracked state (branch, modified files, ahead/behind, last commit) is read live
at render with `git status --porcelain=v2 -uno`; untracked scanning can take
seconds in a large tree, so it lives here.

```toml
repos = ["~/code/webapp", "~/code/infra"]   # top level, before any [table]
```

## weather

Providers are tried in order and the first answer wins:

| Provider | Needs | Notes |
|---|---|---|
| `open-meteo` | latitude, longitude | current conditions, feels-like, wind, humidity, today's high/low, rain chance, sunrise/sunset |
| `nws` | latitude, longitude (US) or `nws_station` | the US National Weather Service's latest observation; the nearest station is looked up once and remembered |
| `wttr` | nothing | wttr.in; without a location it locates you from your IP |

```toml
[sources.weather]
latitude = 40.71
longitude = -74.01
place = ""               # a label for your own reference
units = "imperial"       # or "metric"
providers = []           # default: open-meteo, nws, wttr with coordinates; wttr alone without
wttr_location = ""       # e.g. "Paris" (default: your coordinates, else IP lookup)
nws_station = ""         # e.g. "KNYC"
user_agent = ""          # NWS asks for contact details: "(myapp, you@example.com)"
timezone = "auto"        # Open-Meteo's timezone for sunrise/sunset
```

Leave a provider out of `providers` to never call it. The header says "(3h
ago)" when the reading is more than two hours old instead of passing it off
as current.

## quotes

A pool of up to `pool` short quotes for the footer, refreshed weekly: zenquotes
(50 per call, a short pause between calls), then `fortune -s` if installed.
A pool thinner than 10 quotes keeps the previous one. With the source off, or
before its first run, the footer uses `footer.quotes_file` or the bundled
public-domain list.

```toml
[sources.quotes]
zenquotes = true
fortune = true
pool = 250
```

## sites and health

The same probe, two scopes: `sites` for public endpoints (collected by the hub
in a fleet) and `health` for this machine's own routes.

```toml
[sources.sites]
targets = [
  { label = "blog", url = "https://example.com" },
  { label = "api", url = "https://api.example.com/health", show = "version", format = "v{}" },
  { label = "admin", url = "https://admin.example.com", expect = [200, 302] },
]
strikes = 2      # consecutive failures before alerting: a deploy restarts things
slow_ms = 1500   # slower than this renders amber

[sources.health]
targets = [{ label = "api", url = "http://127.0.0.1:8000/healthz", contains = "ok" }]
```

Per target: `url`, `label`, `expect` (a status or list; default: anything
below 500 is up), `strict` (only 2xx/3xx), `contains` (text the body must
hold), `show` (a dotted JSON path appended to the cell, e.g. `build.sha`),
`format` (`{}` is the value), `timeout`, `verify` (TLS verification, default
true, and a bad certificate reports as `tls error`, never silently passes),
`follow` (follow redirects; off by default, so a 302 to a login page is "up").

Probe a cheap route. A health endpoint that fans out to other services turns a
slow dependency into a false outage and costs those services a call per probe.

## certs

```toml
[sources.certs]
targets = ["example.com", { name = "api.example.com", connect = "127.0.0.1", port = 443 }]
warn_days = 21
crit_days = 10
```

A full TLS handshake with verification, so an expired, self-signed or
wrong-name certificate is `invalid: …` (crit). `connect` checks the
certificate your own server presents for a name (useful behind a CDN that
terminates TLS in front of it).

## backups

Judge the service, not its timer: a systemd timer stays green while every run
of its service fails.

```toml
[sources.backups]
units = ["restic-home", { unit = "pg-dump", label = "db", every_hours = 24 }]
files = [{ label = "nightly", path = "/var/backups/nightly-*.tar.zst", max_age_hours = 26 }]
```

- `units`: the service's last `Result` (anything but success is crit, with a
  `journalctl` fix hint) and its timer's cadence: a run older than 1.5× the
  interval plus an hour is overdue (warn). `every_hours` sets the interval for
  services started some other way.
- `files`: the newest match of a glob must be younger than `max_age_hours`
  (cron jobs, NAS dumps, anything that leaves a file).

## files

Size budgets for files that silently stop working when they grow: agent
context files, memory indexes, generated configs.

```toml
[sources.files]
targets = [
  { path = "~/.claude/CLAUDE.md", max_bytes = 40000 },
  { glob = "~/notes/*/MEMORY.md", max_bytes = 24000, max_lines = 190, warn_pct = 90 },
]
```

Warns at `warn_pct` of either budget, crit (or `sev`) once over it. The FILES
panel shows a gauge per file.

## drift

Is what is deployed what the repo says?

```toml
[sources.drift]
targets = [
  { label = "web", repo = "~/code/webapp", file = "/srv/webapp/REVISION", ref = "origin/main" },
  { label = "api", repo = "~/code/api", url = "https://api.example.com/version", field = "git.sha" },
  { label = "infra", repo = "~/code/infra", command = "cat /etc/infra-rev" },
]
fetch = false          # true: `git fetch` before comparing (or per target)
```

The deployed revision comes from a `file`, a JSON `url` (`field` is a dotted
path) or a `command`; it is compared with `ref` (default `HEAD`) and counted in
commits. `grace_hours` mutes the alert for a while after a fetch, for
pipelines that deploy a few minutes behind.

## mongodb

Opt-in. `mongosh` runs `ping`, `hello` and `dbStats` (read-only). The URI
travels in the child's environment, never in argv (which every user on the
machine can read with `ps`), and is scrubbed from any error.

```toml
[sources.mongodb]
uri_env = "MONGODB_URI"   # where to find the URI: the environment or secrets files…
env_file = ""             # …or a service's own .env, read in memory
database = ""
slow_ms = 3000
```

Alerts: no primary (crit), connect+ping slower than `slow_ms` (warn).

MongoDB runs on whichever machine holds the URI, and it is a *shared* source:
its record rides along in that machine's snapshot, so in a fleet the hub's
HEALTH panel shows it too without the hub ever holding the credential.

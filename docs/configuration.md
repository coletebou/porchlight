# Configuration

One TOML file: `~/.config/porchlight/config.toml` (or `$XDG_CONFIG_HOME/porchlight/`,
or the path in `$PORCHLIGHT_CONFIG`). Everything is optional; with no file at
all porchlight shows what it can detect. `porchlight init` writes a commented
starter file from what the machine actually has, and `porchlight doctor` lists
any setting it does not recognize.

Resolution, lowest to highest precedence:

1. built-in defaults (below)
2. the file's top-level tables
3. every `[hosts."<glob>"]` table whose glob matches this machine, in file order
4. `PORCHLIGHT_IDENTITY` / `PORCHLIGHT_THEME` from the environment

Tables merge key by key; lists and scalars replace. That lets one file,
shared through a dotfiles repo, describe a whole fleet.

> TOML rule worth knowing: top-level keys (`repos`, `ports`) must appear before
> the first `[table]` header, or they become part of that table. `doctor`
> reports `unknown setting services.repos` when that happens.

## Machine identity

The machine's name is `$PORCHLIGHT_HOST`, else the short lowercase hostname.
`[hosts."…"]` globs match against it, the fleet uses it, and `porchlight label`
prints the display label (handy for prompts and tmux status lines, so every
tool agrees on one friendly name):

```toml
[hosts."ip-10-0-*"]
general.label = "build-box"
```

## Top level

```toml
repos = ["~/code/webapp"]                       # git checkouts for the REPOS panel
ports = [{ port = 5173, label = "vite" }]       # TCP ports for NETWORK (host = "127.0.0.1")
```

## [general]

```toml
[general]
identity = "default"      # this machine's identity (see Identities)
label = ""                # display name; default: the machine name
role = "auto"             # auto | dev | server. Auto: a headless Linux box is a server
tags = []                 # extra tags for host filters: ["db", "gpu"] → #db, #gpu
secrets_files = []        # KEY=VALUE files, read by the collector only
secrets_command = ""      # a command printing KEY=VALUE lines (a password manager)
clock = "auto"            # auto | local | utc. Auto: UTC first on servers
clock_secondary = "auto"  # auto | "" (hide) | an IANA zone, e.g. "Asia/Tokyo"
plugins = true            # import ~/.config/porchlight/plugins/*.py
hints = ["alerts", "doctor"]   # commands listed in the footer
```

Automatic tags: `#mac` or `#linux`, `#wsl`, `#dev` or `#server`, `#hub` (has
peers), and `#<identity>`.

## [theme]

```toml
[theme]
name = "porchlight"   # porchlight nord dracula gruvbox catppuccin tokyonight solarized daylight mono
accent = ""           # preset (porchlight teal meadow dusk ember aurora orchid forest) or ["#hex", "#hex", "#hex"]
colors = {}           # override any named color: { green = "#22c55e", dim = "#7b7b7b" }
ascii = "auto"        # auto | true | false
```

See [themes.md](themes.md). `PORCHLIGHT_THEME=nord porchlight` tries one
without editing the file; `porchlight themes` previews them all.

## [banner]

```toml
[banner]
text = "{label}"      # placeholders: {label} {host} {user} {identity}
font = "auto"         # auto | block | small | big | none | figlet | figlet:<font>
file = ""             # pre-made art (plain or ANSI-colored); colored art is printed as is
image = ""            # a logo, rendered with chafa (cached) when it is installed
image_rows = 8
image_cols = 0        # default: 4 × rows
colors = []           # gradient stops; default: the theme accent
```

`auto` picks the built-in `block` font, then `small`, whichever fits beside the
header facts; with neither fitting the banner sits above them, and on a phone it
gives way to a title line.

## [layout]

```toml
[layout]
panels = "auto"       # or an explicit ordered list: ["system", "agents", "docker", ...]
mode = "auto"         # auto | full | compact
compact = ["system", "services", "agents", "fleet"]   # panels in compact mode
compact_width = 100   # auto mode goes compact below this width
columns = 3           # at most
min_column = 58
three_column_width = 180
gap = 3
attention = 10        # alerts in the attention strip (5 in compact mode)
attention_compact = 5
```

`auto` shows every panel that has data, in this order: system, services,
agents, drover, docker, fleet, network, ai, prs, access, instances, health,
edge, infra, aws, comms, backups, files, money, repos, then your custom panels.

## [header] and [footer]

```toml
[header]
weather = true
ips = true            # Tailscale IP and the default-route address
last_visit = true     # "last visit 3h ago · 2 new alerts"
user = false          # user@label

[footer]
quote = true
qotd = true           # a question of the day, the same on every machine
quotes_file = ""      # fallback quotes (one per line) instead of the bundled list
qotd_file = ""        # your own questions, one per line
hints = true
timing = true         # "rendered 18ms"
```

## [thresholds]

```toml
[thresholds]
disk_warn = 85
disk_crit = 92
mem_warn = 92
mem_crit = 98
swap_warn = 80
swap_crit = 95
load_warn = 1.0       # × CPU cores
load_crit = 2.0
```

## [alerts]

```toml
[alerts]
ignore = []           # regexes; matching alerts are muted everywhere ("^swap .* on build-box$")
ignore_units = ["motd-news.service"]   # failed units to ignore (globs)
peer_max_age = 1800   # peers' alerts older than this are not imported
```

## [services]

```toml
[services]
systemd = ["nginx", "docker", { unit = "actions.runner.*", label = "runners" }]
systemd_user = ["syncthing"]
launchd = ["homebrew.mxcl.*", { label = "com.example.sync", name = "sync" }]
auto = true           # with nothing listed: common units on Linux, Homebrew services on macOS
optional = []         # labels that may be down without an alarm
remote_login = true   # macOS: show whether Remote Login (sshd) is on
```

Units sharing a label merge into one cell (`runners 3/3`). Globs expand with
one `systemctl list-units` call. A unit that restarted three or more times
shows `↻N`. A launchd daemon must have a PID; an interval job a zero last exit.
A listed unit systemd does not know is crit `not found`; when `systemctl`
itself cannot answer (no user bus over ssh, a timeout), that scope's units
show as unknown and raise no alert.

## [system]

```toml
[system]
mounts = []           # default: / (macOS: the Data volume); WSL adds C:
mount_labels = { "/srv" = "data" }
```

A mount whose `statvfs` does not answer within 0.8 s (a dead NFS/SMB/FUSE
mount) is reported as "not responding" instead of hanging your login.

## [collector]

```toml
[collector]
interval = 300        # the timer's period (porchlight install timer)
deadline = 50         # seconds for a whole run; stragglers are abandoned
workers = 10
login_refresh = true  # a login that finds stale data starts one background collect
spawn_gap = 30        # at most one such spawn per 30 s (twenty new tmux panes → one collector)
```

## [fleet]

```toml
[fleet]
peers = []            # ssh aliases; any peers make this machine the hub
order = []            # fleet table order
labels = {}           # machine name → short label in the table
ssh_options = []      # extra -o options, e.g. ["ProxyJump=bastion"]
connect_timeout = 6
push_timeout = 25
remote_cache = ""     # the peers' cache dir if it is not the default
badge = ""            # a source whose `badge` fills this host's agents column ("docker" → "12/12")
```

See [fleet.md](fleet.md).

## [identities.*]

```toml
[general]
identity = "work"

[identities.work]
allow = ["procs", "docker", "aws_auth", "prs", "reach", "claude_usage"]   # nothing else runs here
deny = []
secrets_files = ["~/.config/porchlight/work.env"]
secrets_command = ""
```

A source runs, loads secrets, and shows alerts only on machines whose identity
it serves. Restrict a source with `[sources.NAME] identities = ["personal"]`;
lock a whole identity down with `allow`. That is how one shared config keeps
personal API keys off a work laptop, and work credentials off personal boxes.

## [sources.*]

Every source takes `enabled`, `identities` and `hosts`, plus its own settings:

- [Host sources](reference/core.md): procs, tailscale, repos, weather, quotes,
  sites, health, certs, backups, files, drift, mongodb
- [AI plan limits](reference/ai.md): claude_usage, codex_usage, cursor_usage, grok_usage
- [Cloud and SaaS](reference/saas.md): cloudflare, vercel, uptimerobot, hetzner,
  github, sentry, stripe, resend, langfuse, ai_spend, twilio
- [The AWS account and its bill](reference/aws.md): aws_account, aws_cost
- [Developer machines](reference/dev.md): docker, aws_auth, instances, prs, reach
- [Drover](reference/drover.md): drover

For example, the AWS panel and the DROVER panel:

```toml
[sources.aws_account]
profile = "readonly"            # the one profile the AWS panel reads with
home_regions = ["us-east-1"]    # read every run; the account's other regions are swept a few per run
cost_explorer = "daily"         # one $0.01 Cost Explorer request a day; "off" (the default) makes none

[sources.drover]
versions = "hub"                # daemon versions and the credential pool: read on the fleet hub only
```

## [[custom]]

```toml
[[custom]]
name = "restic"
title = "BACKUP"
command = "~/bin/check-restic --json"
ttl = 3600
```

See [plugins.md](plugins.md) for the output formats (JSON, plain lines, and
Nagios plugins), and for Python plugins.

## [hosts."glob"]

Any of the tables above, for matching machines only:

```toml
[hosts."web-*"]
general.role = "server"
layout.panels = ["system", "services", "health", "docker"]
sources.health.targets = [{ label = "app", url = "http://127.0.0.1:8080/healthz" }]

[hosts."*-laptop"]
general.identity = "work"
theme.accent = "teal"
```

## Environment

| Variable | Effect |
|---|---|
| `PORCHLIGHT_CONFIG` | the config file |
| `PORCHLIGHT_CONFIG_DIR` | where `config.toml` and `plugins/` live |
| `PORCHLIGHT_CACHE_DIR` | the cache (default `$XDG_CACHE_HOME/porchlight`) |
| `PORCHLIGHT_HOST` | this machine's name |
| `PORCHLIGHT_IDENTITY`, `PORCHLIGHT_THEME` | override the config |
| `PORCHLIGHT_WIDTH` | render width |
| `PORCHLIGHT_COLOR` | `truecolor`, `256`, `16` or `none` |
| `NO_COLOR`, `FORCE_COLOR` | the usual conventions |
| `PORCHLIGHT_ASCII` | `1` forces ASCII, `0` forbids it |
| `PORCHLIGHT_COMPACT`, `PORCHLIGHT_FULL` | force a layout mode |
| `PORCHLIGHT_DISABLE` | the login render (`--login`) prints nothing |

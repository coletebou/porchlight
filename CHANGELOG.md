# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Opt-in GCP and Azure account checks in ACCESS, using the existing CLI logins
  and read-only API calls to distinguish verified access from cached accounts.
- Gitea, Forgejo and Bitbucket Cloud providers in PULL REQUESTS, with explicit
  repository targets, bounded pagination and per-provider stale-data handling.
- A migration guide for fleet-motd configurations.
- A swap column in the FLEET table, coloured by `swap_warn`/`swap_crit`; `-`
  for machines without swap.
- A DROVER panel and `drover` source: the agent sessions a
  [Drover](https://github.com/coletebou/drover) daemon sees on every host. Who
  waits on you and what they ask (an alert names them), what finished or crashed
  since your last visit (the source keeps the feed, with Drover's cursor), who
  works where, and on the fleet hub each daemon's commit and the credential pool.
  The board is read with Drover's read-only widget token whenever there is one;
  Drover's text is masked by pattern only, never by matching secret values.
- An AWS panel: the month against the account budget with a forecast, a two-week
  spark, the burn per hour, spend by service, every running instance at its list
  price, dedicated hosts, and what stopped boxes still cost a month. MONEY shows
  the month in one line.
- `aws_account` now reads the whole account with free calls only: every enabled
  region (the home regions each run, the others swept a few per run), list prices
  from the Price List API cached for a week (on-demand, spot, EBS, dedicated
  hosts, public IPv4), and the account's budgets. It warns when the month is over
  the budget, when anything runs outside the home regions, and when a dedicated
  host has no instance on it. At most `max_concurrent` aws processes run at once.
- An `aws_cost` source (opt-in, `cost_explorer = "daily"`): the month's spend by
  day and by service from Cost Explorer, at most one billed request per UTC date.
  The cap is enforced in the source against the last attempt, noted before the
  request goes out, so `collect --force`, `collect aws_cost` and retries never
  add a request; the CLI's own retries and pagination are off, and in a fleet
  only the hub asks. Account ids in stored profile names and AWS errors keep
  only their last four digits.
- Panel helpers for plugins: `fitted()` (as many items as fit, then "+N"),
  `wrap()` (words onto lines, a word cut only when it alone outgrows its line),
  `theme.spark()`, and
  `x.last_seen` (the previous login) on the render context. A source's `optin`
  may be a callable that decides whether a machine opted in.

### Changed
- `[sources.aws_account]` reads one account: `profile` (when set, the only
  profile tried) replaces `profiles`, and `home_regions` replaces `regions`.
  `cost_explorer` takes `"daily"` or `"off"` (the default); `true` still means
  daily. An expired or missing login is now a soft failure that keeps the last
  inventory, with the login command in the panel, instead of a warning that hid it.
- INFRA shows a one-line AWS summary only when the AWS panel is not in the
  layout. MONEY's AWS line is the month against the budget; with
  `cost_explorer = true` it used to ask Cost Explorer every hour for each account.

### Fixed
- `git status` scans (the REPOS panel and the `repos` source) no longer take
  optional locks, so a scan killed by its timeout cannot leave
  `.git/index.lock` behind and break your next commit.
- A systemd scope that `systemctl` cannot answer for (no bus, a timeout) shows
  its units as unknown, without alerts, instead of reporting every configured
  unit as crit "not found". Globs it could not expand no longer vanish.
- Error text masks a secret under a password name (PASSWORD, PASSWD,
  PASSPHRASE) from four characters instead of eight, and masks values whose
  whitespace differs from the text's (tabs, newlines, runs of spaces), which
  collapsing whitespace first used to let through. Words that merely contain a
  token prefix ("disk-data") stay intact.

## [0.1.0] - 2026-10-21

First public release.

### Dashboard
- Cache-backed login render (~10-40 ms): gradient banner beside the header facts,
  a ranked attention strip with NEW-since-last-login badges, a 1-3 column masonry
  grid of panels, and a footer with a quote and a question of the day.
- Compact view for phone-width terminals; `porchlight all` / `--full` overrides.
- Built-in half-block pixel fonts (block, small, big), figlet fonts, custom art
  files, and logos through chafa.
- Nine themes, eight accent presets, per-host accents, truecolor/256/16/none,
  `NO_COLOR`, and an ASCII mode that keeps every column in place and turns on
  automatically when the locale cannot show glyphs.

### Collector
- Per-source TTLs, timeouts and failure isolation; last good data kept on
  failure; soft failures for expired logins; two-strike flap guards; a run
  deadline; single instance via flock; rate-limited background refresh from
  the login.
- `porchlight install timer` for launchd, systemd user timers or cron;
  `porchlight install shell` for zsh, bash and fish.

### Fleet and identities
- Hub/peer exchange over existing SSH aliases: one round trip per peer pushes
  account-wide records and pulls vitals, alerts and shared records.
- Identities with per-source scoping and allow/deny lists, so work and personal
  credentials never cross.

### Sources
- Host: process census with AI coding agents, orphans, MCP servers, zombies and
  ssh tunnels; Tailscale; repos; weather (Open-Meteo, NWS, wttr.in); quotes;
  public sites and local health routes; TLS certificates; backups; file budgets;
  deploy drift; MongoDB.
- AI plans: Claude, Codex, Cursor, Grok (read-only; tokens are never refreshed).
- Cloud and SaaS: Cloudflare, Vercel, UptimeRobot, Hetzner, GitHub, Sentry,
  Stripe, Resend, Langfuse, API spend (Anthropic, OpenAI, OpenRouter, xAI), AWS,
  Twilio.
- Developer machines: Docker/Podman compose stacks, AWS credentials and SSO,
  EC2 instances, GitHub and GitLab pull requests, VPN and endpoint reachability.
- `[[custom]]` commands (JSON, lines, Nagios plugins) and Python plugins.

### Tools
- `doctor`, `sources`, `panels`, `alerts`, `json`, `init`, `demo`, `themes`,
  `top`, `label`.

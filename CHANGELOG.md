# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

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

# Security model

porchlight reads a lot of sensitive state: API tokens, OAuth logins, cloud
credentials, the process table. The design keeps all of it on the machine
that owns it and out of everything porchlight writes.

## What runs where

- **The login render never loads a secret** and never touches the network. It
  reads cache records and runs a few local probes (procfs, `statvfs`,
  `systemctl show`, `launchctl list`, `tmux`, `git status`).
- **The collector** (`porchlight collect`, from a timer or a background spawn)
  is the only process that reads `secrets_files` or runs `secrets_command`, and
  only for sources that apply to this machine's identity.

## Credentials

- Secrets files are parsed in memory as `KEY=VALUE` lines: no shell, no
  expansion beyond `~` and `$HOME`, so a secrets file cannot execute code.
- Values go into request headers (or a child's environment), never into URLs,
  argv (which every local user can read with `ps`), cache records, logs or
  error messages.
- Every error string is scrubbed twice before it is stored: each
  secret-looking environment value (by variable name) is replaced by
  `<redacted:NAME>`, then anything token-shaped (`sk-…`, `ghp_…`, `glpat-…`,
  JWTs, URI user:password, long base64 runs) is masked.
- Credentialed HTTP requests never follow redirects, so an `Authorization`
  header cannot be forwarded to another host; tokens are only sent to the
  vendor's own API hosts, never to a host an API response merely names.
- **Identities** scope credentials: a source runs, loads secrets, and shows
  alerts only on machines whose identity it serves, and an identity's `allow`
  list makes everything else impossible there (see
  [configuration.md](configuration.md#identities)).

## Read-only probes

Every built-in source is read-only. HTTP sources use GET, plus a small set of
documented read-only POST queries (GraphQL reads for Cloudflare and GitHub,
UptimeRobot's legacy `getMonitors`); the SaaS test suite audits the source for
any other verb. Nothing creates, updates or deletes remote state.

Cloud account checks use the official AWS, Google Cloud and Azure CLIs. These
CLIs may maintain or refresh their own login caches while making read-only API
requests. Porchlight does not issue login, token-export or explicit refresh
commands. GCP and Azure checks are opt-in.

## Drover

The Drover source reads the daemon's own token files and sends a token only in
the `Authorization` header of a request to the daemon's URL from `daemon.json`;
no token is stored. The two board operations go out with the read-only widget
token whenever one exists, so nothing porchlight sends can message or steer a
session; the main token is used only for two read operations on the fleet hub.
Drover's text is masked by pattern (token shapes, credentials in URIs), never by
matching secret values against it, which would reveal which secret equals a word
on the screen ([drover.md](reference/drover.md#tokens-and-text)).

## AI subscription logins

The AI plan-limit sources read the OAuth tokens that Claude Code, Codex,
Cursor and the Grok CLI already store (files, or the macOS Keychain) and call
the same usage endpoints those tools' own `/usage` views call.

- **Tokens are never refreshed, rotated or written.** An expired token is
  reported as "login expired"; the vendor CLI refreshes it on its next use.
  Refreshing it from porchlight would rotate the refresh token and could log
  the CLI out.
- Keychain reads never wait on a GUI prompt (a timeout; a non-GUI session such
  as ssh gets a clear message instead).
- With `fallback_hosts`, the probe module is piped to `ssh HOST python3 -` so a
  token never leaves the machine that owns it; only the usage numbers come back.

## Files on disk

- Cache: `$XDG_CACHE_HOME/porchlight` (default `~/.cache/porchlight`), mode
  0700; every record is written mode 0600 via write-then-rename.
- Records hold summaries and counts. `porchlight json` shows exactly what is
  stored; nothing in it is a credential.
- The collector log rotates at 256 KB and holds source names, timings and
  scrubbed errors.

## Fleet

The hub pushes only *global* source records (account-level summaries) and
pulls only snapshots (vitals, counts, alert messages, and the records of
sources explicitly marked `share`). Everything travels inside your own SSH
connections, with `BatchMode=yes`, to the aliases you list.

## Plugins and custom commands

`[[custom]]` commands and `plugins/*.py` run with your user's privileges and
the collector's merged environment, including secrets: they are your own
code, so treat that directory like your shell config.

## Reporting a vulnerability

Please report security issues privately through GitHub's "Report a
vulnerability" (Security → Advisories) on the repository rather than in a
public issue.

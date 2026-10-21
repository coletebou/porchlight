# AI plan limits

How much of your AI subscriptions' usage windows you have left: Claude Pro/Max,
Codex on a ChatGPT plan, Cursor and the Grok Build CLI. Each source reads the
login the vendor's own tool already stored on this machine and asks the same
usage endpoint that tool's `/usage` view asks. Nothing is configured by hand:
if `claude`, `codex`, Cursor or `grok` is signed in here, its row appears.

| Source | Default cadence | Reads | Asks |
|---|---|---|---|
| [`claude_usage`](#claude_usage) | 10 min | Claude Code's login: `~/.claude/.credentials.json`, or the macOS Keychain | `api.anthropic.com/api/oauth/usage` |
| [`codex_usage`](#codex_usage) | 10 min | `~/.codex/auth.json`; Codex's rollout files when offline | `chatgpt.com/backend-api/wham/usage` |
| [`cursor_usage`](#cursor_usage) | 30 min | cursor-agent's login (Keychain or `auth.json`), or the Cursor app's state database | `api2.cursor.sh` dashboard service, `cursor.com/api/usage-summary` as a fallback |
| [`grok_usage`](#grok_usage) | 1 h | `~/.grok/auth.json` | `cli-chat-proxy.grok.com/v1/billing` |

All four are *global*: they describe an account, not a machine. In a
[fleet](../fleet.md) the hub collects them and every peer renders the hub's
copy, so one machine signed in to Claude is enough for every login screen to
show the Claude row.

Each runs only where its tool is set up (its config directory exists:
`~/.claude`, `~/.codex`, `~/.cursor` or the Cursor app's support directory,
`~/.grok`) or where `fallback_hosts` can answer for it. Elsewhere `porchlight
sources` shows it as off, rather than failing every run with "not signed in".

## Read-only, and never a refresh

- **No credential is ever refreshed, rotated or written.** OAuth refresh tokens
  are single-use: a refresh from porchlight would hand the vendor CLI a token
  that no longer works and sign it out. An expired access token is reported
  instead, without a request, and the CLI renews it the next time you use it.
- **No token is ever stored or printed.** Results are built from numbers and
  labels; error text is scrubbed of every token the run read and of anything
  token-shaped. `porchlight json claude_usage` shows exactly what is cached.
- **The Keychain never blocks.** `security` runs under a 5 second timeout, so a
  locked keychain or an access prompt cannot hang a run. In a session with no
  GUI login (ssh, cron) macOS refuses Keychain reads with rc 36; the source says
  so once instead of asking again for every account.
- **Credentialed requests never follow redirects**, so a token cannot be
  replayed to a host it was not meant for.
- Only the fields shown below are kept from a response. The Cursor billing
  profile, for example, contributes its plan name and nothing else.

## Settings

The common settings, shown with their defaults. Each source also takes
`enabled`, `identities` and `hosts` like any other.

```toml
[sources.claude_usage]       # likewise codex_usage, cursor_usage, grok_usage
warn = 75                    # alert at this percent of any window (cursor: 85)
crit = 90                    # (cursor: 98)
fallback_hosts = []          # ssh aliases to ask when this machine has no live login
fallback_timeout = 25        # seconds for all fallback hosts together
quiet_errors = false         # true: no "login expired" row on this machine
```

`claude_usage` and `codex_usage` add `config_dir`; `claude_usage` and
`cursor_usage` add `keychain`:

```toml
config_dir = ""              # claude: "" = $CLAUDE_CONFIG_DIR, else ~/.claude
                             # codex:  "" = $CODEX_HOME, else ~/.codex
keychain = true              # macOS: also read the vendor's Keychain item
```

### quiet_errors

On a machine where no subscription login is expected (a work laptop whose
Claude Code runs on a cloud API key, say), the hint row is noise:

```toml
[hosts."work-*".sources.claude_usage]
quiet_errors = true
```

Readings are still collected and still alert; only the "not signed in" and
"login expired" rows are hidden.

### fallback_hosts

When this machine's login is missing or expired, the source asks other machines
in order until one answers from the vendor's API:

```toml
[sources.claude_usage]
fallback_hosts = ["desktop", "devbox"]
```

For each alias it runs `ssh ALIAS python3 -I - --json claude` and writes the
probe module (`porchlight/sources/ai_probe.py`, stdlib only, no porchlight
imports) to its stdin. Nothing is installed on the other machine, `-I` keeps
its working directory and `PYTHON*` variables out of the import path, and the
other machine reads **its own** login: the token never crosses the wire, only
the numbers do. ssh runs with `BatchMode=yes` and your `[fleet]` options
(`connect_timeout`, `ssh_options`), so it never stops at a password prompt.

- `config_dir` and `keychain = false` travel along (`config_dir` as written, so
  a `~` expands on the other machine).
- A reading that came this way carries `detail.via`, the alias that answered.
- When nobody answers live, the freshest reading with numbers wins (a Codex
  rollout snapshot, say), else this machine's own answer, with every attempt
  listed in `detail.tried`.
- Each alias needs `python3` (3.9 or newer) on its PATH for non-interactive ssh.

## claude_usage

Claude Code's subscription login (`claudeAiOauth`), never an API key.

- **Linux:** `<config_dir>/.credentials.json`.
- **macOS:** that file and the login Keychain. Claude Code keeps the login under
  service `Claude Code-credentials`; with `CLAUDE_CONFIG_DIR` set it appends `-`
  and the first 8 hex digits of the variable's SHA-256, so each config dir has
  its own login, and the source follows the same rule. The Keychain can hold
  more than one item under a service (a stale one under an old account name
  beside the live one) and `security` returns whichever it meets first, so every
  candidate account is read (`$USER`, the login name, `claude-code-user`,
  `unknown`, then any) and the login that expires last wins.

`GET https://api.anthropic.com/api/oauth/usage` (`anthropic-beta:
oauth-2025-04-20`) returns the windows `/usage` shows: `5h` (the session
window), `7d` (all models) and per-model weekly windows such as `7d Opus`
(marked `extra`). Older responses without the `limits` list are read from their
`five_hour` / `seven_day` / `seven_day_<model>` keys. Extra usage (pay as you
go past the plan) goes to `credits`; undocumented dollar budgets are kept in
`detail.dollar_buckets`.

Alerts: every window at `warn` or `crit`, with its reset time
(`Claude 7d at 91% (resets Tue 02:36)`), and a warning when extra usage is on
and has hit its monthly spend limit.

## codex_usage

Codex signed in with ChatGPT: the access token in `<config_dir>/auth.json`
(`tokens.access_token`, sent with its `ChatGPT-Account-Id`). The plan comes from
the response, or from the token's claims when there is none.

`GET https://chatgpt.com/backend-api/wham/usage` gives the plan's windows,
labelled by their length (`5h`, `7d`; a plan with a single weekly limit has just
`7d`), the code review limit (`review 7d`) and any additional limits (each by
name, `extra`), plus the credit balance.

**Offline fallback, zero network:** when the token is missing or expired, or the
request fails, the source reads the rate limits Codex itself recorded in its
newest `token_count` event, under `<config_dir>/sessions/YYYY/MM/DD/rollout-*`.
Only the newest date directories are opened (until three days that hold
rollouts), never the whole tree, and a file is read backwards from its end.
Compressed `.jsonl.zst` rollouts are read with the standard library on Python
3.14+ and with the `zstd` binary otherwise (skipped when neither exists). Such a
reading has `"source": "rollout"`, `as_of` is when Codex wrote it, and a window
that has reset since shows no number. A snapshot whose every window has reset
says nothing about now and is not used.

An API-key login has no plan limits and is reported as such. Codex's optional
OS keyring store (`cli_auth_credentials_store = "keyring"`) is not read: macOS
would show a permission dialog to any program but codex itself. The rollout
fallback still works there.

Alerts: every window at `warn` or `crit`, and `Codex rate limit reached` (crit,
first) when the response or the snapshot says a limit has been hit.

## cursor_usage

Cursor tokens, tried in order: cursor-agent's Keychain item (macOS, service
`cursor-access-token`, account `cursor-user`), cursor-agent's `auth.json`
(`~/.cursor/auth.json` on macOS, `$XDG_CONFIG_HOME/cursor/auth.json` on Linux),
then the desktop app's state database (`…/Cursor/User/globalStorage/state.vscdb`),
opened read-only (`mode=ro`), so it is safe while Cursor runs. The first
unexpired token is used.

`POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage`
returns the billing cycle's included usage: `total`, and its `auto` and `api`
shares (`extra`), with the cycle end as the reset. Included, bonus and
on-demand dollars go to `credits`. A token without a cached plan asks
`/auth/full_stripe_profile` for the plan name. If the dashboard service fails,
the web dashboard's `GET https://cursor.com/api/usage-summary` is tried with the
session cookie Cursor's site builds from the same token (sent to cursor.com
only).

Alerts: one at most. The total window at `warn` / `crit` raises it, in Cursor's
own words when the response carries a message (`Cursor: You've used 90% of your
included usage (resets Fri 09:42, on-demand $81)`). Any other message from Cursor
raises a warning even below `warn`; a routine "You've used N%" notice below
`warn` does not. The shares never alert on their own: they are slices of the
total.

## grok_usage

The Grok Build CLI's grok.com login: the newest entry in `~/.grok/auth.json`
(its key and expiry only; the name, e-mail and ids beside them are never read
into the result). grok.com sessions last 7 days; an expired one needs `grok
login`.

`GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`, with the headers
the CLI sends (its version from `~/.grok/version.json`), is what the CLI's own
`/usage` shows: the billing period's credit use as one window. This response
is undocumented, so it is parsed defensively and `detail.response_keys` lists
the fields it had. When `XAI_API_KEY` is set, `detail.note` says so: the CLI
then prefers the API key, so its usage bills the xAI API team, not the plan.

Alerts: the period window at `warn` or `crit`.

## Data

`porchlight json <source>` shows a record. Its `data`:

```json
{"provider": "claude", "plan": "max (default_claude_max_20x)",
 "windows": [{"label": "5h", "used_pct": 17.0, "resets_at": 1792151999, "severity": "normal"},
             {"label": "7d Opus", "used_pct": 40.0, "resets_at": 1792500000, "extra": true}],
 "credits": {"extra_usage_enabled": false, "currency": "USD", "used": 0.0, "monthly_limit": null, "limit_reached": false},
 "source": "api", "as_of": 1792140000, "latency_ms": 280, "error": null,
 "detail": {"token_store": "keychain:you", "token_expires_at": 1792168000},
 "alerts": [{"sev": "warn", "msg": "Claude 7d at 82% (resets Sat 09:00)"}]}
```

- `windows[].resets_at` is epoch seconds; `used_pct` is null when nothing was
  reported or the window reset after a snapshot (`note` says so).
- `credits` per provider: Claude `extra_usage_enabled`, `used`,
  `monthly_limit`, `limit_reached`; Codex `has_credits`, `unlimited`,
  `balance`; Cursor `included_used_usd`, `included_limit_usd`, `bonus_usd`,
  `total_spend_usd`, `on_demand_used_usd`; Grok the billing response's numbers.
- `detail.auth`, when there is no reading: `none` (not signed in), `expired`
  (the CLI renews it on its next run), `relogin` (sign in again), `api_key` (no
  plan limits), `keychain` (the login is most likely in the Keychain, which this
  session cannot read).
- Alert reset times are the collecting machine's local time: an alert is read
  later than it is written, so it names a moment instead of a countdown.

A failed run keeps the last good reading (the record turns `ok: false`, `kept:
true`), so the panel shows the old numbers with the reason beside them.

## The AI LIMITS panel

```
 AI LIMITS ● ───────────────────────────────────────────────────────────────── claude ✖ 2h
 claude max5x   5h ━━━━╸───────  38%  7d ━━━━━━━━━╸──  82% ↻2h13m
                7d Opus 57% · extra usage $12.40/$50.00
                token expired · renews on the next `claude` run
 codex plus     5h ━───────────  12%  7d ━━━━╸───────  41% ↻3h21m
                review 7d 3% · 240 credits
 cursor pro     total ━━━━━━━╸────  64% ↻Nov 01
                $13/$20 included · $4 bonus
 grok SuperGrok login expired · run `grok login`
 langfuse       web 602 traces $2.10 · 24h · v3.1
```

One row per provider: plan, the two main windows, the soonest reset
(`↻3h06m` within a day, a weekday within six, else a date). Below it, a dim line
of extras: per-model and other `extra` windows, credits, extra usage, a Cursor
cycle's dollars (as many as fit). A window that has reset since its reading
shows `—` rather than a stale number. A provider with no reading gets a hint
instead:

| Hint | Meaning |
|---|---|
| ``token expired · renews on the next `claude` run`` | the access token lapsed; using the CLI renews it |
| ``login expired · run `claude auth login` `` | the login itself is over (refresh token expired, or the API refused it) |
| ``not signed in · `codex login` `` | no subscription login on this machine |
| `Keychain not readable in this session` | collect from your GUI session (`porchlight install timer`), or use `fallback_hosts` |
| anything else | the error, whitespace folded, already scrubbed |

The same hint appears under a kept reading when a login is why it is not
newer. The title names the first provider whose record is failing or unusually
old (`claude ✖ 2h`: failing for two hours), with `+N` for any others. The panel
also shows a Langfuse line (traces and cost per project over 24 hours) when the
`langfuse` source has data.

## Running the probe by hand

```sh
python3 porchlight/sources/ai_probe.py                  # one line per provider
python3 porchlight/sources/ai_probe.py --json claude    # the raw result
python3 porchlight/sources/ai_probe.py --host devbox    # the same, read on devbox over ssh
```

`--claude-dir`, `--codex-dir` and `--no-keychain` match the settings above.

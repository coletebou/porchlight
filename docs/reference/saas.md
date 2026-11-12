# SaaS and cloud sources

Eleven sources watch the services a small team or a homelab runs on:
Cloudflare, Vercel, UptimeRobot, Hetzner, GitHub, Sentry, Stripe, Resend,
Langfuse, AI API spend (OpenRouter, Anthropic, OpenAI, xAI) and Twilio.
The EDGE, INFRA, COMMS and MONEY panels show them (see [Panels](#panels)).
The AWS account and its bill have their own page: [aws.md](aws.md).

Each source turns itself on when its credentials are present: set the
variables in your shell or in a `[general] secrets_files` file, and run
`porchlight collect`. `porchlight sources` shows what runs here and why not.

## Shared behaviour

**Read-only.** Every request is a GET, apart from exactly three read-only POST
queries: Cloudflare GraphQL, GitHub GraphQL, and UptimeRobot's v2
`getMonitors` (only when the v3 API refuses). Nothing creates, changes or
deletes anything. The only subprocess is `gh auth token`. A test audits the
source file for these rules.

**Credentials** travel in request headers (UptimeRobot v2 takes its key in the
POST body, the way that API is defined). They never appear in a URL, an error,
a summary or the stored details: every result is scrubbed of credential values
once more before it is written. Redirects are never followed, so a credential
cannot be forwarded to another host, and a token is only ever sent to the host
it was issued for, never to one an API merely names.

**Timeouts.** Each request has a 6 second timeout, and the collector's
deadline caps every run.

**Results.** Every source stores `{"status": "ok" | "warn" | "crit" |
"unknown", "summary": "...", "details": {...}, "error": null | "..."}`. Each
warn or crit issue also becomes an alert (with a `fix` command where one
helps). Status `unknown` with an error is a soft failure: missing credentials,
an expired login, a key without read access. The collector keeps the last good
reading, marks it stale, and retries at the normal interval. A service without
credentials shows nowhere.

**Scope.** All of them are `scope = "global"`: in a fleet the hub collects
them and ships the records to every peer, so one set of API keys serves every
machine. Identities can keep a personal key off a work laptop:

```toml
[identities.work]
deny = ["stripe", "ai_spend"]
```

**Common settings**, valid in every `[sources.<name>]` table: `enabled`,
`hosts`, `identities`, and `paths` (extra directories to search for `gh`).
`ignore` lists match case-insensitive substrings of a name.

## `cloudflare`

Tunnels (healthy, degraded, recently down, retired), zone status, the last 24
hours of edge traffic with the 5xx rate judged per zone, and Email Routing.

**Credentials**, either of:

- `CLOUDFLARE_API_TOKEN` (or `CF_API_TOKEN`), preferred: create a token with
  only read permissions for Zone, Analytics, Cloudflare Tunnel and Email
  Routing.
- `CF_EMAIL` + `CF_GLOBAL_API_KEY` (or `CLOUDFLARE_EMAIL` +
  `CLOUDFLARE_API_KEY`), the legacy global key.

Without either the source stores `missing env: ...` and shows nothing.

```toml
[sources.cloudflare]
account_id = ""            # else CLOUDFLARE_ACCOUNT_ID / CF_ACCOUNT_ID, else the one account that owns your zones
zones = []                 # zone ids or names; else CF_ZONE_ID / CLOUDFLARE_ZONE_ID; else every zone (first 50 listed)
max_zones = 25             # with no zones set: how many zones get traffic and Email Routing checks
ignore = []                # zone or tunnel names to leave out
tunnels = true
tunnel_recent_days = 14    # a tunnel down longer than this is retired, not an incident
traffic = true
warn_5xx_pct = 1.0
crit_5xx_pct = 5.0
min_requests_warn = 20     # below this many requests a zone's 5xx rate is not judged
min_requests_crit = 50
email_routing = true       # false: skip; "required": a zone with routing off is a warning too
```

**Alerts.** crit: a tunnel down within `tunnel_recent_days`, a zone's 5xx rate
at `crit_5xx_pct` (with at least `min_requests_crit` requests). warn: a
degraded tunnel, a zone's 5xx rate at `warn_5xx_pct`, a zone that is not
active, a configured zone name that matches nothing, Email Routing enabled but
not ready, partial data (tunnels, zones or traffic unreadable).

**Endpoints** (`https://api.cloudflare.com/client/v4`, read-only):
`GET /zones?per_page=50`, `GET /accounts/{account}/cfd_tunnel?is_deleted=false`,
`GET /zones/{zone}/email/routing`, and `POST /graphql` for the
`httpRequests1hGroups` and `emailRoutingAdaptiveGroups` analytics queries
(GraphQL takes only POST; these are queries, never mutations, sent in batches
of ten zones, the API's limit).

## `vercel`

The latest production deployment of every project: ready, building, and
failing (by how recently). A CANCELED deployment is routine (an ignored build
step, a superseded build) and is counted, never alarmed on.

**Credentials:** `VERCEL_TOKEN`. Optional `VERCEL_TEAM_ID`.

```toml
[sources.vercel]
team_id = ""       # else VERCEL_TEAM_ID; a team token already defaults to its team
projects = []      # only these projects (exact names); empty: all
ignore = []
crit_hours = 24    # a production ERROR newer than this is crit
warn_days = 7      # ...newer than this is warn; older is a stale project, shown dimmed
```

**Alerts.** crit: a production deployment in ERROR within `crit_hours`. warn:
one within `warn_days`.

**Endpoints:** `GET https://api.vercel.com/v9/projects?limit=100` (paged with
`until`, up to five pages).

## `uptimerobot`

External monitors: how many are up, paused and down, and for how long.

**Credentials:** `UPTIMEROBOT_API_KEY` (a read-only API key is enough).

```toml
[sources.uptimerobot]
ignore = []        # monitor names to leave out (substring match)
recent_days = 7    # down for less than this: crit; longer: warn (a target you have abandoned)
```

**Alerts.** crit: a monitor down for less than `recent_days` (or for an unknown
time). warn: a monitor down longer.

**Endpoints:** `GET https://api.uptimerobot.com/v3/monitors?limit=200` with the
key as a Bearer token; a next page is followed only on that same host. If v3
refuses the key, `POST https://api.uptimerobot.com/v2/getMonitors` (the v2 API
is POST-only; getMonitors only reads).

## `hetzner`

A per-resource monthly cost table — servers (with their backup surcharge),
volumes, primary IPv4/IPv6 addresses, Floating IPs, snapshots, load balancers
and Storage Boxes — from the Hetzner Cloud price list, each priced at its own
type and location; `est_monthly` is their sum. Hetzner has no billing API, so
every figure here (`est_monthly`, `est_by_kind`, and each resource's
`usd_mo`) is a list-price estimate, never an invoice; `cost_mtd` in the
details says so. Also: server and load balancer status, outgoing traffic
against the included allowance (with a list-price estimate of any overage),
and Storage Box fill.

**Credentials:** `HCLOUD_TOKEN` (a read-only project token is enough; the same
token reads the project's Storage Boxes).

```toml
[sources.hetzner]
storage_boxes = true
primary_ips = true         # false: skip (older tokens may not reach every resource type)
floating_ips = true
snapshots = true
load_balancers = true
price_basis = "gross"      # "net": Hetzner prices carry both; gross includes VAT
ignore = []                # resource names to leave out (servers, boxes, IPs, snapshots, load balancers)
traffic_warn_pct = 90      # of a server's or load balancer's included outgoing traffic
box_warn_pct = 80
box_crit_pct = 90
```

**Alerts.** crit: a server off, deleting or unknown; a Storage Box at
`box_crit_pct`. warn: a server or load balancer starting/stopping/migrating or
over `traffic_warn_pct`; a Storage Box at `box_warn_pct` or not active;
partial data for the inventory itself (servers, Storage Boxes). A failed
price-list read, or a primary IP/Floating IP/snapshot/load balancer read the
token cannot reach, only drops that category from the estimate — an extra,
like an unpriced resource, never news on its own.

**Endpoints:** `GET https://api.hetzner.cloud/v1/servers`, `/volumes`,
`/primary_ips`, `/floating_ips`, `/images?type=snapshot`, `/load_balancers`
and `/pricing`; `GET https://api.hetzner.com/v1/storage_boxes`.

## `github`

The CI verdict on each repository's branch, open pull requests and changes
requested.

The verdict comes from the latest conclusive run of each workflow on the
branch, across `push` and `repository_dispatch` runs (deploy workflows are
often re-run by a dispatch after a failed push run). Cancelled, skipped and
neutral runs never decide it, and a workflow whose latest run failed more than
`stale_days` ago is stale rather than an incident. A workflow that looks red is
confirmed from its own run list before it raises an alarm. The rollup of
status checks on the branch head counts too.

**Credentials,** the first one GitHub accepts: `GH_TOKEN`, `GITHUB_TOKEN`, then
the `gh` CLI's own login (`gh auth token`). A fine-grained token needs only
read access to Actions, Pull requests, Commit statuses and Metadata.

```toml
[sources.github]
repos = ["owner/name", "owner/other@master"]   # required: "owner/name", "owner/name@branch" or { repo, branch, label }
branch = "main"                                 # for entries without @branch
user = ""                                       # whose open PRs to count; default the token's owner
events = ["push", "repository_dispatch"]        # the run events that decide the verdict
stale_days = 14
```

The source is off until `repos` is set (`porchlight init` fills it from your
checkouts).

**Alerts.** crit: a workflow failing (`fix`: `gh run list -R owner/name -b
branch`), or the branch head's checks failing. warn: pull requests with
changes requested, partial data.

**Endpoints** (`https://api.github.com`): `GET /user` (to learn the token's
owner) or `GET /rate_limit` (free) to validate the token;
`POST /graphql` with one read-only query (repos and branches are passed as
variables) for PR counts and the head rollup;
`GET /repos/{owner}/{name}/actions/runs?branch=&event=&per_page=30`; and, only
for a workflow that looks red, `GET
/repos/{owner}/{name}/actions/workflows/{id}/runs?branch=`.

## `gh_actions_cost`

Month-to-date GitHub Actions spend for one or more accounts: gross vs net,
minutes by SKU, non-minute usage (storage) by SKU, and the top repositories by
net cost. A separate source from `github` above (that one is CI status; this
one is billing) so either runs without the other.

**Credentials:** the same lookup as `github`: `GH_TOKEN`, `GITHUB_TOKEN`, then
`gh auth token`. Reading billing needs more than CI does: a classic token
needs the `user` scope (your own account) or `admin:org` (an org you own); a
fine-grained token needs the account's "Plan: read" permission. A token that
can reach GitHub but lacks that scope is reported as needing it — a state,
never an error alert. GitHub answers such a token with a 403, or, for a classic
token, a 404 on both billing endpoints; the classic case is recognised from the
`X-OAuth-Scopes` header GitHub returns, so a fine-grained token's 404 (no such
header) still shows as a plain fetch failure.

```toml
[sources.gh_actions_cost]
user = ""          # empty: the token's own login (one extra GET to learn it, as github.user does)
orgs = []          # org logins to also bill; each needs its own billing-read scope
budget_usd = 0     # 0: no budget alert, only a plain "net spend > 0" warning
top_repos = 8      # repositories kept in top_repos, sorted by net cost
```

**What counts as Actions.** A usage line item counts when its `product` is
exactly `"Actions"`, or its `sku` names Actions (the larger "Linux"/"Windows"/
"macOS" runners, and larger-runner variants, are separate SKUs under the same
product). A bare `"Shared Storage"` row — which GitHub Packages can also bill
— is left out rather than attributed to Actions by a guess; it would only
ever show up if a repository uses no other Shared Storage consumer.

**Enhanced vs legacy billing.** The primary read is the enhanced usage report,
`GET /users/{user}/settings/billing/usage` (or `/organizations/{org}/...`),
scoped to this month (`year`, `month` query parameters); each item carries
`date`, `product`, `sku`, `quantity`, `unitType`, `pricePerUnit`,
`grossAmount`, `discountAmount`, `netAmount` and, for Actions, `repositoryName`.
An account without the enhanced billing platform (GitHub Enterprise Server, or
an older personal account) gets a 404 there; the source falls back to the
legacy `GET /users/{user}/settings/billing/actions` (`/orgs/{org}/...`), which
has no dollar figures or per-repo detail, only `total_minutes_used` against
the plan's `included_minutes`. Mixing targets (your own account on the
enhanced platform, an org still on the legacy one) is normal: both read, and
the panel shows what each has.

`discount_pct` is `discountAmount / grossAmount` for the Actions-only rows: the
share of this month's usage the plan's included minutes, or an existing
credit, already covered — not literally "percent of your plan's minute quota
used", since neither billing API exposes the quota's size, only the discount
it already applied.

**Alerts.** crit: net spend over `budget_usd`. warn: any net spend at all
(the common case for a public-repo-only, free-tier account is $0 net forever).
Nothing else alerts: a target needing billing scope, a legacy-only target, or
a partial multi-target failure are states, shown in the panel, never an alarm.

**Endpoints** (`https://api.github.com`, GET only): `GET /user` or `GET
/rate_limit` to validate the token (as `github` does); `GET
/users/{u}/settings/billing/usage` or `/organizations/{org}/settings/billing/usage`
(`year`, `month` params); on a 404, `GET /users/{u}/settings/billing/actions`
or `/orgs/{org}/settings/billing/actions`.

## `sentry`

Unresolved issues that had events in the last 24 hours, per project.

**Credentials:** `SENTRY_AUTH_TOKEN` (scopes `org:read`, `project:read` and
`event:read`).

```toml
[sources.sentry]
url = "https://sentry.io"   # a self-hosted Sentry: its https:// URL
orgs = []                   # organization slugs; empty: the first five the token sees
issues_warn = 1
issues_crit = 10
events_crit = 200
```

**Alerts.** warn: at least `issues_warn` unresolved issues. crit: at least
`issues_crit` issues or `events_crit` events.

**Endpoints:** `GET {url}/api/0/organizations/`, then for each organization
`GET {region}/api/0/organizations/{slug}/events/` (one Discover query) and
`.../projects/`. An organization's region URL is used only when it is https on
the configured host, or on a `*.sentry.io` host when that host is sentry.io;
any other host never receives the token.

## `stripe`

Available and pending balance, subscriptions by status, and an MRR estimate
(active subscriptions' flat recurring prices, normalised to a month; metered
and tiered prices and discounts are left out).

**Credentials:** `STRIPE_SECRET_KEY`. Prefer a restricted key (`rk_live_...`)
with read access to Balance and Subscriptions only. The mode (live or test)
comes from the key's prefix; the key itself is never stored.

```toml
[sources.stripe]
subscriptions = true   # false for a key without subscription access
max_pages = 3          # 100 subscriptions per page, newest first
```

**Alerts.** warn: past-due or unpaid subscriptions, partial data.

**Endpoints:** `GET https://api.stripe.com/v1/balance`, `GET
https://api.stripe.com/v1/subscriptions?status=all&limit=100` (paged with
`starting_after`).

## `resend`

Domain verification and the last 24 hours of sends by outcome. Only each
email's event and time are read, never a recipient or a subject.

**Credentials:** `RESEND_API_KEY`. Reading needs a full-access key: Resend has
no read-only scope, and a sending-only key (the kind most apps hold) is
detected and reported as such instead of being guessed at.

```toml
[sources.resend]
bounce_crit_pct = 5      # bounced, complained or failed, of the last 24h
bounce_min_emails = 20   # below this many sends any bounce is only a warning
```

**Alerts.** crit: a domain failed verification, the key rejected, bounces at
`bounce_crit_pct`. warn: a domain pending, any bounce.

**Endpoints:** `GET https://api.resend.com/domains`, `GET
https://api.resend.com/emails?limit=100`.

## `langfuse`

Server health and version, and per project the last 24 hours of traces and
their cost. Each project is its own key pair.

**Credentials:** the server's URL in `LANGFUSE_HOST` (or `LANGFUSE_BASE_URL`,
or the `host` setting), plus one public/secret key pair per project.

```toml
[sources.langfuse]
host = ""   # a URL or a list of URLs, tried in order; else LANGFUSE_HOST, then LANGFUSE_BASE_URL
keys = [{ public_env = "LANGFUSE_PUBLIC_KEY", secret_env = "LANGFUSE_SECRET_KEY" }]
# more projects: add { public_env = "...", secret_env = "...", label = "staging" }
```

**Alerts.** crit: no URL answers its health check. warn: a key pair rejected,
no key pair set.

**Details** keep this shape, which other panels read:
`{"version": "3.120.0", "projects": [{"project": "name", "traces_24h": 123,
"cost_24h_usd": 1.23}]}` (a project whose keys failed also carries `error`).

**Endpoints:** `GET {host}/api/public/health` (no credentials), then with
Basic auth `GET /api/public/projects`, `GET /api/public/metrics?query=...`,
and on servers older than the metrics API `GET /api/public/traces?limit=1`.
With an `http://` URL, keep it on a private network.

## `ai_spend`

Month-to-date API spend and prepaid credits: OpenRouter's remaining credits,
Anthropic's and OpenAI's admin cost reports, and xAI's prepaid balance. Any
subset works; only the providers with a key run.

**Credentials:** `OPENROUTER_MGMT_KEY` (or `OPENROUTER_API_KEY`),
`ANTHROPIC_ADMIN_KEY`, `OPENAI_ADMIN_KEY` (admin keys: ordinary API keys cannot
read costs), `XAI_MGMT_KEY` (a management key with BillingRead).

```toml
[sources.ai_spend]
openrouter_warn_usd = 10   # OpenRouter credits left
openrouter_crit_usd = 2
```

**Alerts.** OpenRouter credits below the thresholds. A provider whose key is
rejected shows its reason in the MONEY panel without an alarm.

**Endpoints:** `GET https://openrouter.ai/api/v1/credits` and `/api/v1/key`;
`GET https://api.anthropic.com/v1/organizations/cost_report` (daily buckets
from the 1st, UTC); `GET https://api.openai.com/v1/organization/costs`;
`GET https://management-api.x.ai/auth/management-keys/validation` and
`/v1/billing/teams/{team}/prepaid/balance`.

## `twilio`

Account balance and today's SMS count.

**Credentials:** `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` from the
environment, or from a service's own dotenv file named by `env_file` (read in
memory, never copied). The auth token travels only in the Basic auth header;
the account SID is part of Twilio's URL scheme.

```toml
[sources.twilio]
env_file = ""        # e.g. "/srv/sms/.env"; its values win over the environment
balance_warn = 20
balance_crit = 5
scope = "global"     # "local" when only one machine holds the credentials
```

With `scope = "local"`, set `hosts = ["that-machine"]` too: that machine
collects, and the hub reads its record through the fleet snapshot (`share`
follows `scope` unless set). Both are read when the sources are registered.

**Alerts.** crit: balance below `balance_crit`. warn: below `balance_warn`,
partial data, and (when `env_file` is set) credentials missing from it.

**Endpoints:** `GET
https://api.twilio.com/2010-04-01/Accounts/{sid}/Balance.json` and
`.../Usage/Records/Today.json?Category=sms`.

## Panels

| Panel | Shows |
|-------|-------|
| `edge` | UptimeRobot monitors, the `sites` endpoints, Cloudflare tunnels, traffic and inactive zones, Vercel production deploys |
| `infra` | Hetzner servers and Storage Box fill, GitHub CI per repo with open PRs, Sentry, and a one-line AWS summary when the AWS panel is not in the layout |
| `comms` | Resend domains and sends, Cloudflare Email Routing, Twilio SMS |
| `money` | Stripe balance, subscriptions and MRR, Anthropic and OpenAI month-to-date, OpenRouter credits, xAI, the Hetzner run-rate, GitHub Actions spend, the AWS month against its budget ([aws.md](aws.md)), Cursor on-demand spend, the Twilio balance |

A panel's note names a source whose reading is stale or whose last run failed
(`cloudflare ✖ 2h`). A service that answered with a reason and no data (a
send-only key, an expired login) shows the reason, dimmed.

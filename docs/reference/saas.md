# SaaS and cloud sources

Twelve sources watch the services a small team or a homelab runs on:
Cloudflare, Vercel, UptimeRobot, Hetzner, GitHub, Sentry, Stripe, Resend,
Langfuse, AI API spend (OpenRouter, Anthropic, OpenAI, xAI), AWS and Twilio.
The EDGE, INFRA, COMMS and MONEY panels show them (see [Panels](#panels)).

Each source turns itself on when its credentials are present: set the
variables in your shell or in a `[general] secrets_files` file, and run
`porchlight collect`. `porchlight sources` shows what runs here and why not.

## Shared behaviour

**Read-only.** Every request is a GET, apart from exactly three read-only POST
queries: Cloudflare GraphQL, GitHub GraphQL, and UptimeRobot's v2
`getMonitors` (only when the v3 API refuses). Nothing creates, changes or
deletes anything. The only subprocesses are `gh auth token` and three
read-only AWS CLI calls. A test audits the source file for these rules.

**Credentials** travel in request headers (UptimeRobot v2 takes its key in the
POST body, the way that API is defined). They never appear in a URL, an error,
a summary or the stored details: every result is scrubbed of credential values
once more before it is written. Redirects are never followed, so a credential
cannot be forwarded to another host, and a token is only ever sent to the host
it was issued for, never to one an API merely names.

**Timeouts.** Each request has a 6 second timeout, each AWS CLI call 15
seconds, and the collector's deadline caps every run.

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
`hosts`, `identities`, and `paths` (extra directories to search for `gh` and
`aws`). `ignore` lists match case-insensitive substrings of a name.

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

Hetzner Cloud servers (status, outgoing traffic against the included
allowance), Storage Box fill, and a monthly run-rate from the price list.
Hetzner has no billing API, so the run-rate (servers with their backup
surcharge, volumes, storage boxes) is an estimate, not an invoice.

**Credentials:** `HCLOUD_TOKEN` (a read-only project token is enough; the same
token reads the project's Storage Boxes).

```toml
[sources.hetzner]
storage_boxes = true
ignore = []                # server or storage box names to leave out
traffic_warn_pct = 90      # of a server's included outgoing traffic
box_warn_pct = 80
box_crit_pct = 90
```

**Alerts.** crit: a server off, deleting or unknown; a Storage Box at
`box_crit_pct`. warn: a server starting, stopping or migrating; traffic at
`traffic_warn_pct`; a Storage Box at `box_warn_pct` or not active; partial
data. A failed price-list read only drops the estimate.

**Endpoints:** `GET https://api.hetzner.cloud/v1/servers`, `/volumes` and
`/pricing`; `GET https://api.hetzner.com/v1/storage_boxes`.

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

## `aws_account`

The STS identity behind each AWS CLI profile, running EC2 instances in each
account, and (opt-in) month-to-date cost. Only the last four digits of an
account id are kept, and never the role session name.

**Credentials:** the AWS CLI's own: SSO, profiles or environment credentials.
The source is off when `aws` is not installed. An expired SSO login is a
warning with the `aws sso login --profile ...` fix.

```toml
[sources.aws_account]
profiles = []          # empty: AWS_PROFILE, then every profile in ~/.aws/config (up to eight)
regions = []           # empty: each profile's own region (else AWS_REGION, else us-east-1)
cost_explorer = false  # true adds month-to-date cost; AWS bills $0.01 per Cost Explorer call
```

Two profiles into one account are counted once.

**Alerts.** warn: every profile's login expired (with the fix), an EC2 query
failing.

**Commands** (read-only): `aws sts get-caller-identity`, `aws ec2
describe-instances --filters Name=instance-state-name,Values=running`, and
with `cost_explorer` `aws ce get-cost-and-usage`. The CLI runs with
`AWS_EC2_METADATA_DISABLED=true`, so a machine without credentials does not
wait on the instance metadata service.

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
| `infra` | Hetzner servers and Storage Box fill, GitHub CI per repo with open PRs, Sentry, AWS accounts |
| `comms` | Resend domains and sends, Cloudflare Email Routing, Twilio SMS |
| `money` | Stripe balance, subscriptions and MRR, Anthropic and OpenAI month-to-date, OpenRouter credits, xAI, the Hetzner run-rate, AWS Cost Explorer, Cursor on-demand spend, the Twilio balance |

A panel's note names a source whose reading is stale or whose last run failed
(`cloudflare ✖ 2h`). A service that answered with a reason and no data (a
send-only key, an expired login) shows the reason, dimmed.

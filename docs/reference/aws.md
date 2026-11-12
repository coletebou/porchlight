# The AWS account and its bill

Two sources and one panel answer "what runs in my AWS account, and what does it
cost?":

| Source | Scope | Runs | Calls |
| --- | --- | --- | --- |
| `aws_account` | global | hourly | free, read-only AWS CLI calls: the inventory in every enabled region, list prices, budgets |
| `aws_cost` | global, opt-in | hourly check, at most one request a day | Cost Explorer, which bills $0.01 a request |

The AWS panel shows them together (see [The AWS panel](#the-aws-panel)). MONEY
gets one line, the month against its budget; INFRA shows a one-line summary only
when the AWS panel is not in your layout.

Two developer-machine sources stay as they were ([dev.md](dev.md)): `aws_auth`
(ACCESS: which credentials the CLI on *this* machine would use) and `instances`
(INSTANCES: your dev boxes in one region, with private IPs and the command that
starts a stopped one). They run on the machine you log in to; `aws_account` and
`aws_cost` are account-wide, so in a fleet the hub collects them and every peer
renders the hub's copy.

Both sources run the official AWS CLI (no SDK, no dependency), with your own
credential chain: SSO, profiles, `credential_process`, environment keys. They are
off on a machine without `aws`.

---

## `aws_account`

One account: the first profile STS accepts. Every run reads, in each region it
covers, every instance that is not terminated (type, state, uptime, spot or
on-demand, tenancy), every EBS volume, the Elastic IPs and the dedicated hosts;
then prices them at list price, and reads the account's budgets.

```toml
[sources.aws_account]
profile = ""            # empty: AWS_PROFILE, last run's profile, then each profile in the AWS files (up to eight)
home_regions = []       # read every run; empty: the profile's region, else AWS_REGION, else us-east-1
sweep = 2               # the account's other enabled regions read per run, round robin (0: home regions only)
max_concurrent = 3      # aws processes at once; each one peaks near 130 MB
cost_explorer = "off"   # "daily": aws_cost asks Cost Explorer once a day ($0.01 a request)
cost_detail = "usage_type"  # the day's free second --group-by: "usage_type" (default), "region", "linked_account" or "none"
cost_forecast = false       # true: one more $0.01/day request (Cost Explorer's own GetCostForecast)
```

**The profile.** With `profile` set, that profile is the only one tried: if its
login has expired, the dashboard says so; it never falls back to another
identity (an administrator's SSO session, say) on its own. Without it, the
candidates are `AWS_PROFILE`, last run's profile, then the profiles of the AWS
config and credentials files in order (section names only: no key is ever read
out of either file); the first one STS accepts is used. With no AWS files at all
the CLI's own chain runs (environment credentials, for instance).

**Regions.** The home regions are read every run, and so is any other region that
held something last run. The account's other enabled regions
(`ec2 describe-regions`, once a day) are swept `sweep` per run, round robin, so a
box started anywhere appears within a few hours and is flagged while it sits
outside the home regions. A swept region skips Elastic IPs: a stray instance,
volume or dedicated host is what the sweep is for.

**Prices.** List prices from the AWS Price List API (`pricing get-products`, free),
cached for a week per instance type, volume type and host family, at most 24 new
lookups a run (a large account fills its cache over a few runs):

| Resource | Priced as |
| --- | --- |
| on-demand instance | the hourly rate for its type, Linux or Windows, shared or dedicated tenancy |
| spot instance | the current spot price in its Availability Zone (`ec2 describe-spot-price-history`, free) |
| instance on a dedicated host | nothing: the host bills |
| EBS gp3 | storage, IOPS above 3,000 and throughput above 125 MiB/s |
| EBS io1, io2 | storage and provisioned IOPS (io2 at its first tier) |
| EBS gp2, st1, sc1, standard | storage |
| dedicated host | its hourly rate |
| public IPv4 address | $0.005 an hour, attached or not |

Other platforms (RHEL, SUSE, a licensed Windows image) and anything AWS lists no
price for count as unpriced, never guessed. These are list prices: Savings Plans,
Reserved Instances, credits and discounts are not applied. The bill is Cost
Explorer's.

**The burn** is what accrues per hour now: running instances, every volume
(attached or not), each public IPv4 address, dedicated hosts. **Idle spend** is
what accrues per month for nothing: stopped instances' volumes and Elastic IPs,
unattached volumes, unassociated Elastic IPs, and dedicated hosts with no instance
on them.

**Budgets.** `budgets describe-budgets` (free) lists the account's cost budgets.
The account budget is the largest monthly one that counts the whole account. Its
filters may leave out record types, as the console's default for a new budget
leaves out credits and refunds; a budget narrowed any other way (a service, a tag,
a cost category, a linked account, a Bedrock model, a custom billing view, or a
kind of filter porchlight does not know yet) is not it. Its actual spend, which
AWS refreshes a few times a day, is the month to date in the panel when there is
one. Reading those filters takes AWS CLI 2.28.4 or newer (v1: botocore 1.40.4):
an older CLI leaves them out of its replies, so with one no budget counts as the
account's, and the panel's budget row says which CLI to install. The check is
`aws --version`, which calls no API.

**What is kept.** Counts, prices and names: the account id's last four digits,
never the whole id, the role session name or a public address. Any account id in
a profile name (the AWS SSO wizard's default names carry one) or in an error
(an access-denied message quotes the caller's ARN) is cut to its last four
digits before it is kept, and last run's profile is recognized by that masked
name. Name tags lose any control characters and are cut to 28 characters.

**A run** stays under about 40 seconds: the region reads, then the price lookups,
spot prices and budgets, each within what is left of that budget.

### Alerts

| Severity | When |
| --- | --- |
| warn | the month's spend is over the account budget: `aws: $150 spent this month, 125% of the $120 budget` |
| warn | anything outside the home regions: `aws: 1 instance, 1 volume in eu-west-1, outside the home regions` |
| warn | a dedicated host with no instance on it: it bills by the hour regardless |
| warn | a home or active region that could not be read on two runs in a row |

Deliberately not alerts: stopped instances, a single failed read (one miss is a
blip), a swept region that cannot be read (an organization policy that denies
the region), unpriced resources, and credentials that are missing or expired.
Credentials are a soft failure: the last inventory is kept, and the panel says
why it is old (`aws sso login --profile …` when the SSO login expired). The
ACCESS panel alerts when this machine's own credentials are rejected.

### Commands

All read-only, each with a timeout, through one helper that a test audits:
`aws sts get-caller-identity`, `aws ec2 describe-regions`, `describe-instances`,
`describe-volumes`, `describe-addresses`, `describe-hosts`,
`describe-spot-price-history`, `aws pricing get-products` and
`aws budgets describe-budgets`. The CLI runs with `AWS_EC2_METADATA_DISABLED=true`
(no wait on the instance metadata service), no pager, no prompt, and an
environment holding only the `AWS_*` settings: no other secret, and no service
bearer token (`AWS_BEARER_TOKEN_*`).

---

## `aws_cost`

The month's spend by day and by service, from Cost Explorer. Off unless
`[sources.aws_account] cost_explorer = "daily"` (`true`, from configurations
written for 0.1.0, means the same).

**At most one request a day.** Cost Explorer bills $0.01 a request, so this
source checks every hour and asks AWS at most once per UTC date, and never within
20 hours of its last attempt: the first run after 12:00 UTC (the previous day has
posted by then), or any run once 36 hours have passed (a machine that slept
through noon). That is about $0.30 a month.

The cap is enforced in the source, against the time of the last attempt, not by
the collector's schedule, so `porchlight collect --force`, `porchlight collect
aws_cost` and the retry after a failure never add a request. The attempt is noted
in the state directory before the request goes out, so a run the collector
abandons at its deadline, or a crash, still counts; if it cannot be noted, the
request is not made. The CLI makes a single attempt (`AWS_MAX_ATTEMPTS=1`, no
retries) and does not paginate (`--no-paginate`): a second page would be a second
billed request, so it is never asked for, and the result is marked partial
instead. Any failure, a refusal or a reply that cannot be read, waits the full
gap too. In a fleet only the hub asks: a peer whose hub is alive (its bundle is
under three days old) never does, even with `--force`, and shows the hub's
reading instead; it takes the source over only after the hub has been silent
for three days.

**The request.** Daily unblended cost grouped by service, from the first of the
month (or two weeks back, early in a month) through today, which is partial. A
second `--group-by` dimension rides along at no extra charge (one request, with
or without it): `cost_detail = "usage_type"`, the default, so the reply also
breaks each day down by usage type, which the panel uses to split the opaque
"EC2 - Other" bucket (and the EC2 compute bucket) into EBS, snapshots, NAT
gateways, public IPv4 addresses and idle Elastic IPs, and data transfer — the
rest falls into an `other` bucket (mostly instance-hours, already counted in
`services`):

```text
aws ce get-cost-and-usage --time-period Start=2026-10-01,End=2026-10-28 --granularity DAILY \
  --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE Type=DIMENSION,Key=USAGE_TYPE --no-paginate
```

Set `cost_detail = "region"` or `"linked_account"` instead to trade that
breakdown for a top list by region or linked account (also free: it swaps the
second dimension, it does not add one); `"none"` drops the second dimension
and asks only by service, as before 0.3.0.

It keeps the month to date, the last two weeks of daily totals, the top services
this month, the usage-type (or region/account) breakdown, and the average daily
spend over the last full week per bucket (EC2 instances, EC2-Other, VPC and
everything else) for the heuristic forecast.
A refusal (Cost Explorer not enabled for the account, a missing permission) is
shown in the panel and asked again the next day.

**The forecast.** By default the month line's forecast is a heuristic: the month
so far, plus each bucket's daily rate (the hourly model's, or Cost Explorer's own
average where the model only covers part of a bucket) carried to the end of the
month. Set `cost_forecast = true` to add a second, genuinely extra request the
same day — Cost Explorer's own `GetCostForecast`, a model trained on the
account's own history — and the panel shows that number instead:

```text
aws ce get-cost-forecast --time-period Start=2026-10-28,End=2026-11-01 \
  --metric UNBLENDED_COST --granularity MONTHLY
```

That makes two $0.01 requests on the day both run (about $0.60 a month instead
of $0.30). A forecast failure never costs the base reading, already billed that
day; the panel falls back to the heuristic.

To stop asking Cost Explorer at all, set `cost_explorer = "off"` (the default),
or `[sources.aws_cost] enabled = false`. Per month, opted all the way in: at
most 31 requests for the usage/usage-type query, plus 31 more for the forecast —
**$0.31 to $0.62** total, never more, no matter how often the collector runs.

---

## Permissions

A read-only policy for the profile the dashboard reads with:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["ec2:DescribeRegions", "ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeAddresses",
               "ec2:DescribeHosts", "ec2:DescribeSpotPriceHistory", "pricing:GetProducts", "budgets:ViewBudget",
               "ce:GetCostAndUsage", "ce:GetCostForecast"],
    "Resource": "*"
  }]
}
```

`sts:GetCallerIdentity` needs no permission. Drop `ce:GetCostAndUsage` when Cost
Explorer is off, and `ce:GetCostForecast` unless `cost_forecast = true`. A
missing permission shows as that resource's error in the panel, never as a
guess.

---

## The AWS panel

```text
 AWS ● ────────────────────────────────────────────── prod …4821
 month    $288 Oct · 72% of $400 budget · ≈$343 by Oct 31
 daily    ▇▇▆▅▇███▆▅▆███ · yesterday $11.37 · 7d avg $10.10
 burn     $0.48/h ≈$11/day · ec2 $0.36 · ebs $0.09 · ip $0.02
 services EC2 $205 · EC2-Other $58.21 · VPC $15.62 · +3 $8.73
 usage    ebs $34.10 · xfer $9.15 · snap $4.85 · nat $3.20 · ip $2.60
 running  ▶ api-1    m7i.l   21d $0.10/h
          ▶ api-2    m7i.l   21d $0.10/h
          ▶ build-eu t3.l     2h $0.09/h eu-west-1
          ▶ worker-1 c7i.xl   3d $0.07/h spot
 stopped  2 · $54/mo idle: batch-1 $40 · sandbox $14
```

| Row | Shows |
| --- | --- |
| month | the month to date (the account budget's spend, else Cost Explorer's), against the budget, and a forecast. With `cost_forecast = true`, Cost Explorer's own `GetCostForecast`; otherwise a heuristic: the month so far plus a daily rate carried to the month's end, per bucket the hourly model's (instances, which it knows to the hour) or, where the model covers only part of a bucket (EC2-Other also holds snapshots, NAT gateways and transfer), the larger of it and Cost Explorer's average of the last full week |
| daily | a spark of the last two weeks' full days, the latest of them (named by its date unless it was yesterday: the read's own last day was partial), and the week's average (Cost Explorer) |
| burn | the run-rate per hour now, about per day, and its parts: compute, EBS, public IPv4, dedicated hosts, how many resources are unpriced |
| services | this month's spend by service (Cost Explorer) |
| usage | with the default `cost_detail = "usage_type"`: EC2 and EC2-Other broken into ebs, snap(shot), nat (gateway), ip (public IPv4 / idle EIP) and xfer (data transfer); `other` (mostly instance-hours, already in `services`) is shown only when nothing else is |
| region / account | with `cost_detail = "region"` or `"linked_account"`: the month's top regions or linked accounts, in place of the `usage` row |
| running | each running instance: type, uptime, price per hour, spot, its region when outside the home regions |
| host | dedicated hosts: family, region, age, instances on it, price |
| stopped | the idle spend per month, and what makes it up |
| partial | a region that could not be read |

The note is the profile and the account's last four digits, or how old the
reading is when the source is failing. The panel turns amber over the budget and
on any of the source's alerts.

Every row is built to fit the column, down to porchlight's narrowest (58 cells):
a list says how many items it left out (`+3`, or `+3 $8.73` with their sum)
instead of cutting a word, and the less important parts of a row (the forecast's
date, the IPv4 share of the burn) give way first.

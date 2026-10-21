# Developer sources

Five sources for a development or work machine, and the four panels that show
them:

| Source | Scope | Panel | Shows |
| --- | --- | --- | --- |
| `docker` | local | DOCKER | containers by compose project, restart loops, fresh crashes, expected containers |
| `aws_auth` | local | ACCESS | the credentials the AWS CLI would use, the SSO token clock, an STS identity check |
| `instances` | local, `#dev` hosts | INSTANCES | your EC2 instances: type, private IP, uptime |
| `prs` | global | PULL REQUESTS | your open pull/merge requests on GitHub and GitLab, and the ones waiting on your review |
| `reach` | local, opt-in | ACCESS | ssh routes (a TCP connect, no login) and the endpoints you depend on |

One rule runs through all of them: what is merely environmental renders as
state and never alerts. The container engine not started, no AWS login on this
box, being off the VPN, a stopped stack or a stopped instance are all shown,
dimmed, and none of them raises an alert. Alerts are for things you can act
on.

Every call is read-only: `docker ps`, `aws sts get-caller-identity`,
`aws ec2 describe-instances`, one GitHub GraphQL query, GitLab `GET`s,
`ssh -G`, TCP connects, and `GET`s to your endpoints. No credential is ever
written to the cache: tokens stay in memory, AWS keys are never read out of
the credentials file, and error text is scrubbed before it is stored.

The network panel already shows Tailscale, ssh tunnels and dev ports, so
these panels do not repeat them.

---

## docker

Every container the engine knows about (`docker ps -a`), grouped by its
compose project (`com.docker.compose.project`), plus standalone containers.
For each running stack: how many containers are up out of how many should be,
health checks still starting, one-shots that finished, deliberate stops, and
the problems: unhealthy, restarting, exited. Stopped stacks are listed on one
line.

How an exited container is read:

| Exit | Meaning |
| --- | --- |
| `0` | a one-shot that finished (migrations, seeds): done, not counted as missing |
| `130`, `143` | SIGINT or SIGTERM: `docker stop`, `compose stop`, Ctrl-C. A choice, not a crash |
| anything else | a crash. Shown with its age; alerts only while younger than `recent_crash_hours`, and only in a stack that is still running |

A project whose containers all crash-loop counts as running, not stopped.

```toml
[sources.docker]
binary = ""               # a name or path; default: docker, else podman
ignore_projects = []      # compose projects (globs) to leave out entirely
expected = []             # containers that must be running (see below)
expected_file = ""        # a JSON file listing more of them
recent_crash_hours = 6    # an older crash is history, not news
```

The CLI is looked for on `PATH` and where desktop installers put it: Docker
Desktop, OrbStack, Rancher Desktop and the Podman installer. colima drives
Homebrew's `docker`, which is on the usual paths. The CLI picks its engine the
usual way (the current context, `DOCKER_HOST`, podman's `CONTAINER_HOST` in the
collector's environment).

### Expected containers

`expected` names containers that must be running. An entry is:

* a container name (`"registry"`) or a glob (`"db-*"`);
* `project/service` (`"widgets/web"`), also globbable (`"widgets/*"`, `"*/db"`)
  when the entry contains a slash.

Every entry must match a running container. A glob also reports each
container it matches that is not running, except one-shots that finished with
exit 0. `expected_file` is a JSON list of entries (or an object with a
`"running"` or `"expected"` list) for inventories another tool generates; it
is read on every run.

With `expected` set, the record's `badge` is running/expected
(`"11/12"`); without it, running out of what the running stacks should have
up. Put it in the fleet table with `[fleet] badge = "docker"`.

### Alerts

| Severity | When |
| --- | --- |
| crit | a container is stuck restarting: `restart loop: widgets-worker-1` (fix: `docker logs --tail 50 …`) |
| crit | an expected container is not running: `container down: web, db-*` |
| warn | a running stack has unhealthy containers or a crash younger than `recent_crash_hours` |
| warn | a standalone container is unhealthy |
| warn | `expected_file` is unreadable, or expected containers cannot be checked (no access to the engine's socket) |

Deliberately not alerts: the engine not running or not installed, no
permission on its socket, a stopped stack (even one with crashed containers),
exits 0, 130 and 143, crashes older than `recent_crash_hours`, ignored
projects. The exception is `expected`: if you listed containers, an engine
that is down means they are down, and that alerts.

---

## aws_auth

What the AWS CLI would use on this machine, read from the files without the
network:

* profiles holding static keys (in `credentials` or `config`), and whether
  `AWS_ACCESS_KEY_ID` is set in the environment (presence only);
* SSO profiles (`sso_session` or `sso_start_url`) and their account;
* the newest SSO access token's expiry from `~/.aws/sso/cache`, and whether it
  carries a refresh token (the CLI then renews it by itself). Client
  registrations (`botocore-client-*`) and files without a `startUrl` (other
  tools share the directory) are skipped.

Then one `aws sts get-caller-identity`, which says who you are (principal and
account) or why not. The call repeats only when something changed (the
credential or config file's modification time, the newest token's expiry,
`AWS_PROFILE`, whether `AWS_ACCESS_KEY_ID` is set, the `profile` or `region`
setting), once `recheck_minutes` have passed, or after a network failure.

```toml
[sources.aws_auth]
region = ""                    # default: the CLI's own (AWS_REGION, the profile's region)
profile = ""                   # default: AWS_PROFILE, else the default profile
login_command = "aws sso login"
recheck_minutes = 30
paths = []                     # extra directories to look for `aws` in
```

The source is off on a machine without the `aws` CLI. Environment:
`AWS_PROFILE`, `AWS_REGION`/`AWS_DEFAULT_REGION`, `AWS_CONFIG_FILE` and
`AWS_SHARED_CREDENTIALS_FILE` are honored as the CLI honors them; `AWS_*`
settings from your secrets files are passed to the CLI. Service bearer
tokens (`AWS_BEARER_TOKEN_<SERVICE>`, such as an AI SDK's Bedrock key) are
removed from the CLI's environment: each authenticates one unrelated API and
has no business on an STS or EC2 call. A `profile` setting is passed as
`--profile`, which (unlike `AWS_PROFILE`) also outranks keys in the
environment.

The STS outcome is one of:

| State | Meaning |
| --- | --- |
| `ok` | the principal (`role/Name` for an assumed role) and the account |
| `rejected` | credentials exist and AWS says no: expired, invalid, a failed SSO refresh |
| `none` | nothing configured |
| `unreachable` | the network: no route, DNS, a timeout |
| `error` | anything else, with the CLI's own words |

### Alerts

| Severity | When |
| --- | --- |
| warn | `AWS credentials rejected (ExpiredToken)`, with `login_command` as the fix (`--profile` added when `profile` is set; `aws configure` instead when this machine has static keys and no SSO profile) |

Deliberately not alerts: no credentials configured, STS unreachable, an SSO
access token past its expiry (with a refresh token the CLI renews it, and the
STS check is the verdict).

---

## instances

Your EC2 instances from `aws ec2 describe-instances` (terminated ones left
out), running first: name (the `Name` tag, else the id), type, private IP,
and uptime since the last start. Stopped instances are summarized on one line
with a command that starts them.

```toml
[sources.instances]
hosts = ["#dev"]          # where it runs (the default: machines with role dev)
region = ""               # default: the CLI's own
profile = ""              # default: the CLI's own
filters = []              # e.g. ["Name=tag:Owner,Values=me"]; default: every instance you can see
cache_file = ""           # a TSV fallback for when AWS says no (see below)
start_hint = "aws ec2 start-instances --instance-ids {id}"   # {id} and {name} are filled in
limit = 50
paths = []
```

`filters` are passed to `--filters` as written. When none of them filters on
`instance-state-name`, porchlight adds `running,stopped,stopping,pending`. The
default `start_hint` gets `--region` and `--profile` appended when those are
set.

When the call fails (expired credentials, off the network), `cache_file` is
read instead if it is newer than the last list: one instance per line, six
tab-separated columns, the CLI's own text output for this query:

```text
name	type	private-ip	instance-id	launch-time	state
devbox	m7i.xlarge	10.0.1.23	i-0abc3456def789012	2026-10-18T08:00:00+00:00	running
```

Without a usable cache file the last list is kept (marked stale). On a
machine with no AWS credentials at all the panel stays hidden.

Alerts: none. A stopped or pending instance is state.

---

## prs

Your open pull requests on GitHub and merge requests on GitLab, newest first,
and the ones waiting on your review. Each row: mergeability (conflicts or
not), approvals, changes requested, the CI result of the head commit, draft,
last update.

* **GitHub**: one GraphQL query (`POST /graphql`, read-only) running two
  searches, `is:pr is:open author:@me` and `is:pr is:open
  review-requested:@me`. The token is `GH_TOKEN`, else `GITHUB_TOKEN`, else
  whatever `gh auth token --hostname github.com` prints (the host is always
  named: bare `gh auth token` answers for `GH_HOST`). For GitHub Enterprise set
  `github_url` to `https://ghe.example.com/api`; the token then comes from
  `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN` or
  `gh auth token --hostname ghe.example.com`.
* **GitLab**: `GITLAB_TOKEN` (scope `read_api`), sent only to its own
  instance: `gitlab_url`, else the one glab is pointed at (`GITLAB_HOST`,
  `GL_HOST`, `GITLAB_URI`), else gitlab.com. `GET
  /api/v4/user` (who you are), then `GET /api/v4/merge_requests` twice:
  `scope=created_by_me` and `reviewer_username=<you>`. For your own first
  `detail` MRs it also reads `…/approvals` and the newest `…/pipelines` entry;
  review rows take CI from `detailed_merge_status`.

```toml
[sources.prs]
github = true                        # used when a token is found
github_url = "https://api.github.com"
gitlab = "auto"                      # auto: on when GITLAB_TOKEN is set; true: required; false: off
gitlab_url = ""                      # default: GITLAB_HOST / GL_HOST / GITLAB_URI, else https://gitlab.com
repos = []                           # an allowlist: "acme/widgets", "acme/*", "group/sub/*"
limit = 20                           # rows per list (up to 50)
detail = 10                          # your own GitLab MRs that get the approvals and pipeline reads
```

As a global source it is collected on the hub and shipped to peers, so one
machine's tokens serve the fleet.

If one forge fails (a self-hosted GitLab off VPN, say), its last rows are
kept and the panel says so while the other forge stays live. If every forge
fails, the last list is kept and the panel shows its age.

Every row has the same shape:

```json
{"forge": "gh", "ref": "acme/widgets#128", "n": 128, "title": "…", "merge": "mergeable",
 "approvals": 2, "changes": 0, "ci": "success", "draft": false, "updated": 1792573200,
 "url": "https://github.com/acme/widgets/pull/128"}
```

`merge` is `mergeable`, `conflicts` or `unknown` (not computed yet); `ci` is
`success`, `failure`, `pending` or `null`; GitLab refs read `group/project!34`.

### Alerts

| Severity | When |
| --- | --- |
| warn | one of your own PRs has merge conflicts |
| warn | changes were requested on one of your own PRs |
| warn | CI is failing on one of your own PRs |

Deliberately not alerts: drafts (whatever their state), review requests
waiting on you, pending CI, a forge that is unreachable or rejects the token
(the panel shows it), no GitHub or GitLab credentials on this machine (the
panel stays hidden).

---

## reach

"Can I reach what I need from here?" Two kinds of check, run in parallel:

* **ssh routes**: each alias is resolved offline with `ssh -G` (your config,
  no connection), then a TCP connection is opened to its `HostName:Port`, which
  proves the VPN or VPC route without logging in. With a `ProxyJump`, the first
  hop is probed instead, since that is where the packets go. A `ProxyCommand`
  route cannot be probed from outside and reads as unknown.
* **endpoints**: a DNS lookup, then a `GET` that does not follow redirects (a
  302 to a login page counts as up; anything under 500 is up). When the direct
  attempt fails at the network level, the same `GET` is retried through a
  SOCKS5 proxy with `curl --socks5-hostname`, so a VPC-only hostname resolves at
  the far end. The panel marks those with ⇄.

```toml
[sources.reach]
ssh = ["bastion", "devbox"]
endpoints = [
  { label = "git", url = "https://git.example.com/" },
  { label = "wiki", url = "https://wiki.internal.example.com/", verify = false },
]
socks_port = 1080   # default: a live `ssh -D` proxy found by the procs source
```

The source is opt-in: it runs once `[sources.reach]` exists. Without
`socks_port`, it uses the first live `ssh -D` tunnel in the `procs` source's
census, so `ssh -f -N -D 1080 bastion` is all it takes to be detected; the panel
suggests exactly that when endpoints are unreachable and no proxy is running.

### Alerts

| Severity | When |
| --- | --- |
| warn | an endpoint answers 5xx on two runs in a row (`ci answers HTTP 503`) |

Deliberately not alerts: no route at all (off VPN), a single 5xx (deploys
restart things), a refused or timed-out ssh route, an unresolvable name.

---

## Panels

| Panel | Reads | Note (top right) |
| --- | --- | --- |
| DOCKER | `docker` | the badge (`11/13 up`) and staleness |
| PULL REQUESTS | `prs` | the forges and your open count (`gh+gl · 6 open`) |
| ACCESS | `aws_auth`, `reach` | staleness |
| INSTANCES | `instances` | `3/5 running`, and the cache file's age when the list came from it |

Each panel is skipped when its source has nothing to say on this machine: no
container engine (unless you configured `[sources.docker]`), no forge
credentials, an AWS CLI that was never set up, no instances visible.

# Moving from fleet-motd

Porchlight is the maintained terminal dashboard. It carries forward the cached
collector and SSH fleet model, plus the GCP/Azure account checks and additional
forge providers from fleet-motd.

Install Porchlight and start with `porchlight init`. It uses its own
`~/.config/porchlight/config.toml` and `~/.cache/porchlight`, so the old JSON
configuration is not loaded automatically. Run `porchlight collect` and
`porchlight doctor`, inspect the dashboard, then use `porchlight install all`
when you are ready to switch your login hook and collector timer. Remove the
old fleet-motd hook and use its `./install.sh --uninstall` to stop its timer.

| fleet-motd | Porchlight |
| --- | --- |
| `gcp` | `[sources.gcp_auth]` (`login_hint` becomes `login_command`) |
| `azure` | `[sources.azure_auth]` (`login_hint` becomes `login_command`) |
| `mrs.provider = "gitea"` plus `url`, `repo` | `[sources.prs]` with `gitea = true`, `gitea_url`, `gitea_repos` |
| `mrs.provider = "forgejo"` plus `url`, `repo` | `[sources.prs]` with `forgejo = true`, `forgejo_url`, `forgejo_repos` |
| `mrs.provider = "bitbucket"` plus `repo` | `[sources.prs]` with `bitbucket = true`, `bitbucket_repos` |
| `~/.config/motd/env` | a secrets file named in `[general] secrets_files` |
| `hosts.<name>` | `[hosts."name-or-glob"]` overrides |
| `#hub` plus `peers` | `[fleet] peers` |
| `motd mrs`, `motd ec2`, `motd repo` | `porchlight prs`, `porchlight instances`, `porchlight repos` |

Forgejo uses `FORGEJO_TOKEN` rather than sharing `GITEA_TOKEN`. For Bitbucket,
replace any app password with an Atlassian API token and use `BITBUCKET_EMAIL`
and `BITBUCKET_API_TOKEN`, or keep a supported Bearer access token in
`BITBUCKET_TOKEN`. Full examples and the queried endpoints are in the
[developer-source reference](reference/dev.md).

Fleet-motd's notification senders, declarative `checks` rule engine and history
sparklines remain available in its archived source. Porchlight has built-in
health, certificate, backup and file checks, plus [custom commands and Python
plugins](plugins.md); it does not translate the old rules automatically.

AI plan sources also have different defaults: Porchlight detects installed CLI
configurations. Disable unwanted probes with `[sources.<name>] enabled = false`
or an identity allowlist before collecting. For a fleet, configure Porchlight's
SSH peers and caches on each host before switching the hub.

# porchlight: guide for coding agents

A fast, fleet-aware login dashboard (MOTD) for the terminal: vitals, services, AI agent/plan
limits, containers, pull requests, cloud status and alerts, ranked into one list and rendered from
cache in milliseconds. Humans start at [README.md](README.md) and
[CONTRIBUTING.md](CONTRIBUTING.md); this file is what an agent needs on top of them. `CLAUDE.md`
only imports this file — edit this one.

## Read before changing behaviour

- [CONTRIBUTING.md](CONTRIBUTING.md) — ground rules, dev loop, and the exact steps for adding a
  source (most new work is this). Read it before writing a new source or panel.
- [docs/architecture.md](docs/architecture.md) (collector vs. render split, fleet hub/peer model),
  [docs/security.md](docs/security.md) (credential handling — read before touching `secrets.py` or
  any source that takes a token), [docs/plugins.md](docs/plugins.md) (the source contract for both
  custom commands and Python plugins).

## Hard rules

- **Standard library only, Python 3.9+.** No runtime dependencies (the vendored `tomli` under
  `porchlight/_vendor/` covers TOML on 3.9/3.10). A PR that adds a dependency is out of scope.
- **The login render never touches the network and must stay fast** (tens of milliseconds).
  Anything that can take more than a few milliseconds belongs in a source run by the collector, not
  in a panel or a live probe.
- **Read-only, always.** Sources observe; they never change remote state, and never refresh or
  write a credential themselves (an official cloud CLI may maintain its own login cache while
  porchlight makes read-only calls through it — document that if you rely on it; don't add new
  login/export/refresh calls).
- **No secrets in output.** Credentials go in request headers or a child process's environment,
  never a URL, argv, a cache record, a log line or an error message. Any new code that handles a
  credential needs a test proving it doesn't leak (see `tests/test_hardening.py` for the pattern).
- **State is not an alarm.** Alert on things to act on; show, don't alert on, a stopped dev server,
  an offline laptop, or Docker simply not running.
- Report a real vulnerability through GitHub's private **Report a vulnerability** flow
  (SECURITY.md), never a public issue.
- Never skip, weaken or delete a test to get green; change one only when the intended behavior
  changed, and say why in the PR. Conventional Commits (`feat:`, `fix:`, `docs:`, …).

## Where things are, and what a change can break

| Path | Holds | Breaks |
|---|---|---|
| `porchlight/core.py` | process/HTTP helpers, caching, number formatting — imported by the login render, must stay cheap to import | everything; see Known traps below before using `run()`/`parallel()` |
| `porchlight/sources/` | one module per source family (`@source(...)`-decorated functions), the collector's work | that source's panel and any test in `tests/test_*` for it |
| `porchlight/panels/` | rendering for each panel; must return `None` with no data and fit `x.cw` at any width | the dashboard layout, the width-matrix CI check |
| `porchlight/collect.py`, `porchlight/render.py`/`ui.py` | the collector loop and the renderer | TTL/timeout/failure isolation, or the actual on-screen output |
| `porchlight/config.py`, `hosts.py` | config schema, host-glob overrides, identities | `porchlight doctor`'s validation, per-host behavior |
| `porchlight/secrets.py` | secrets-file parsing and redaction | the security guarantees in docs/security.md |
| `porchlight/snapshot.py` | the hub/peer fleet push-pull | the FLEET panel and multi-host data |
| `porchlight/data/demo/*.json` | fictional sample data for `porchlight demo` and screenshots | the demo command and `scripts/screenshots.sh` |
| `docs/reference/*.md` | per-source docs (what it shows, settings, alerts, endpoints called) | nothing in code, but required for every new source (CONTRIBUTING step 4) |

## Checks: run what your diff can break, let CI run the rest

```sh
python3 -m unittest discover -s tests          # whole suite, no network
python3 -m unittest tests.test_sources          # one module
python3.9 -m unittest discover -s tests         # the oldest supported Python too
ruff check porchlight tests scripts             # lint (CI runs it; optional locally)
bin/porchlight demo                             # run from the checkout, no install step
```

- **The full gate is CI's job**: `.github/workflows/ci.yml` runs on every push to `master` and PR —
  a test matrix across `{ubuntu, macos} x py{3.9..3.13}`, a demo-render check at five widths plus an
  ASCII-mode non-ASCII-byte check, a CLI smoke test on an empty machine (`collect`, default render,
  `doctor`, `sources`, `init --print` round-tripped through `doctor`), a wheel build+install smoke,
  and `ruff`. It triggers automatically on push/PR; `gh pr checks` watches an open PR's runs.

## Running it

```sh
git clone https://github.com/coletebou/porchlight && cd porchlight
bin/porchlight demo          # sample fleet, zero setup
bin/porchlight init          # write a config from what this machine actually has
bin/porchlight collect       # fill the caches once
```

## Known traps

- `core.run(argv, ...)` never raises — it returns `(rc, stdout, stderr)`, with `rc` 124 for a
  timeout and 127 for a missing binary. Check `rc` before parsing `stdout`: an empty string from a
  124/127 is not the same as a real empty result, and a source that skips the check can render a
  confident-looking blank instead of failing visibly.
- `core.parallel(jobs, ...)` deliberately swallows every exception per job and records `None` (one
  broken probe must never blank the whole dashboard). That also means a bug you introduce while
  developing a new source looks identical to "no data" on the dashboard — call the source function
  directly in a shell/test while developing it, don't debug it only through the rendered panel.
- A new source with no fixture in `porchlight/data/demo/<area>.json` won't show up in `porchlight
  demo` or in regenerated screenshots (`scripts/screenshots.sh`) — easy to forget; CONTRIBUTING's
  "Adding a source" steps cover it but nothing enforces it automatically.

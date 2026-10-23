# Contributing

Thanks for helping! porchlight aims to stay small, fast and dependency-free.

## Ground rules

- **Standard library only**, Python 3.9+. No runtime dependencies. (TOML on
  3.9/3.10 comes from the vendored `tomli`.)
- **The login path never touches the network.** Anything that can take more
  than a few milliseconds belongs in a source (the collector), not in a panel
  or a live probe.
- **Read-only.** Sources observe; they never change remote state or directly
  refresh a token or write a credential. Official cloud CLIs may maintain their
  own login caches while performing read-only queries; document this behavior.
- **No secrets in output.** Credentials go in headers or a child's
  environment, never URLs, argv, records, logs or errors. Add a test that
  proves it for anything new that handles a credential.
- **State is not an alarm.** Alert on things to act on: a stopped dev server,
  a laptop off the VPN or Docker not running are states to *show*.

## Development

```sh
git clone https://github.com/coletebou/porchlight && cd porchlight
bin/porchlight demo                         # run from the checkout, no install
python3 -m unittest discover -s tests       # the whole suite, no network
ruff check porchlight tests scripts         # lint (optional locally; CI runs it)
```

Run the suite on the oldest supported Python too (`python3.9 -m unittest
discover -s tests`, or Apple's `/usr/bin/python3` on a Mac).

Tests import `tests/support.py` first: it points the cache and config at a
scratch directory and pins the color mode, and gives you `host(toml)`,
`put(name, data)`, `FakeHTTP` and `ctx()`.

## Adding a source

1. Write it in the right module under `porchlight/sources/` (or a new one,
   added to `sources/__init__.py`): `@source("name", ttl=..., timeout=...,
   scope=..., defaults={...})`. See [docs/plugins.md](docs/plugins.md#the-source-contract).
2. Render it in a panel under `porchlight/panels/` (or extend an existing
   panel). Return `None` without data. Fit every line to `x.cw`.
3. Add realistic, entirely fictional sample data to
   `porchlight/data/demo/<area>.json` so `porchlight demo` shows it.
4. Document it in `docs/reference/<area>.md`: what it shows, settings and
   defaults, env vars, what it alerts on, and which endpoints it calls.
5. Tests: parsing, status logic, alerts, no secret leakage, and the panel at
   widths 60/90/130.

## Screenshots

```sh
scripts/screenshots.sh     # regenerates docs/img/*.svg from the demo
```

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
`docs:`, `test:`, `refactor:`, `chore:`.

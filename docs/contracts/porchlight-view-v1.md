# Contract: `porchlight-view v1`

`porchlight view` prints the dashboard as one JSON document: the same panels
the login render shows, built by the same code (`render.prepare`), as plain
text plus the selectable items and actions behind them. It is the shared
component between the login dashboard, `porchlight -i`, and other programs
that want the dashboard without parsing ANSI (ai-cli's `ai --motd`, a status
bar, a voice assistant's "what needs me?").

```sh
porchlight view                 # the whole dashboard, pretty-printed
porchlight view todos drover    # just those panels
porchlight view --compact-json  # one line, for programs
```

It reads the cache only (no network), like a login. Exit code 0.

## Document

```json
{
  "schema": "porchlight-view",
  "v": 1,
  "porchlight": "0.2.0",
  "host": "atlas", "label": "atlas", "identity": "personal",
  "ts": 1794057333.6,
  "fleet": {"role": "peer", "hub": "atlas", "bundle_age": 132.5},
  "alerts": [
    {"sev": "warn", "msg": "demo-review@atlas is waiting on you: Push now?", "src": "drover", "fix": "", "new": true}
  ],
  "panels": [
    {
      "key": "todos", "title": "TODOS", "level": "warn", "note": "30 open",
      "lines": ["open      30 · badland 4 · drover 15 · …"],
      "items": [
        {
          "id": "todos:badland:6ac18ce5", "text": "upgrade to latest models", "level": "ok",
          "detail": ["list Badland DeV · priority none · created 2026-10-03"],
          "url": "https://badland.ai/todos/69bc3f2f",
          "actions": [{"name": "todos.done", "label": "mark done", "key": "d",
                       "args": {"id": "badland:6ac18ce5"}, "writes": true,
                       "confirm": "Mark 'upgrade to latest models' done in Badland?"}]
        }
      ]
    }
  ]
}
```

- `fleet.role`: `hub` (this machine pushes the bundle), `peer` (it renders the
  hub's bundle; `hub` names it and `bundle_age` says how old it is, seconds) or
  `solo`.
- `alerts`: ranked crit → warn → info, as the attention strip shows them.
- `panels[].lines`: the panel's rows with color removed, fitted to a 120-column
  layout. For display only; parse `items`, not lines.
- `panels[].items`: may be empty. `id` is stable across runs. `actions[].name`
  is a handler in `porchlight/actions.py`; `writes: true` actions are refused
  unless the machine's config sets `[interactive] allow_writes = true`.
- Every string has had token shapes masked (`secrets.mask_shapes`). The
  document never holds a credential.

Additive changes (a new field, a new panel) keep `v: 1`. Consumers must ignore
fields they do not know.

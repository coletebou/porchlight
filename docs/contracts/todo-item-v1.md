# Contract: `todo-item v1`

The `todos` source (`porchlight/sources/todos.py`) folds open items from every
system that tracks work into one list. This file fixes the record shape so the
TODOS panel, `porchlight -i`, `porchlight view`, a fleet peer reading the hub's
bundle, and other dashboards built on the same data (a fleet-motd port, ai-cli)
agree on it. Additive changes (a new optional field, a new provider) keep `v1`;
renaming or retyping a field is `v2`.

## Settings (`[sources.todos]`, scope `global`, opt-in)

```toml
[sources.todos]
keep = 200                       # items kept in the record (and the bundle)
shown = 6                        # rows the panel lists
ask_pattern = ""                 # e.g. '\bV-\d{2,3}\b': the "track every ask" view
ask_systems = ["badland", "github", "drover", "brain"]   # where every ask should be recorded

[sources.todos.badland]          # a Badland-compatible todos API; off while base_url is ""
base_url = ""                    # e.g. "https://api.example.com"
web_url = ""                     # item links: {web_url}/todos/{listId}
key_env = "BADLAND_API_KEY"      # Bearer key, read from the environment / secrets files
lists = []                       # list titles or ids to read; empty = every non-archived list

[sources.todos.drover]           # features from the local Drover daemon; off without a daemon
enabled = true
states = ["not-started", "in-progress"]

[sources.todos.reflections]      # open reflections (action, question) from the Drover daemon
enabled = true
kinds = ["action", "question"]

[sources.todos.github]           # open issues; off while both labels and query are empty
owner = ""                       # user or org: user:<owner>
labels = []                      # one search per label, e.g. ["voice-gateway"]
query = ""                       # a raw search query instead (is:open is:issue is added)
token_env = ["GH_TOKEN", "GITHUB_TOKEN"]   # first set one wins, then `gh auth token`

[sources.todos.file]             # todo.txt lines or Markdown "- [ ]" boxes
paths = []
```

A brain counts as a system for `asks` only through the `knowledge` source's
recent entries (titles), never by an extra brain query from here.

## The record (`porchlight json todos` → `data`)

```json
{
  "v": 1,
  "items": [Item, ...],
  "providers": {
    "badland":     {"ok": true,  "open": 4, "ms": 212, "error": ""},
    "drover":      {"ok": true,  "open": 15, "ms": 40, "error": ""},
    "reflections": {"ok": true,  "open": 9, "ms": 900, "error": ""},
    "github":      {"ok": false, "open": 0, "ms": 0, "error": "no GitHub token (GH_TOKEN / GITHUB_TOKEN / gh)"},
    "file":        {"ok": true,  "open": 2, "ms": 1, "error": ""}
  },
  "total": 30,
  "asks": {
    "pattern": "\\bV-\\d{2,3}\\b",
    "ids": {
      "V-41": {"badland": "open", "github": "open", "drover": "in-progress", "brain": "noted"},
      "V-43": {"github": "closed"}
    },
    "systems": ["badland", "github", "drover", "brain"]
  },
  "alerts": []
}
```

- `items`: open items only, sorted by priority (urgent, high, medium, low,
  none), then due date (soonest first, none last), then `updated` (newest
  first); at most `keep` (default 200) so the record and the hub bundle stay
  small.
- `providers`: one entry per provider that is configured (`enabled`) on this
  machine, including ones that failed. A failed provider keeps its previous
  items (from `ctx.prev`) and says why in `error`; it never blanks the others.
  `error` is a short reason, never a response body, never a credential.
- `total`: open items across providers before the `keep` cut.
- `asks` (optional): present when `ask_pattern` is set. For every ask id the
  pattern finds in any item title (or brain entry title), which systems hold
  it and in what state. `systems` lists the systems the source could read this
  run, so a gap ("V-07 has no GitHub issue") is only claimed for a system that
  answered. This is the "track every ask" view.
- `alerts`: `warn` for an item overdue by more than a day with priority high
  or urgent; none for a provider failing (that is a state, shown in the panel).

## Item

| Field | Type | Meaning |
|---|---|---|
| `id` | str | `"<provider>:<ref>"`, stable across runs |
| `provider` | str | `badland` \| `drover` \| `reflections` \| `github` \| `file` \| a plugin's name |
| `ref` | str | the provider's own id (Badland item id, Drover fid, reflection id, `owner/repo#123`, `path:line`) |
| `title` | str | one line, control characters removed, token shapes masked, at most 200 chars |
| `group` | str | the list, repo or label it belongs to (`"Voice Gateway"`, `"dotfiles"`, `"coletebou/porchlight"`) |
| `state` | str | `open` \| `in-progress` \| `waiting` \| `blocked` |
| `priority` | str | `urgent` \| `high` \| `medium` \| `low` \| `none` |
| `due` | number \| null | epoch seconds |
| `created`, `updated` | number \| null | epoch seconds |
| `url` | str | a web link, `""` when the provider has none |
| `tags` | [str] | labels/tags, at most 8 |
| `asks` | [str] | ask ids found in the title (empty without `ask_pattern`) |
| `host` | str | the machine it was recorded on (reflections, Drover), `""` otherwise |
| `can` | [str] | write actions the provider supports for it: `"done"` |

## Providers

| Provider | Reads | `done` writes | Credentials |
|---|---|---|---|
| `badland` | `GET {base_url}/api/todos/lists`, then `GET /api/todos/lists/{id}/items` for each non-archived list with open items; `isCompleted: false` only. `priority` none/low/medium/high maps 1:1; `dueDate` → `due`; url `{web_url}/todos/{listId}` | `POST /api/todos/items/{id}/toggle` | `BADLAND_API_KEY` (Bearer) |
| `drover` | the local daemon's `events.state` op: features in state `not-started` or `in-progress` (`state` → `open` / `in-progress`); `group` = repo | `features.upsert` with `state: "shipped"` | the daemon's main token file |
| `reflections` | the daemon's `reflections.list` op, `{all: true, status: "open", kind: "action,question", limit}` | the daemon's `reflections.mark` op, `{op: "done", ids: [id]}` | the daemon's main token file |
| `github` | `GET /search/issues?q=is:open is:issue <query>`; default query `user:<owner> label:<label>` for each configured label | `PATCH /repos/{o}/{r}/issues/{n}` `{state: "closed"}` | `GH_TOKEN`, `GITHUB_TOKEN`, else `gh auth token` |
| `file` | todo.txt lines or Markdown `- [ ]` checkboxes in configured paths | none (edit the file) | none |

Every write runs only from `porchlight -i`, only with `[interactive] allow_writes =
true`, and only after a y/N confirmation (see `porchlight/actions.py`). The
handlers (`todos.done`) are registered in `porchlight/sources/todos.py`, next to
the provider clients; the panel only attaches `ui.Action("todos.done", ...)`
to items whose `can` lists `"done"`.

Every HTTP call goes through `core.http` (its `User-Agent` matters: a
Cloudflare-fronted API answers 403 to Python's default one).

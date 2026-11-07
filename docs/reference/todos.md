# Todos

The `todos` source folds open items from every system that tracks your work
into one normalized list: a Badland-compatible todos API, [Drover](drover.md)
features, Drover reflections, GitHub issues and local `todo.txt`/Markdown
files. The TODOS panel and `porchlight -i` read it. Off by default (opt-in):
it runs once `[sources.todos]` exists. Record shape:
[`docs/contracts/todo-item-v1.md`](../contracts/todo-item-v1.md) — the same
contract a fleet-motd port and other dashboards built on this data agree on.

```text
 TODOS ● ────────────────────────────────────────────────────── 9 open
 open     badland 4 · drover 2 · reflections 2 · ○ github (no GitHub token) · file 1
 items    ● V-41 open-source ai-cli: secret-scan, license, CONTRIBUTING
          ● V-43 todos panel: providers, coverage view and write policy
          ● decide: V-39 shared fleet panels vs per-host MOTD
          ● V-50 write the lux-wire v0 contract doc
          ● question: V-42 should interactive porchlight merge with…
          ● record a wake-word sample set for the edge build
 asks     6 · 2 everywhere · missing: badland 3 · drover 3
```

| Row | Shows |
| --- | --- |
| open | the open total per provider; a failed one shows as `○ name (reason)` with its last error, never a response body |
| items | the top `shown` items (default 6): a priority dot, the title, then the group and due date dimmed (overdue in amber, or red when the priority is high or urgent) |
| asks | present only with `ask_pattern` set: how many ask ids were found, how many show up in every system that answered, and which systems are missing the rest — see ["track every ask"](#track-every-ask) below |

The panel's header note is the kept total and, when a run is stale or failed,
how long ago it last succeeded. A provider's own failure never blanks the
others: each is isolated behind its own try/except and I/O timeout, and keeps
its previous items (marked by the age in the header) until it answers again.

## Settings (`[sources.todos]`)

```toml
[sources.todos]
keep = 200                       # items kept in the record (and a fleet bundle)
shown = 6                        # rows the panel lists
ask_pattern = ""                 # e.g. '\bV-\d{2,3}\b': the "track every ask" view
ask_systems = ["badland", "github", "drover", "brain"]   # where every ask should be recorded

[sources.todos.badland]          # a Badland-compatible todos API; off while base_url is ""
base_url = ""                    # e.g. "https://api.example.com"
web_url = ""                     # item links: {web_url}/todos/{listId}
key_env = "BADLAND_API_KEY"      # Bearer key, read from the environment / secrets files
lists = []                       # list titles or ids to read; empty = every non-archived list with open items

[sources.todos.drover]           # open/in-progress features from the local Drover daemon
enabled = true
states = ["not-started", "in-progress"]

[sources.todos.reflections]      # open reflections (action, question) from the same daemon
enabled = true
kinds = ["action", "question"]

[sources.todos.github]           # open issues; off while both labels and query are empty
owner = ""                       # user or org: user:<owner>
labels = []                      # one search per label, e.g. ["voice-gateway"]
query = ""                       # a raw search query instead (is:open is:issue is added for you)
token_env = ["GH_TOKEN", "GITHUB_TOKEN"]   # first set one wins, then `gh auth token`

[sources.todos.file]             # todo.txt lines or Markdown "- [ ]" boxes
paths = []
```

## Providers

Every provider is independent, optional and failure-isolated. None is
required; configure as many as apply.

### Badland

Reads `GET {base_url}/api/todos/lists`, skips archived lists and ones with no
open items (`itemCount == completedCount`), filters by `lists` (titles or
ids) when set, then `GET {base_url}/api/todos/lists/{id}/items` for each one
that's left (`isCompleted: false` only). `priority` (none/low/medium/high)
and `dueDate` map straight across; the item's link is
`{web_url}/todos/{listId}`, or `""` when `web_url` is empty. Credentials:
`Bearer $BADLAND_API_KEY` (or whatever `key_env` names). Writes: `done` calls
`GET .../items/{id}` first and only `POST .../items/{id}/toggle` when it is
not already complete — it never blind-toggles.

### Drover

Reads the local [Drover](drover.md) daemon's `events.state` op (the main
token; this op is not on the read-only widget token's scope) for features in
`states` (default `not-started`, `in-progress`; mapped to `open` /
`in-progress`). `group` is the feature's repo. Writes: `done` calls
`features.upsert {fid, state: "shipped"}`.

### Reflections

Reads the same daemon's `reflections.list {all: true, status: "open", kind:
"<kinds joined with commas>", limit: keep}`. Only the pointer fields are
kept — id, title, kind, priority, host, repo, tags, timestamps — never the
body. Writes: `done` calls `reflections.mark {op: "done", ids: [id]}`.

### GitHub

One search per configured label (`GET /search/issues?q=is:open is:issue
user:<owner> label:"<label>"`), or one raw `query` instead of the label
loop when it's set. The token is the first set variable in `token_env`,
else `gh auth token`; with neither, the provider reports `no GitHub token
(GH_TOKEN / GITHUB_TOKEN / gh)` and keeps its previous items. Writes: `done`
calls `PATCH /repos/{owner}/{repo}/issues/{n} {state: "closed"}`.

### File

`todo.txt`-style lines (skips lines starting `x `; reads a leading `(A)`–`(C)`
priority as high/medium/low) in any configured path whose extension is not
`.md`/`.markdown`; Markdown `- [ ]` / `* [ ]` checkboxes otherwise. `paths`
are shell-expanded (`~`, `$HOME`). `ref` is `path:line`. No write support —
`done` reports "edit the file directly".

## Track every ask

Set `ask_pattern` to a regex (for example `'\bV-\d{2,3}\b'` for ticket ids
like `V-43`) and the source turns into a coverage dashboard: every id the
pattern finds in an item's title, across every provider, plus the
[`knowledge`](knowledge.md) source's recent brain entries (titles only,
never an extra brain query from here), which mark an ask `"brain": "noted"`.

The record's `asks` field is:

```json
{"pattern": "\\bV-\\d{2,3}\\b",
 "ids": {"V-43": {"badland": "open", "drover": "in-progress"}, "V-41": {"github": "closed"}},
 "systems": ["badland", "drover"]}
```

`systems` lists only the providers in `ask_systems` that actually answered
this run. The brain is never one of them: its few newest titles are not a full
read, so they can mark an ask but never make it "missing" from the brain. A gap
("V-07 has no GitHub issue") is only ever claimed for a system that could
truthfully answer, never for one that was down or unconfigured. The panel's
`asks` row is this as one line: the total ids found, how many show up under
every system in `systems` ("everywhere"), and a `missing: <system> <n>`
breakdown for the rest, worst offender first. `porchlight -i` adds one `Item`
per ask with a gap, listing which systems have it and which don't.

An invalid `ask_pattern` is reported in the record (`asks.error`) and shown
in the panel as a plain note — never raised, and every item's own `asks`
list is simply empty while it's broken.

## Write policy (`porchlight -i`)

Every provider's `done` handler is read-only unless `[interactive]
allow_writes = true`, and even then only runs after you confirm the
exact question the item's `Action` carries (`Mark '<title>' done in
<provider>?`) — see [`docs/interactive.md`](../interactive.md) and
`porchlight/actions.py` for the general policy. A handler loads its own
credentials when it runs; nothing credential-shaped ever rides in an
`Action`'s arguments, and every result message is masked the same way an
error is.

## Privacy

- **Masked by shape.** Titles, group names and tags go through
  `secrets.mask_shapes`: control characters are stripped, and anything
  token-shaped or a credential in a URI reads as `<redacted…>`.
- **No bodies.** The reflections provider keeps only the pointer fields a
  reflection has (id, title, kind, priority, …); its body never reaches the
  cache.
- **Short errors only.** A failed provider's `error` is a few words — never a
  response body, a stack trace or a credential.

## Items (`porchlight -i` and `porchlight view`)

One `Item` per kept todo (capped at 50, not just the shown ones): detail
lines for its group, state, priority, due date, created/updated age, tags,
matched ask ids and host; its web link when it has one; a `copy id` action;
and, when the provider supports it, a `mark done` write action with the
confirmation question above. Plus one `Item` per ask id with a coverage gap
(see above), listing which systems have it and which don't.

## Alerts

| Severity | When |
| --- | --- |
| warn | a kept item with priority `high` or `urgent` is overdue by more than a day |

Deliberately not an alert: a provider that is down or unconfigured (shown in
the panel, with its reason), or an ask with a coverage gap (that's the
`asks` row's job).

## Troubleshooting

- **A provider shows `○ name (reason)` and never clears.** Check its
  credential: `BADLAND_API_KEY` (or `key_env`) for Badland, the Drover daemon
  state directory (`DROVER_STATE`, default `~/.local/state/drover`) for
  `drover`/`reflections`, and `GH_TOKEN`/`GITHUB_TOKEN`/`gh auth status` for
  GitHub. `porchlight doctor` shows the last error and how old the data is.
- **GitHub never runs.** It is off while both `labels` and `query` are
  empty — set at least one label (with or without `owner`) or a raw `query`.
- **An item I expect is missing.** Badland skips archived lists and ones
  with no open items; `lists` (titles or ids) narrows further. `keep` (200
  by default) caps the whole record — raise it if your fleet tracks more
  than that many open items at once.
- **`ask_pattern` finds nothing.** The pattern is matched against item
  titles (and recent brain entry titles) as plain Python `re`, case
  sensitive; test it with `porchlight json todos` and check `asks.error` for
  a syntax mistake.

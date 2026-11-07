# Todos

One cached list of the open items from every system you track work in, as a
TODOS panel, a `porchlight todos` command and a list you can act on in
`porchlight -i`. It's built to be the dashboard for one habit: write every
ask down somewhere, then see from one place which systems have it and which
don't.

```
 TODOS ● ─────────────────────────────────────────────────────── 2 overdue
 115 open · gh 12 · drover 18 · refl 35 · badland 50
 ! Renew the TLS certificate on the edge box                 badland -1d
 ● Wake word false-accepts on TV audio (V-07)       gh octo/speaker#41
 · Port the fleet view to the private dashboard        drover dotfiles
   +109 more · porchlight todos
 asks     52 tracked · 31 everywhere · 21 gaps
          V-04 -drover · V-05 -gh · V-09 -drover,gh · +18
 ✖ gh: GitHub search HTTP 401 (token rejected)
```

## Providers

Each provider is on only when its settings say so. It reads with its own
timeout and fails alone: its error becomes a row, and the others still
render. A provider that fails keeps its last items (marked `kept`), so one
outage doesn't empty the list.

| Provider | Turn it on with | Reads |
|---|---|---|
| `files` | `files = ["~/todo.txt"]` | todo.txt-style lines: `(A)` priorities, `due:2026-12-01`, `+project @context`; `x ` lines are done |
| `github` | `github_query = "is:open is:issue label:voice-gateway user:octocat"` | the issue search, with the first set of `github_token_env` (default `GITHUB_TOKEN`, `GH_TOKEN`), else `gh`'s own login |
| `drover` | `drover = true` | [Drover](https://github.com/coletebou/drover) features not shipped or abandoned (`drover_states`) |
| `reflections` | `reflections = true` | open Drover reflections of `reflections_kinds` (default actions and questions) across repos |
| `badland` | `badland = true` | open [Badland](https://badland.ai) todos, `GET /api/mcp-context/tasks` with `badland_key_env` (default `BADLAND_BRAIN_KEY`, a read-scoped key) |
| `command` | `command = "my-tracker export --json"` | any command printing a JSON list of `{id, title, url?, status?, priority?, due?, repo?, labels?}` |

The source is opt-in and global. On a fleet, the hub collects it and every
peer shows the hub's list ([fleet-consistency.md](fleet-consistency.md)),
so the credentials stay on one machine.

## Ask tracking

```toml
[sources.todos]
ask_pattern = "V-\\d{2,3}"                 # the ids your asks carry
ask_systems = ["badland", "drover", "github"]  # where every ask should be recorded
```

The source looks for ask ids in each item's title and labels, and in the
feature notes, reflection body or todo description. It searches those bodies
for ids and then drops them, so they're never stored. It then groups items by
ask and reports which systems each ask appears in and which it's missing from.
The panel's `asks` row counts asks found everywhere and asks with gaps.
`porchlight todos asks` prints the full table:

```
ask      badland   drover    gh
V-01     -         ✔         -         1 open
V-04     ✔         -         ✔         2 open
```

## Commands

```
porchlight todos                 every open todo (cached), grouped by system
porchlight todos asks            ask coverage: which systems each ask id is in
porchlight todos done <key>      mark one done in its own system   [--yes] [--via-hub]
porchlight todos --json          the items as JSON
```

Listing reads the cache and never touches the network. A key can be
shortened to any unique prefix of the key or of the item's id.

## Acting on an item

The collector only ever reads. Marking an item done is off until you set
`actions = true`, and it always asks first (`y/N`, or `--yes`). It writes
through the item's own system:

| Provider | Done means |
|---|---|
| `files` | the line gets `x <today> ` in place (an atomic rewrite that keeps the file's mode); a line that changed meanwhile is left alone |
| `github` | the issue is closed as completed (token, else `gh issue close`) |
| `drover` | the feature is marked shipped |
| `reflections` | the reflection is marked done, with a note that porchlight did it |
| `badland` | the todo is completed, with `badland_write_key_env` (default `BADLAND_API_KEY`); it re-checks that the item is still open first, because Badland's endpoint toggles |
| `command` | `done_command` runs with `{id}` shell-quoted |

After a done, the item is dropped from the local record at once, and a
background refresh of the source starts. On a peer that shows the hub's list,
`--via-hub` (and `d` in `porchlight -i`, automatically) runs the same command
on the hub over ssh, where the credentials are.

## Keeping another identity's items out

Drover can show every repo you have, work and personal alike. To keep one
side off a dashboard:

```toml
[sources.todos]
exclude_repos = ["corp-*", "client-repo"]   # names or globs
exclude_paths = ["/identities/work/"]       # an item's file, checkout or cwd containing this
```

The paths are matched at collect time and never stored. `porchlight -i`
search applies the same two settings to its hits.

## Every setting

```toml
[sources.todos]
enabled = true
files = []
github_query = ""
github_api = "https://api.github.com"
github_token_env = ["GITHUB_TOKEN", "GH_TOKEN"]
drover = false
drover_states = ["not-started", "in-progress"]
reflections = false
reflections_kinds = ["action", "question"]
badland = false
badland_url = "https://api.badland.ai"
badland_key_env = "BADLAND_BRAIN_KEY"
badland_write_key_env = "BADLAND_API_KEY"
command = ""
done_command = ""
exclude_repos = []
exclude_paths = []
limit = 60            # items kept per provider
ask_pattern = ""
ask_systems = []
actions = false
state_dir = ""        # Drover's state dir (default: the drover source's, else ~/.local/state/drover)
overdue_alert = true  # warn when any item is past due
shown = 6             # rows the panel shows
```

## Security

Every title is cleaned (control characters out) and masked by pattern before
it's stored: token shapes and URI credentials. Records never hold a
credential. Tokens travel only in request headers to their own service. The
Drover token is the daemon's own file, read when needed and never copied.

# Knowledge, Drover and your wikis

porchlight ties into [Drover](https://github.com/coletebou/drover), the
control plane that runs your coding agents, in three places:

| Where | What |
|---|---|
| DROVER panel (`drover` source) | sessions waiting on you and what they ask, what finished or crashed, who works where ([configuration.md](configuration.md)) |
| TODOS panel (`todos` source) | Drover features and open reflections next to your other trackers ([todos.md](todos.md)) |
| KNOWLEDGE panel (`knowledge` source) | the shared brain, the search index, reflections per repo, note folders and links (below) |

`porchlight -i` adds the actions: `a` jumps to a session's tmux pane, `D`
opens Drover's own TUI, `s` searches the brain and reflections, `o` opens a
link ([interactive.md](interactive.md)).

## KNOWLEDGE

```
 KNOWLEDGE ──────────────────────────────────────────────────────────────────
 brain    2130 entries · fresh
          Ship the voice gateway behind the lux-wire contract               2h
          Keep work data out of personal stores                             4d
          +3 decisions · porchlight -i
 index    74.5k chunks · 1170 sources · newest 11s
 refl     425 open dotfiles 107 · speaker 79 · infra 43 · +15
 notes    wiki 1240 · fleet 14m · speaker-setup 2h
 links    wiki · grafana · drover
```

| Row | From | Shows |
|---|---|---|
| `brain` | Drover `brain.status`, `brain.context` | whether the shared memory answers, its entries, a stale index, the latest decisions recorded in it |
| `index` | Drover `search.status` | the local search index over transcripts, memories, reflections and docs: chunks, sources, the newest chunk's age |
| `refl` | Drover `reflections.repos` | open reflections per repo |
| `notes` | your folders | markdown notes per folder (an Obsidian vault, a docs tree or a wiki checkout) and the ones edited last |
| `links` | your config | named links to your wikis and dashboards, plus Drover's own UI |

```toml
[sources.knowledge]
enabled = true
notes = ["~/notes", "~/code/wiki"]
links = [{ name = "wiki", url = "https://wiki.example.com" },
         { name = "grafana", url = "https://grafana.example.com/d/fleet" }]
exclude_repos = ["corp-*"]               # names or globs
exclude_paths = ["/identities/work/"]    # drop repos whose open reflections all live there
decisions = 5
index_stale_hours = 24
```

Each part fails alone: a daemon that doesn't serve `search.status` shows one
error row, and the brain and links still render. Alerts: a brain that's
configured but unreachable (warn), an empty brain index (warn), a stale brain
index (info), and a search index whose newest chunk is older than
`index_stale_hours` (info).

## What is stored

Only counts, titles, dates, repo names and link URLs. No note body, decision
body or reflection body, and no token. The daemon's token travels only in
the Authorization header to the daemon's own URL. The Drover link is the
daemon's base URL, never the URL with its token. Titles are masked by
pattern (token shapes, URI credentials) before they're stored. A notes folder
is walked with a cap of 20,000 files and skips dot-directories
(`.obsidian`, `.git`) and `node_modules`.

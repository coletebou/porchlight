# Should every machine's dashboard show the same thing?

Short answer: **no, but half of it should.** Every screen has two parts:

1. **This machine** first: its vitals, services, units, ports, Docker, repos,
   agents. These differ by machine on purpose. A web server's screen shows nginx
   and its certificates. A laptop's shows its tmux sessions and dirty repos.
   Logging in to a box means asking how *that box* is doing.
2. **The fleet** after it: the same panels in the same order with the same
   numbers on every machine of one identity. That covers the fleet table,
   todos, Drover, AI plan limits, money, SaaS health and PRs. They describe your
   accounts and your work, not the box, so they should read the same on
   every screen.

Fully identical screens hide the local trouble you logged in to fix. Fully
per-host screens make the fleet drift: one box shows AI limits and another
doesn't, the todo list is two hours old here and fresh there, and you can't
tell a real difference from a config one. The split keeps both properties:
local first, fleet the same everywhere.

## How it works

```
 hub  [fleet] peers = [...], shared_panels = ["fleet", "todos", "drover", "ai", "money"]
  │   collects every global source once, then pushes one bundle per peer over ssh:
  │   { from, label, identity, ts, sources: {...}, fleet: {hosts}, layout: {shared: [...]} }
  ▼
 peer  renders: its own panels (minus the shared ones) → the hub's shared panels, in the hub's order
       footer: "fleet view hub 3m"  (amber once older than fleet.stale_after)
```

- **The hub decides.** `shared_panels` is set once, on the hub. It travels in
  the bundle the hub already pushes every collection. Adding a panel there
  changes every screen at the next push, so peers need no config edits.
- **Same data, not just the same panels.** Shared panels read global sources,
  and peers already render the hub's copy of those from the bundle. Every
  screen shows the hub's numbers, with the hub's credentials, collected once.
  A peer never needs a SaaS key or a GitHub token to show the todo list.
- **Local first.** A shared panel that a peer also lists locally moves to the
  fleet part, in the fleet's order. It doesn't appear twice.
- **Opt out per machine.** `[fleet] adopt_layout = false` on a peer keeps its
  own `shared_panels` (or none). It suits a wall display or a constrained box.
- **Identity boundary.** A peer adopts the layout only from a bundle of its own
  identity. A work machine never takes a personal hub's list, and a personal
  machine never takes a work hub's. Data sources keep their own identity rules
  (`registry.serves`), so this boundary covers the layout too.
- **No bundle yet.** Until the first push arrives, a peer uses its own
  `shared_panels`. With `[fleet] hub = "name"` set, it also shows an info
  alert ("no fleet view from name yet") that names the fix: add the machine to
  the hub's `peers`.

## Staleness

The hub pushes every collection (every 5 minutes with the timer). Three
missed pushes is the default threshold, `fleet.stale_after = 900` seconds:

| Bundle age | What a peer shows |
|---|---|
| under `stale_after` | footer `fleet view hub 3m`, dim |
| over `stale_after` | footer in amber, plus a warning: "fleet view from hub is 2h old (the hub stopped pushing)", fix `on hub: porchlight doctor` |
| over a source's `fallback` (`max(3 × ttl, 45 min)`) | a peer with the credentials starts collecting that global source itself (existing behaviour), so a dead hub degrades instead of blanking |

Each shared panel still carries its own stale note ("12m", "✖ 3h"), so a
fresh bundle that carries one failing source still says which one.
`porchlight doctor` prints the bundle's origin and age and marks it
`(stale)`. It also prints the fleet view in effect and where it came from:
this hub's config, the hub's bundle, or this machine's own setting.
`stale_after = 0` turns the warning off.

## Configuration

On the hub:

```toml
[fleet]
peers = ["web-1", "db-1", "gpu-box"]
shared_panels = ["fleet", "todos", "drover", "ai", "money", "prs"]
```

On a peer (all optional):

```toml
[fleet]
hub = "hub"            # names the hub: "no fleet view yet" until the first bundle arrives
stale_after = 900      # seconds; 0 = never warn
adopt_layout = true    # false = keep this machine's own shared_panels

[footer]
fleet_view = true      # the "fleet view hub 3m" footer note
```

## What was considered and rejected

- **One config file for every machine** (`[hosts.*]` overrides). It already
  works and still has a place. But keeping the shared list in sync is then a
  deploy problem: every machine needs the new file. The bundle travels anyway,
  so the layout costs nothing extra to ship with it.
- **Peers fetching the hub's config over HTTP.** That needs a new port and a
  new service. The ssh push already exists, needs no listener and is already
  authenticated.
- **Rendering on the hub and shipping text.** That gives identical screens with
  no local part, the opposite of what you want at a login. It also breaks on
  each terminal's width and color depth.
- **Treating a stale bundle as an error.** The data is still the last good
  copy, and the per-panel notes already say how old each source is. A warning
  plus an amber footer is enough. A crit would cry wolf whenever the hub
  reboots.

## Interactive mode

`porchlight -i` uses the same split. The fleet panels sit in the same place on
every machine, and the top line says where the fleet view comes from and how
old it is. Acting on a shared item (marking a todo done) writes through the
item's own system. On a peer without the credentials,
`porchlight todos done <key> --via-hub` runs the action on the hub over ssh.
See [interactive.md](interactive.md) and [todos.md](todos.md).

# Interactive mode

`porchlight -i` (also `porchlight interactive` or `porchlight tui`) is the
dashboard you can move around in and act from. The panels are listed on the
left, alerts first, each marked with its level and item count. The selected
panel fills the right side at full width, followed by its items: the rows you
can open and act on.

```
 porchlight · atlas · ✖1 · ▲2 · fleet view hub 2m · 15:05
● ALERTS 3        │  TODOS ─────────────────────────────────────────────
  SYSTEM          │  115 open · gh 12 · drover 18 · refl 35 · badland 50
  SERVICES        │  ...
▶ TODOS 115       │
● DROVER 24       │ 115 items · ⏎ to open
  KNOWLEDGE 31    │ ▶ Renew the TLS certificate on the edge box
                  │   Wake word false-accepts on TV audio (V-07)
 d done  o open   ↑↓ move · ⏎ open · ← back · / filter · s search · ? help · q quit
```

The login render never imports this mode, so a login stays as fast as
before. `-i` reads the same cache and runs the same live probes, so a frame
costs what a login render does. It redraws on a key, on a resize, and every
`[interactive] refresh` seconds (default 5). It's stdlib only: termios for
raw keys, ANSI for drawing.

## Keys

| Key | Does |
|---|---|
| `↑` `↓` / `j` `k`, PgUp PgDn | move |
| `→` `⏎` / `l` | into a panel's items, then open one (its detail: every field) |
| `←` `⎋` / `h` | back |
| `tab` / `shift-tab` | next / previous panel |
| `/` | filter the panel's items as you type |
| `d` | mark a todo done, after a `y/N` prompt (needs `[sources.todos] actions = true`; see [todos.md](todos.md)) |
| `o` | open the item's link (a browser where there is one; over ssh it prints the link) |
| `a` | jump to a Drover session's tmux pane (`switch-client` inside tmux, `attach` outside, over `ssh -t` for another host) |
| `s` | search the brain and reflections through Drover; hits honour the todos exclusions |
| `R` | refresh this panel's source now, in the background |
| `r` | redraw from the cache |
| `D` | Drover's own TUI (`drover tui --view board`), then back |
| `A` | ai-cli's TUI (`ai -t`: ask several models side by side), then back |
| `?` | help |
| `q`, ctrl-c | quit |

## What has items

| Panel | Items | Actions |
|---|---|---|
| ALERTS | every alert, with its source and fix | |
| TODOS | every open todo, not only the ones that fit the panel | `d`, `o` |
| DROVER | sessions waiting on you (with what they ask), working, and recently finished or crashed | `a`, `D` |
| KNOWLEDGE | the brain's latest decisions, reflections per repo, note folders, links | `o` on links |

Any panel, including a plugin's, can give the dashboard items. Pass
`items=[{"key", "text", "detail", "url", "actions", "raw"}]` to `x.panel(...)`
([plugins.md](plugins.md)). The plain dashboard ignores them.

## Your own programs

`D` and `A` are entries in one table, and you can add keys or change them:

```toml
[interactive]
refresh = 5
programs = { G = ["lazygit"], H = ["htop"], A = [] }   # A = [] turns the ai-cli key off
```

porchlight leaves the alternate screen, runs the program in the foreground,
then comes back and reloads. The keys the dashboard uses itself
(`q j k l h d o a s r R / ?` and space) can't be taken. A program that isn't
installed gets a message, not a crash.

## On a fleet

The top line says where the fleet panels come from and how old they are
(`fleet view hub 2m`). On a peer that shows the hub's todos and holds none of
the credentials, `d` runs `porchlight todos done <key> --yes --exact` on the
hub over ssh, after the `y/N` here (the prompt says `via hub`); `--exact` keeps
the hub from reading the key as a prefix of another item. See
[fleet-consistency.md](fleet-consistency.md).

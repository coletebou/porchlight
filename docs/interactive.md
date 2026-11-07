# `porchlight -i`: the interactive dashboard

An optional curses TUI over the same panels the login dashboard renders: move
between them, open an item, act on it. The default login path never pays for
this — `tui.py` is only imported when you ask for it, and startup renders from
the cache only (no network).

```sh
porchlight -i                 # the interactive dashboard (also: interactive, tui)
porchlight demo -i            # the same thing on a sample fleet, no setup needed
```

## A text screenshot

```
porchlight -i  ·  atlas  ·  personal  ·  read-only  ·  1 crit · 2 warn
──────────────────────────────────────────────────────────────────────────
 ✖ ATTENTION (2)   │ ✖ search health down (refused) on atlas
   SYSTEM (6)       │ ▲ TLS cert api.example.com expires in 12d
 ▲ DROVER (7)       │
   AGENTS (3)       │
   FLEET (4)        │
──────────────────────────────────────────────────────────────────────────
j/k move · Enter open · o open · y copy · a ask · r refresh · / filter · ? help · q quit
```

The left pane is every panel that has something to show (an `ATTENTION`
pseudo-panel first, one row per current crit/warn alert); the right pane is
the selected panel's rows. `Enter` opens the selected row full-screen: its
text, detail lines, URL, and the keys it offers.

## Key map

| Keys | Does |
|---|---|
| `↑`/`↓`, `j`/`k` | move in the focused pane |
| `PgUp`/`PgDn` | page |
| `g` / `G` | jump to the top / bottom |
| `Tab`, `←`/`→`, `h`/`l` | switch between the panel list and its rows |
| `Enter` | open the selected row's detail view |
| `Esc`, `Backspace` | back (close detail, a modal, or the filter prompt) |
| `o` | open the row's URL (prints it even with no browser, so the terminal can still make it clickable) |
| `y` | copy — the row's text, or its own `copy` action when it has one |
| `a` | ask the AI CLI about the selected row (see below) |
| `r` | reread the cache now, and ask the collector to refresh in the background |
| `/` | filter the current panel's rows (substring, case-insensitive) |
| `?` | this help; any key closes it |
| `q`, `Ctrl-C` | quit |
| *(a row's own key)* | whatever `Action.key` that row declares — shown in its detail view |

Terminals as small as 40×12 work; under about 80 columns the dashboard shows
one pane at a time (whichever has focus) instead of side by side. Resizing
the terminal (`KEY_RESIZE`) reflows the layout immediately; panel *content*
re-wraps on the next reread (`r`, or the periodic refresh below).

## The panels/items/actions model

Every panel the login dashboard would show appears here too, built by the
same code (`render.prepare`, the same `Frame` `porchlight view` uses — see
[contracts/porchlight-view-v1.md](contracts/porchlight-view-v1.md)). A panel
with `ui.Item` rows (built by its panel function) shows those items,
selectable, with their own actions. A panel with none shows its plain lines
as read-only rows — still selectable, still copyable, just without a detail
view full of actions. New panels get this for free; nothing in `tui.py` is
panel-specific.

Two `Action.args` conventions a panel author can rely on, handled by the TUI
itself before the handler ever runs:

- `args["input"]` — a prompt string. The TUI asks for one line of text first
  and passes it to the handler as `args["text"]`. An empty answer cancels.
- `args["pause"]` on a `run` action — after the program exits, wait for
  `Enter` before redrawing (for a command that prints something and quits,
  like a transcript).

## The safety model

- **Read-only by default.** An action whose handler (or whose `Action`) says
  `writes=True` is refused — with the same message `actions.run` would give
  you on the command line — unless `[interactive] allow_writes = true`.
- **Confirmed, always.** Even with `allow_writes` on, a write still asks a
  y/N question (`Action.confirm`, or a generic "run *label*?") before it
  runs; only `y` confirms, anything else cancels and nothing happens.
- **`run` actions are limited to `[interactive] run_allow`.** A `run` action
  (the `a` key, or a row's own `run` action — jumping to a Drover session,
  say) only starts a program whose name is on that list and that `which`
  finds; otherwise the TUI shows the refusal and never leaves the dashboard.
  Foreground runs (`run`, `a`) leave the alternate screen for the program's
  own output and come back when it exits.
- **No credential ever reaches an `Action`'s args.** A handler loads its own
  secrets when it runs ( `secrets.load`); args are never more than ids, text
  and flags.
- **Every string you see has been masked.** Panel lines, item text and detail
  all pass through `secrets.mask_shapes` (token shapes and URI credentials
  replaced) on the way in, the same as `porchlight view`.

## The AI hand-off

`a` on the selected row runs `[interactive] ai_command` (default `["ai"]`)
with a question built from the panel's title, the row's text and its detail
lines, ending "Explain this and tell me what to do first." The dashboard
steps aside (same as any foregrounded `run`) and comes back when it answers.
If the command is missing or not in `run_allow`, you get the same message
`porchlight` would print on the command line — the TUI never drops into a
blank foreground for a program it can't start.

The hand-off runs the other way too: [ai-cli](https://github.com/coletebou/ai-cli)
can read `porchlight view --compact-json` for its own `--motd` context. See
[ai-cli-integration.md](ai-cli-integration.md).

## Jumping to a Drover session

The DROVER panel's items carry their own actions: `s` jumps to the session
(`tmux attach`, `switch-client` inside tmux, or `ssh -t <host> tmux attach`),
`t` shows its transcript, `c` copies its id, and on a waiting session `m`
answers it (a write: `allow_writes` and a y/N). The keys work from the list and
the detail view alike. Jumping is subject to the same `run_allow` and
foregrounding as the `a` key. See [reference/drover.md](reference/drover.md).

A row's own key never shadows one of the TUI's keys above (`j k g G h l o y a r
q / ?`); a plugin that picks one of those anyway gets it only in the detail view.

## `[interactive]` settings

Matches [configuration.md](configuration.md#interactive):

```toml
[interactive]
allow_writes = false  # true: writes are offered, and each still asks y/N first
run_allow = ["tmux", "ssh", "drover", "ai", "porchlight", "gh", "glab"]
refresh = 30           # seconds between cache rereads while the TUI is open
ai_command = ["ai"]    # the AI CLI the "a" key hands the selected item to
```

## Limitations

- No Windows: this is a standard-library `curses` program, and `curses` is
  not available there. Use `porchlight view` instead.
- Needs a real terminal on both stdin and stdout; piped output, `TERM=dumb`,
  or a terminal `curses` cannot initialize all fall back to a short message
  on stderr and exit `2` (try `porchlight view`).
- One panel's rows at a time — there is no way to see two panels side by side
  besides the panel list itself.

## For plugin authors

Nothing plugin-specific is needed: any panel your `@panel` builder returns
(see [plugins.md](plugins.md)) gets browsing, filtering and a detail view for
free. To make its rows *do* something:

```python
from porchlight.ui import Item, Action

Item("todos:badland:6ac18ce5", "upgrade to latest models",
     detail=["list Badland DeV · priority none"],
     url="https://badland.ai/todos/69bc3f2f",
     actions=[Action("todos.done", "mark done", "d", {"id": "badland:6ac18ce5"},
                      writes=True, confirm="Mark 'upgrade to latest models' done?")])
```

and register the handler it names next to your source:

```python
from porchlight.actions import action, Result

@action("todos.done", writes=True)
def todo_done(host, args):
    ...                       # secrets.load(host.cfg), call the API
    return Result(True, "marked done", refresh=("todos",))
```

`writes=True` on either the `Action` or the handler is enough to put it
behind `allow_writes` and the confirm modal — set it on both so the same
handler stays safe if some other caller (a future `porchlight` command) ever
calls it directly. `Result.refresh` names the sources the TUI should ask the
collector to refresh after a successful write, so the panel shows the new
state on the next reread.

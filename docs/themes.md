# Themes and terminals

```sh
porchlight themes                 # preview every theme
PORCHLIGHT_THEME=nord porchlight  # try one without editing the config
```

```toml
[theme]
name = "porchlight"     # the palette
accent = "ember"        # the banner and title gradient (a preset or three colors)
colors = { green = "#2fbf71", dim = "#7d7d7d" }   # override any named color
```

| Theme | Character |
|---|---|
| `porchlight` | warm gold and amber on a dark background (the default) |
| `nord` | arctic frost blues and aurora accents |
| `dracula` | pink, purple and cyan |
| `gruvbox` | retro warm yellows and oranges |
| `catppuccin` | Mocha pastels |
| `tokyonight` | cool night blues and violets |
| `solarized` | Solarized dark |
| `daylight` | for light terminal backgrounds |
| `mono` | greys only; severity shows through glyphs (✖ ▲ ●) |

Accent presets work with any palette: `porchlight`, `teal`, `meadow`, `dusk`,
`ember`, `aurora`, `orchid`, `forest`, or `["#hex", "#hex", "#hex"]`. Give
each machine its own accent so you always know where you are:

```toml
[hosts."prod-*"]
theme.accent = "ember"
[hosts."*-laptop"]
theme.accent = "teal"
```

## Named colors

Panels refer to colors by name, and a theme re-maps the names the way a
terminal color scheme does: `green` `amber` `red` (severities), `sky` (info,
quotes), `violet` (branches, PR numbers), `rose` (behind), `slate` (hints),
`teal`, `sage`, `sand`, `moss`, `gold` `gold_l` `gold_h`, and the neutrals
`fg` `text2` `dim` `faint` `ghost`.

## Color depth

Truecolor when the terminal says so (`COLORTERM`) or is a known RGB terminal
(Ghostty, kitty, WezTerm, Alacritty, foot, iTerm2, Windows Terminal, VS Code,
Warp, …); xterm-256 approximations inside tmux/screen and older terminals; 16
colors as a last resort. In truecolor the banner gets a diagonal gradient and
meters a btop-style heat gradient; otherwise one color per row or per meter.

| Setting | Effect |
|---|---|
| `NO_COLOR=1` | no color at all ([no-color.org](https://no-color.org)) |
| `PORCHLIGHT_COLOR=256` | force a depth: `truecolor`, `256`, `16`, `none` |
| `FORCE_COLOR=1` | color even when stdout is not a terminal |
| `TERM=dumb` | no color |

## ASCII mode

`[theme] ascii = "auto"` (the default) switches to pure ASCII, column for
column, when stdout cannot encode UTF-8, or when you are inside tmux/screen
and the locale is not UTF-8 (an SSH hop that drops `LANG` makes tmux replace
every glyph with `_`). The header then says so and suggests
`export LANG=C.UTF-8`. Force it either way with `ascii = true|false` or
`PORCHLIGHT_ASCII=1|0`.

## Width

The layout adapts to the terminal: three columns from 180, two from ~118, one
below; under 100 columns the compact view shows only `layout.compact` panels
(SSH from a phone), and under 70 the banner gives way to a title line.
`porchlight all` or `--full` shows everything regardless; `--width N` or
`PORCHLIGHT_WIDTH` renders for another size.

## Banners

```toml
[banner]
font = "auto"              # block (4 rows), small (3), big (7), none
text = "{label}"           # {label} {host} {user} {identity}
# font = "figlet:slant"    # any figlet/toilet font, cached
# file = "~/.config/porchlight/banner.txt"   # your own art; ANSI-colored art prints as is
# image = "~/logo.png"     # a logo through chafa, cached
```

The built-in fonts are drawn with half blocks, so every pixel is square, and
need no extra software.

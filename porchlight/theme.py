"""Colors, themes, gradients, meters and ANSI-aware width math.

Color depth: truecolor when the terminal says so (or is a known RGB terminal),
xterm-256 approximations otherwise, 16 colors as a last resort, and none for
NO_COLOR, TERM=dumb, a non-tty stdout, or PORCHLIGHT_COLOR=none. FORCE_COLOR or
PORCHLIGHT_COLOR=<mode> overrides detection.

Panels name colors ("green", "amber", "sky", ...). A theme re-maps those names
the way a terminal color scheme does, so every panel follows the theme for free.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata


def hexrgb(value):
    """'#c9b458' / 'c9b458' / [201, 180, 88] → (201, 180, 88); None when malformed."""
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(isinstance(x, int) for x in value):
        return tuple(max(0, min(255, x)) for x in value)
    s = str(value or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in s):
        return None
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _pal(**kv):
    return {k: hexrgb(v) for k, v in kv.items()}


# The default palette: a warm porch light on a dark terminal.
BASE = _pal(
    gold="#c9b458", gold_l="#e8deb0", gold_h="#d4c478", amber="#f59e0b", amber_l="#fbbf24",
    green="#22c55e", green_l="#4ade80", red="#ef4444", red_l="#f87171",
    sage="#8ab294", sky="#76b0e0", rose="#de8498", violet="#a494e0", sand="#d6b686", slate="#96a2b4",
    moss="#80a06c", teal="#00bec2", teal_l="#7ee8dc",
    fg="#e4e4e4", text2="#b0b0b0", dim="#787878", faint="#484848", ghost="#303030",
)

# Full palettes re-map the named colors (and pick their own accent).
THEMES = {
    "porchlight": {"colors": {}, "accent": ("gold_l", "gold", "amber")},
    "nord": {"accent": ("#8fbcbb", "#88c0d0", "#81a1c1"), "colors": _pal(
        green="#a3be8c", green_l="#b5cf9e", amber="#ebcb8b", amber_l="#f0d9a8", red="#bf616a", red_l="#d08770",
        sky="#88c0d0", violet="#b48ead", rose="#d08770", slate="#81a1c1", teal="#8fbcbb", teal_l="#a7d4d3",
        gold="#ebcb8b", gold_l="#f0d9a8", gold_h="#ebcb8b", sage="#a3be8c", sand="#d8c39a", moss="#8fa876",
        fg="#eceff4", text2="#d8dee9", dim="#7b88a1", faint="#4c566a", ghost="#3b4252")},
    "dracula": {"accent": ("#ff79c6", "#bd93f9", "#8be9fd"), "colors": _pal(
        green="#50fa7b", green_l="#7dfc9b", amber="#ffb86c", amber_l="#ffcb94", red="#ff5555", red_l="#ff7b7b",
        sky="#8be9fd", violet="#bd93f9", rose="#ff79c6", slate="#8a94c4", teal="#8be9fd", teal_l="#b1f1fe",
        gold="#f1fa8c", gold_l="#f6fcb6", gold_h="#f1fa8c", sage="#7bd88f", sand="#e0c68f", moss="#8fbf7a",
        fg="#f8f8f2", text2="#d0d0ca", dim="#6272a4", faint="#44475a", ghost="#343746")},
    "gruvbox": {"accent": ("#fabd2f", "#fe8019", "#fb4934"), "colors": _pal(
        green="#b8bb26", green_l="#c7ca4c", amber="#fabd2f", amber_l="#fbcb5a", red="#fb4934", red_l="#fc6d5d",
        sky="#83a598", violet="#d3869b", rose="#fe8019", slate="#a89984", teal="#8ec07c", teal_l="#a8cf9a",
        gold="#fabd2f", gold_l="#fbd46d", gold_h="#d79921", sage="#8ec07c", sand="#d5c4a1", moss="#98971a",
        fg="#ebdbb2", text2="#d5c4a1", dim="#928374", faint="#504945", ghost="#3c3836")},
    "catppuccin": {"accent": ("#f5c2e7", "#cba6f7", "#89b4fa"), "colors": _pal(
        green="#a6e3a1", green_l="#bdebb9", amber="#fab387", amber_l="#f9e2af", red="#f38ba8", red_l="#f5a3ba",
        sky="#89dceb", violet="#cba6f7", rose="#f5c2e7", slate="#9399b2", teal="#94e2d5", teal_l="#b0eae0",
        gold="#f9e2af", gold_l="#fbecc9", gold_h="#f9e2af", sage="#a6d1a1", sand="#f2cdcd", moss="#9ccf8f",
        fg="#cdd6f4", text2="#bac2de", dim="#7f849c", faint="#45475a", ghost="#313244")},
    "tokyonight": {"accent": ("#7dcfff", "#7aa2f7", "#bb9af7"), "colors": _pal(
        green="#9ece6a", green_l="#b3d98a", amber="#e0af68", amber_l="#e9c48f", red="#f7768e", red_l="#f993a6",
        sky="#7dcfff", violet="#bb9af7", rose="#ff9e64", slate="#737aa2", teal="#73daca", teal_l="#95e3d6",
        gold="#e0af68", gold_l="#ebc68e", gold_h="#e0af68", sage="#9ece6a", sand="#d6b98c", moss="#87a85a",
        fg="#c0caf5", text2="#a9b1d6", dim="#565f89", faint="#414868", ghost="#292e42")},
    "solarized": {"accent": ("#b58900", "#cb4b16", "#d33682"), "colors": _pal(
        green="#859900", green_l="#9aad1f", amber="#b58900", amber_l="#cb9d1c", red="#dc322f", red_l="#e35d5b",
        sky="#268bd2", violet="#6c71c4", rose="#d33682", slate="#93a1a1", teal="#2aa198", teal_l="#4fb5ad",
        gold="#b58900", gold_l="#cfa52e", gold_h="#b58900", sage="#859900", sand="#c9b27c", moss="#7a8c00",
        fg="#93a1a1", text2="#839496", dim="#586e75", faint="#2f4a52", ghost="#073642")},
    "daylight": {"accent": ("#a16207", "#b45309", "#c2410c"), "colors": _pal(  # for light backgrounds
        green="#15803d", green_l="#16a34a", amber="#b45309", amber_l="#d97706", red="#b91c1c", red_l="#dc2626",
        sky="#0369a1", violet="#6d28d9", rose="#be185d", slate="#475569", teal="#0f766e", teal_l="#0d9488",
        gold="#a16207", gold_l="#ca8a04", gold_h="#854d0e", sage="#3f6212", sand="#92400e", moss="#4d7c0f",
        fg="#1f2937", text2="#374151", dim="#6b7280", faint="#b8bec8", ghost="#dde1e7")},
    "mono": {"accent": ("#ffffff", "#c8c8c8", "#8c8c8c"), "colors": _pal(
        green="#c8c8c8", green_l="#dcdcdc", amber="#e8e8e8", amber_l="#f0f0f0", red="#ffffff", red_l="#ffffff",
        sky="#b4b4b4", violet="#c8c8c8", rose="#d2d2d2", slate="#a0a0a0", teal="#c8c8c8", teal_l="#dcdcdc",
        gold="#e0e0e0", gold_l="#f0f0f0", gold_h="#d0d0d0", sage="#bebebe", sand="#c8c8c8", moss="#b4b4b4",
        fg="#e4e4e4", text2="#b0b0b0", dim="#808080", faint="#4a4a4a", ghost="#323232")},
}

# Accent presets usable with any palette (`[theme] accent = "ember"`, or per host).
ACCENTS = {
    "porchlight": ("gold_l", "gold", "amber"), "teal": ("teal_l", "teal", "sky"),
    "meadow": ("sage", "sky", "violet"), "dusk": ("sky", "violet", "rose"),
    "ember": ("amber", "rose", "red"), "aurora": ("teal", "sky", "violet"),
    "orchid": ("violet", "rose", "amber"), "forest": ("moss", "sage", "teal"),
}

PAL = dict(BASE)
ACCENT = ACCENTS["porchlight"]
SEV_COLOR = {"ok": "green", "warn": "amber", "crit": "red", "info": "sky", "unknown": "dim", "stale": "dim"}


def _detect_mode() -> str:
    forced = (os.environ.get("PORCHLIGHT_COLOR") or "").lower()
    if forced in ("truecolor", "256", "16", "none"):
        return forced
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return "none"
    force = os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE")
    if not force and not _isatty():
        return "none"
    ct = os.environ.get("COLORTERM", "").lower()
    term = os.environ.get("TERM", "")
    if ct in ("truecolor", "24bit") or force == "3":
        return "truecolor"
    rgb_terms = ("ghostty", "kitty", "wezterm", "alacritty", "foot", "iterm", "contour", "rio")
    if any(t in term for t in rgb_terms) or os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM", "") in (
            "ghostty", "iTerm.app", "WezTerm", "vscode", "WarpTerminal", "Hyper", "rio"):
        return "truecolor"
    if "256" in term or term.startswith(("tmux", "screen", "xterm")) or force == "2":
        return "256"
    return "16"


def _isatty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


MODE = _detect_mode()
ASCII = os.environ.get("PORCHLIGHT_ASCII", "") not in ("", "0")
RESET = "" if MODE == "none" else "\033[0m"
BOLD = "" if MODE == "none" else "\033[1m"


def set_mode(mode: str) -> None:
    global MODE, RESET, BOLD
    MODE = mode if mode in ("truecolor", "256", "16", "none") else _detect_mode()
    RESET = "" if MODE == "none" else "\033[0m"
    BOLD = "" if MODE == "none" else "\033[1m"


def set_ascii(on: bool) -> None:
    global ASCII
    ASCII = bool(on)


LOCALE_HINT = ""


def utf8_locale() -> bool:
    """Whether the session's locale is UTF-8, as tmux and friends judge it.

    Python coerces a C/POSIX locale to C.UTF-8 at startup (PEP 538) and exports
    LC_CTYPE=C.UTF-8 for itself, so on Linux a lone coerced LC_CTYPE with no LANG
    or LC_ALL means the shell's locale was really C: an SSH hop dropped it."""
    lc_all, lc_ctype, lang = (os.environ.get(v, "") for v in ("LC_ALL", "LC_CTYPE", "LANG"))
    if sys.platform == "darwin" and not (lc_all or lc_ctype or lang):
        return True  # macOS terminals are UTF-8 with or without the variables
    if sys.platform.startswith("linux") and lc_ctype in ("C.UTF-8", "C.utf8") and not lang and not lc_all:
        return False
    value = lc_all or lc_ctype or lang
    return "utf-8" in value.lower() or "utf8" in value.lower()


def setup(cfg) -> list:
    """Apply [theme] and decide ASCII mode. Returns configuration problems."""
    global LOCALE_HINT
    th = cfg.table("theme")
    problems = configure(th.get("name"), th.get("accent") or None, th.get("colors") or {})
    forced = os.environ.get("PORCHLIGHT_ASCII")
    if forced not in (None, ""):
        set_ascii(forced != "0")
        return problems
    mode = th.get("ascii", "auto")
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if mode is True:
        set_ascii(True)
    elif mode is False:
        set_ascii(enc not in ("", "utf8", "utf_8"))
    else:
        in_mux = os.environ.get("TERM", "").startswith(("tmux", "screen")) or bool(os.environ.get("TMUX"))
        good = utf8_locale()
        set_ascii(enc not in ("", "utf8", "utf_8") or (in_mux and not good))
        if not good and not ASCII and sys.platform.startswith("linux"):
            LOCALE_HINT = "locale is not UTF-8: glyphs may garble in tmux · export LANG=C.UTF-8"
        elif ASCII and not good:
            LOCALE_HINT = "ASCII mode: the locale is not UTF-8 · export LANG=C.UTF-8"
    return problems


def resolve_stops(spec):
    """An accent spec (preset name, list of names/hex) → tuple of RGB stops."""
    if isinstance(spec, str):
        spec = ACCENTS.get(spec) or THEMES.get(spec, {}).get("accent") or ACCENTS["porchlight"]
    out = []
    for s in spec or ():
        rgb = PAL.get(s) if isinstance(s, str) and s in PAL else hexrgb(s)
        if rgb:
            out.append(rgb)
    return tuple(out) or tuple(PAL[s] for s in ACCENTS["porchlight"])


def configure(name=None, accent=None, colors=None) -> list:
    """Apply a theme: a palette (by name) plus color overrides plus an accent.
    Returns human-readable problems (unknown theme, malformed colors)."""
    global PAL, ACCENT
    problems = []
    PAL = dict(BASE)
    th = THEMES.get(name or "porchlight")
    if th is None:
        problems.append("unknown theme %r (known: %s)" % (name, ", ".join(sorted(THEMES))))
        th = THEMES["porchlight"]
    PAL.update(th["colors"])
    for k, v in (colors or {}).items():
        rgb = hexrgb(v)
        if rgb is None:
            problems.append("theme color %s=%r is not #rrggbb" % (k, v))
        else:
            PAL[k] = rgb
    ACCENT = resolve_stops(accent if accent else th["accent"])
    return problems


ACCENT = resolve_stops(ACCENT)


# ── Color depth ─────────────────────────────────────────────────────────────
def _cube(v: int) -> int:
    """One channel's level (0-5) in the xterm-256 6×6×6 color cube."""
    if v < 48:
        return 0
    if v < 115:
        return 1
    return (v - 35) // 40


def _rgb256(r: int, g: int, b: int) -> int:
    if abs(r - g) < 10 and abs(g - b) < 10:  # near-grey: the 24-step grayscale ramp is finer
        if r < 8:
            return 16
        if r > 238:
            return 231
        return 232 + round((r - 8) / 247 * 23)
    return 16 + 36 * _cube(r) + 6 * _cube(g) + _cube(b)


def _rgb16(r: int, g: int, b: int) -> int:
    bright = max(r, g, b) > 200
    code = (1 if r > 127 else 0) | (2 if g > 127 else 0) | (4 if b > 127 else 0)
    if code == 0 and max(r, g, b) > 90:
        return 90  # bright black = grey
    return (90 if bright else 30) + code


def rgb(color):
    if isinstance(color, str):
        return PAL.get(color) or PAL["fg"]
    return color


def fg(color) -> str:
    if MODE == "none":
        return ""
    r, g, b = rgb(color)
    if MODE == "truecolor":
        return "\033[38;2;%d;%d;%dm" % (r, g, b)
    if MODE == "256":
        return "\033[38;5;%dm" % _rgb256(r, g, b)
    return "\033[%dm" % _rgb16(r, g, b)


def c(text, color, bold=False) -> str:
    if MODE == "none" or text == "":
        return str(text)
    return (BOLD if bold else "") + fg(color) + str(text) + RESET


def sev(text, level, bold=False) -> str:
    return c(text, SEV_COLOR.get(level, "fg"), bold)


def dim(text) -> str:
    return c(text, "dim")


def lerp(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(stops, t: float):
    """Color at position t∈[0,1] along palette names or RGB tuples."""
    pts = [rgb(s) for s in stops] or [PAL["fg"]]
    if len(pts) == 1:
        return pts[0]
    t = min(1.0, max(0.0, t))
    seg = t * (len(pts) - 1)
    i = min(int(seg), len(pts) - 2)
    return lerp(pts[i], pts[i + 1], seg - i)


# ── ANSI-aware width ────────────────────────────────────────────────────────
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ZERO = {0x200D, 0xFE0E, 0xFE0F, 0x20E3}


def strip(s: str) -> str:
    return _ANSI.sub("", s)


def _cw(ch: str, nxt: str = "") -> int:
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0 or o in _ZERO or 0x1F3FB <= o <= 0x1F3FF:
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F") or 0x1F300 <= o <= 0x1FAFF:
        return 2
    if nxt == "\ufe0f":  # a text symbol forced to emoji presentation (☀️, ⛅️)
        return 2
    return 1


def width(s: str) -> int:
    t = strip(s)
    return sum(_cw(ch, t[i + 1] if i + 1 < len(t) else "") for i, ch in enumerate(t))


def ellipsis() -> str:
    return "..." if ASCII else "…"


def fit(s: str, w: int, mark=None) -> str:
    """Truncate an ANSI string to display width w, keeping escape codes intact."""
    if w <= 0:
        return ""
    if width(s) <= w:
        return s
    mark = ellipsis() if mark is None else mark
    out, used, i, n = [], 0, 0, len(s)
    budget = w - width(mark)
    while i < n:
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        ch = s[i]
        cw = _cw(ch, s[i + 1] if i + 1 < n else "")
        if used + cw > budget:
            break
        out.append(ch)
        used += cw
        i += 1
    return "".join(out) + RESET + dim(mark)


def pad(s: str, w: int, align: str = "<") -> str:
    s = fit(s, w)
    gap = w - width(s)
    if gap <= 0:
        return s
    if align == ">":
        return " " * gap + s
    if align == "^":
        return " " * (gap // 2) + s + " " * (gap - gap // 2)
    return s + " " * gap


# ── Glyphs ──────────────────────────────────────────────────────────────────
_GLYPHS = {
    "dot": "●", "hollow": "○", "stale": "◌", "crit": "✖", "warn": "▲", "info": "·", "ok": "✔",
    "work": "▶", "wait": "⏸", "up": "↑", "down": "↓", "reset": "↻", "via": "⇄", "bullet": "◆",
    "rule": "─", "bar": "━", "bar_half": "╸", "bar_empty": "─", "bar_edge": "▌", "sep": "·",
    "quote_l": "❝", "quote_r": "❞", "approx": "≈",
}
_ASCII_GLYPHS = {
    "dot": "*", "hollow": "o", "stale": ".", "crit": "x", "warn": "!", "info": "-", "ok": "+",
    "work": ">", "wait": "=", "up": "^", "down": "v", "reset": "@", "via": "~", "bullet": "*",
    "rule": "-", "bar": "#", "bar_half": "+", "bar_empty": ".", "bar_edge": "|", "sep": "|",
    "quote_l": '"', "quote_r": '"', "approx": "~",
}


# One-for-one ASCII stand-ins keep every column where it was.
_ASCII_MAP = {"·": ".", "•": "*", "—": "-", "–": "-", "→": ">", "←": "<", "↑": "^", "↓": "v", "↻": "@", "⇄": "~",
              "…": ".", "°": "o", "✔": "+", "✖": "x", "▲": "!", "●": "*", "○": "o", "◌": ".", "◆": "*", "▌": "|",
              "━": "#", "╸": "+", "─": "-", "❝": '"', "❞": '"', "▶": ">", "⏸": "=", "×": "x", "≠": "!",
              "“": '"', "”": '"', "‘": "'", "’": "'", "█": "#", "▀": "^", "▄": "_", "µ": "u", "≈": "~",
              "▁": "_", "▂": ".", "▃": ":", "▅": "=", "▆": "+", "▇": "*"}


def asciify(text: str) -> str:
    """Make output pure ASCII without moving a single column: mapped glyphs
    become one character, anything else becomes '?' per display cell."""
    out = []
    for i, ch in enumerate(text):
        if ord(ch) < 128:
            out.append(ch)
        elif ch in _ASCII_MAP:
            out.append(_ASCII_MAP[ch])
        else:
            out.append("?" * _cw(ch, text[i + 1] if i + 1 < len(text) else ""))
    return "".join(out)


def g(name: str) -> str:
    return (_ASCII_GLYPHS if ASCII else _GLYPHS).get(name, "?")


def dot(level: str) -> str:
    key = {"unknown": "hollow", "off": "hollow", "stale": "stale"}.get(level, "dot")
    return sev(g(key), level)


ORDER = {"crit": 4, "warn": 3, "unknown": 2, "stale": 2, "info": 1, "ok": 0, None: -1}


def worst(*levels) -> str:
    best = None
    for lv in levels:
        if ORDER.get(lv, -1) > ORDER.get(best, -1):
            best = lv
    return best or "ok"


def level_for(value: float, warn: float, crit: float) -> str:
    if value >= crit:
        return "crit"
    if value >= warn:
        return "warn"
    return "ok"


def bar(p: float, w: int = 10, warn: float = 70, crit: float = 90) -> str:
    """A heavy-line meter exactly w cells wide. Truecolor: a btop-style heat
    gradient along the bar; otherwise one color for the bar's severity."""
    p = max(0.0, min(100.0, float(p or 0)))
    cells = p / 100.0 * w
    full = int(cells)
    half = cells - full >= 0.5
    out = []
    for i in range(w):
        if i < full or (i == full and half):
            ch = g("bar") if i < full else g("bar_half")
            if MODE == "truecolor":
                t = (i + 0.5) / w * 100.0
                out.append(fg(gradient(("green", "green", "amber", "red"), min(1.0, t / max(crit, 1.0)))) + ch)
            else:
                out.append(fg(SEV_COLOR[level_for(p, warn, crit)]) + ch)
        else:
            out.append(fg("ghost") + g("bar_empty"))
    return "".join(out) + RESET


def rule(w: int, color: str = "faint", ch=None) -> str:
    return c((ch or g("rule")) * max(0, w), color)


def sep() -> str:
    return dim(" %s " % g("sep"))


_SPARK = "▁▂▃▄▅▆▇█"
_SPARK_ASCII = "_.:-=+*#"


def spark(values) -> str:
    """One cell per value, scaled to the largest. Zero is the floor; anything above it
    gets at least the second step, so a small day never reads as nothing."""
    steps = _SPARK_ASCII if ASCII else _SPARK
    vals = [max(0.0, float(v or 0)) for v in values]
    top = max(vals, default=0.0)
    if top <= 0:
        return steps[0] * len(vals)
    return "".join(steps[0] if v <= 0 else steps[1 + min(6, int(v / top * 6.999))] for v in vals)

"""Layout: gradient banner beside the header facts, the attention strip, a
masonry grid of panels, and the footer.

Width-adaptive: three columns from ~180 columns, two from ~118, otherwise one.
Below layout.compact_width (100) the dashboard shows only the compact panels;
below ~70 the banner gives way to a title line (phone SSH).
"""
from __future__ import annotations

import os
import shutil
import time

from . import alerts, core, font, theme as T
from .ui import Panel, RCtx


def term_width() -> int:
    env = os.environ.get("PORCHLIGHT_WIDTH") or os.environ.get("MOTD_WIDTH")
    if env and env.isdigit():
        return max(30, int(env))
    return max(40, min(240, shutil.get_terminal_size((120, 40)).columns))


def layout(width: int, cfg) -> tuple:
    gap = int(cfg.get("layout.gap", 3))
    minc = int(cfg.get("layout.min_column", 58))
    cols = max(1, min(int(cfg.get("layout.columns", 3)), (width + gap) // (minc + gap)))
    if cols == 3 and width < int(cfg.get("layout.three_column_width", 180)):
        cols = 2
    cw = (width - (cols - 1) * gap) // cols
    return cols, cw


def grid(panels, width, accent, cfg) -> list:
    n, cw = layout(width, cfg)
    gap = int(cfg.get("layout.gap", 3))
    cols, heights = [[] for _ in range(n)], [0] * n
    for p in panels:
        block = p.render(cw, accent) + [""]
        i = heights.index(min(heights))  # masonry: the shortest column takes the next panel
        cols[i] += block
        heights[i] += len(block)
    out = []
    for r in range(max(heights) if heights else 0):
        cells = [T.pad(col[r] if r < len(col) else "", cw) for col in cols]
        out.append((" " * gap).join(cells).rstrip())
    while out and not out[-1].strip():
        out.pop()
    return out


# ── Banner ──────────────────────────────────────────────────────────────────
def banner_text(host) -> str:
    import getpass
    tpl = str(host.cfg.get("banner.text", "{label}") or "")
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = ""
    return tpl.replace("{label}", host.label).replace("{host}", host.name).replace(
        "{user}", user).replace("{identity}", host.identity).strip()


def _chafa(path: str, cols: int, rows: int) -> list:
    """A logo image as truecolor half blocks, cached per (path, mtime, size)."""
    import hashlib
    exe = core.which("chafa")
    if not exe or not os.path.exists(path):
        return []
    key = hashlib.sha1(("%s\0%s\0%dx%d" % (path, core.mtime(path), cols, rows)).encode()).hexdigest()[:16]
    cached = os.path.join(core.STATE_DIR, "banner-img-%s.ans" % key)
    txt = core.read_text(cached)
    if not txt:
        rc, out, _ = core.run([exe, "--format", "symbols", "--symbols", "block+border+space-wide-inverted",
                               "--colors", "full", "--size", "%dx%d" % (cols, rows), path], 6)
        if rc != 0:
            return []
        import re
        txt = re.sub(r"\x1b\[\?[0-9;]*[hl]", "", out)  # cursor show/hide escapes would leak into the terminal
        try:
            core.ensure_dirs()
            with open(cached, "w", encoding="utf-8") as f:
                f.write(txt)
        except OSError:
            pass
    return [ln.rstrip("\n") for ln in txt.splitlines()]


def banner_lines(host, max_w: int):
    """(lines, colored) for the banner, or ([], False) when none fits max_w."""
    b = host.cfg.table("banner")
    if b.get("image"):
        rows = int(b.get("image_rows", 8))
        cols = min(max_w, int(b.get("image_cols", rows * 4)))
        art = _chafa(core.expand(b["image"]), cols, rows) if cols >= 8 else []
        if art:
            return art, True
    if b.get("file"):
        lines = core.read_text(core.expand(b["file"])).splitlines()
        lines = _trim(lines)
        if lines and max(T.width(ln) for ln in lines) <= max_w:
            return lines, any("\x1b[" in ln for ln in lines)
        if lines:
            return [], False
    text = banner_text(host)
    spec = str(b.get("font", "auto") or "auto")
    if not text or spec == "none":
        return [], False
    if spec.startswith("figlet"):
        lines = _trim(font.figlet(text, spec.partition(":")[2], core.STATE_DIR))
        return (lines, False) if lines and max(T.width(ln) for ln in lines) <= max_w else ([], False)
    choices = [spec] if spec in font.FONTS else ["block", "small"]
    for f in choices:
        if font.width_of(text, f) <= max_w:
            return font.render(text, f, ascii=T.ASCII), False
    return [], False


def _trim(lines) -> list:
    lines = [ln.rstrip() for ln in lines]
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def paint(lines, stops) -> list:
    """A diagonal gradient in truecolor, one color per row otherwise."""
    if not lines:
        return []
    rows, cols = len(lines), max(T.width(ln) for ln in lines)
    out = []
    for r, line in enumerate(lines):
        if T.MODE != "truecolor":
            out.append(T.c(line, T.gradient(stops, r / max(1, rows - 1))))
            continue
        buf, last = [], None
        for ci, ch in enumerate(line):
            if ch == " ":
                buf.append(ch)
                continue
            rgb = T.gradient(stops, 0.7 * ci / max(1, cols - 1) + 0.3 * r / max(1, rows - 1))
            if rgb != last:
                buf.append(T.fg(rgb))
                last = rgb
            buf.append(ch)
        out.append("".join(buf) + T.RESET)
    return out


# ── Header facts ────────────────────────────────────────────────────────────
def _zone_time(t, zone):
    import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.fromtimestamp(t, ZoneInfo(zone)).strftime("%H:%M %Z")
    except Exception:  # noqa: BLE001 — no tzdata, unknown zone
        return ""


def clock(host) -> str:
    t = time.time()
    g = host.cfg.table("general")
    primary = g.get("clock", "auto")
    if primary == "auto":
        primary = "utc" if host.server else "local"
    second = g.get("clock_secondary", "auto")
    local_zone = time.strftime("%Z", time.localtime(t))
    if primary == "utc":
        head = time.strftime("%a %b %d · %H:%M UTC", time.gmtime(t))
        if second == "auto":
            extra = "" if local_zone in ("UTC", "GMT") else time.strftime("%H:%M %Z", time.localtime(t))
        else:
            extra = _zone_time(t, second) if second else ""
    else:
        head = time.strftime("%a %b %d · %H:%M %Z", time.localtime(t))
        if second == "auto":
            extra = "" if local_zone in ("UTC", "GMT") else time.strftime("%H:%M UTC", time.gmtime(t))
        else:
            extra = _zone_time(t, second) if second else ""
    return head + (T.dim("  ·  " + extra) if extra else "")


def weather_fact(cache):
    w = cache.data("weather")
    if not w or w.get("temp") is None:
        return ""
    unit = w.get("unit") or "°F"
    icon = "" if T.ASCII else (w.get("icon") or "")
    bits = [("%s %s%s %s" % (icon, w["temp"], unit, w.get("desc", ""))).strip()]
    if w.get("hi") is not None and w.get("lo") is not None:
        bits.append(T.dim("H ") + "%s" % w["hi"] + T.dim(" L ") + "%s" % w["lo"])
    if w.get("rain"):
        bits.append("%s%% rain" % w["rain"])
    if w.get("sunset"):
        bits.append(T.dim("sunset ") + w["sunset"])
    age = cache.age("weather")
    if age > 7200:  # say so instead of passing off a stale reading as now
        bits.append(T.dim("(%s ago)" % core.human_age(age)))
    return T.dim(" · ").join(bits)


def side_facts(host, cache, lv, al, last_seen) -> list:
    v = lv["vitals"]
    hc = host.cfg.table("header")
    mid = T.gradient(T.ACCENT, 0.5)
    who = host.label
    if hc.get("user"):
        import getpass
        try:
            who = getpass.getuser() + "@" + who
        except (KeyError, OSError):
            pass
    facts = [T.c(T.g("bullet") + " ", mid) + T.c(who, mid, bold=True) + T.dim("  ·  " + str(v.get("os", "")))]
    facts.append(clock(host))
    if hc.get("weather", True):
        w = weather_fact(cache)
        if w:
            facts.append(w)
    ipbits = [T.dim("up ") + core.human_age(v.get("uptime", 0))]
    if hc.get("ips", True):
        ts = cache.data("tailscale") or {}
        if ts.get("ip"):
            ipbits.append(T.dim("ts ") + ts["ip"])
        if v.get("ip"):
            ipbits.append(T.dim("ip " if host.server else "lan ") + v["ip"])
    facts.append(T.dim(" · ").join(ipbits))
    nc = sum(1 for a in al if a["sev"] == "crit")
    nw = sum(1 for a in al if a["sev"] == "warn")
    if nc or nw:
        parts = []
        if nc:
            parts.append(T.c("%s %d critical" % (T.g("dot"), nc), "red", bold=True))
        if nw:
            parts.append(T.c("%s %d warning%s" % (T.g("dot"), nw, "" if nw == 1 else "s"), "amber"))
        facts.append("  ".join(parts))
    else:
        facts.append(T.c("%s all systems nominal" % T.g("ok"), "green", bold=True))
    snap_mtime = core.mtime(core.SNAPSHOT_PATH)
    data_age = core.now() - snap_mtime if snap_mtime else None
    tail = []
    if last_seen and hc.get("last_visit", True):
        fresh = sum(1 for a in al if a.get("new") and a["sev"] in ("crit", "warn"))
        tail.append(T.dim("last visit ") + core.human_age(core.now() - last_seen, " ago") +
                    (T.dim(" · ") + T.c("%d new alert%s" % (fresh, "" if fresh == 1 else "s"), "sky", bold=True)
                     if fresh else ""))
    if data_age is None:
        tail.append(T.c("collecting first data" + T.ellipsis(), "sky"))
    elif data_age > 900:
        tail.append(T.c("data %s old · refreshing" % core.human_age(data_age), "amber"))
    if tail:
        facts.append(T.dim(" · ").join(tail))
    if getattr(T, "LOCALE_HINT", ""):
        facts.append(T.c(T.LOCALE_HINT, "amber"))
    return facts


def header(host, cache, lv, al, W, last_seen) -> list:
    facts = side_facts(host, cache, lv, al, last_seen)
    fw = min(max(T.width(f) for f in facts), 46)
    colors = host.cfg.get("banner.colors")
    stops = T.resolve_stops(colors) if colors else T.ACCENT
    art, colored = banner_lines(host, W - fw - 6)
    if art:
        painted = art if colored else paint(art, stops)
        aw = max(T.width(ln) for ln in art)
        top = max(0, (len(painted) - len(facts)) // 2)
        rows = []
        for i, line in enumerate(painted):
            j = i - top
            fact = facts[j] if 0 <= j < len(facts) else ""
            rows.append(" " + T.pad(line, aw) + "   " + T.fit(fact, W - aw - 5))
        # Facts that outrun the art continue below it, in the same column.
        extra = [" " * (aw + 4) + f for f in facts[max(0, len(painted) - top):]]
        return [""] + rows + extra
    art, colored = banner_lines(host, W - 2) if W >= 70 else ([], False)
    if art:
        painted = art if colored else paint(art, stops)
        return [""] + [" " + ln for ln in painted] + [""] + [" " + f for f in facts]
    return [""] + [" " + T.fit(f, W - 2) for f in facts]


# ── Attention strip ─────────────────────────────────────────────────────────
def attention(al, W, limit=10, gap=3) -> list:
    shown = [a for a in al if a["sev"] in ("crit", "warn")]
    if not shown:
        return []
    nc = sum(1 for a in shown if a["sev"] == "crit")
    color = "red" if nc else "amber"
    head = " " + T.c(T.g("bar_edge"), color) + T.c("ATTENTION", color, bold=True) + "  " + T.rule(max(4, W - 14))
    items = []
    for a in shown[:limit]:
        gl, col = alerts.glyph(a["sev"])
        badge = T.c("NEW ", "sky", bold=True) if a.get("new") else ""
        items.append(T.c(gl, col) + " " + badge + (T.c(a["msg"], "fg") if a["sev"] == "crit" else a["msg"]))
    more = len(shown) - limit
    if more > 0:
        items.append(T.dim("%s %d more · porchlight alerts" % (T.ellipsis(), more)))
    if W >= 120 and len(items) > 3:
        cw = (W - 4 - gap) // 2
        half = (len(items) + 1) // 2
        rows = []
        for i in range(half):
            right = items[half + i] if half + i < len(items) else ""
            rows.append("   " + T.pad(items[i], cw) + " " * gap + T.fit(right, cw))
        return [head] + rows
    return [head] + ["   " + T.fit(i, W - 4) for i in items]


# ── Footer ──────────────────────────────────────────────────────────────────
def _data_file(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", name)


def _lines(path: str) -> list:
    return [ln.strip() for ln in core.read_text(path).splitlines() if ln.strip() and not ln.startswith("#")]


def quote(host, cache) -> str:
    import random
    pool = (cache.data("quotes") or {}).get("items") or []
    if not pool:
        f = host.cfg.get("footer.quotes_file")
        pool = [q for q in _lines(core.expand(f) if f else _data_file("quotes.txt")) if 8 < len(q) < 180]
    return random.choice(pool) if pool else ""


def qotd(host) -> str:
    """One question per day, the same on every machine. Days since the epoch
    (not day-of-year) so the rotation does not jump back to the top on Jan 1."""
    import datetime
    f = host.cfg.get("footer.qotd_file")
    lines = _lines(core.expand(f) if f else _data_file("questions.txt"))
    if not lines:
        return ""
    return lines[datetime.date.today().toordinal() % len(lines)]


def footer(host, cache, W, t0) -> list:
    fc = host.cfg.table("footer")
    out = []
    if fc.get("quote", True):
        q = quote(host, cache)
        if q:
            out.append(" " + T.c(T.g("quote_l") + " ", "faint") + T.c(T.fit(q, W - 6), "sky") + T.c(" " + T.g("quote_r"), "faint"))
    if fc.get("qotd", True):
        qd = qotd(host)
        if qd:
            out.append(" " + T.dim("QOTD ") + T.c(T.fit(qd, W - 7), "gold_h"))
    left, right = "", ""
    if fc.get("hints", True):
        left = T.dim("porchlight ") + T.dim(" · ").join(T.c(cmd, "slate") for cmd in tuple(host.hints) + ("-h",))
    if fc.get("timing", True):
        right = T.dim("rendered %dms" % int((time.monotonic() - t0) * 1000))
    if left or right:
        gap = W - 1 - T.width(left) - T.width(right)
        out.append(" " + left + (" " * gap + right if gap > 2 else ""))
    return out


# ── Entry ───────────────────────────────────────────────────────────────────
def visit(host, login: bool, found=()):
    """The previous login's time; tags alerts that appeared since then as new.
    Only a login moves the bookmark; running porchlight by hand just reads it."""
    import re
    path = os.path.join(core.STATE_DIR, "last_visit.json")
    prev = core.read_json(path) or {}

    def key(msg):  # digits folded: 'swap 89%' → 'swap 90%' is the same alert, not a new one
        return re.sub(r"\d+(\.\d+)?", "#", msg.replace(" (stale)", ""))
    seen = set(prev.get("alerts") or [])
    if prev.get("ts"):
        for a in found:
            a["new"] = key(a["msg"]) not in seen
    if login:
        try:
            core.ensure_dirs()
            core.write_json_atomic(path, {"ts": core.now(), "alerts": sorted({key(a["msg"]) for a in found})})
        except OSError:
            pass
    return prev.get("ts")


def compact_mode(host, W, only) -> bool:
    if only or os.environ.get("PORCHLIGHT_FULL"):
        return False
    mode = str(host.cfg.get("layout.mode", "auto"))
    if os.environ.get("PORCHLIGHT_COMPACT") or mode == "compact":
        return True
    if mode == "full":
        return False
    return W < int(host.cfg.get("layout.compact_width", 100))


def build_panels(ctx, keys) -> list:
    from . import panels
    built = []
    for key in keys:
        fn = panels.BUILDERS.get(key)
        if not fn:
            continue
        try:
            p = fn(ctx)
        except Exception as e:  # noqa: BLE001 — one broken panel must not blank the dashboard
            p = Panel(key, key.upper(), [T.c("panel error: %s: %s" % (type(e).__name__, e), "red")], "warn")
        if p and p.lines:
            built.append(p)
    return built


def render(host, cache, width=None, only=None, login=False, compact=None) -> str:
    from . import panels
    t0 = time.monotonic()
    W = width or term_width()
    repos = list(host.repos) if (not only or "repos" in only) else []
    lv = alerts.probe_all(host, repos=repos)
    al = alerts.gather(host, cache, lv)
    last_seen = visit(host, login, al)
    compact = compact_mode(host, W, only) if compact is None else compact
    _, cw = layout(W, host.cfg)
    ctx = RCtx(host, cache, lv, al, cw)
    keys = only or host.panels
    if not only:
        keys = keys + [k for k in panels.custom_keys(host) if k not in keys]
    if compact:
        keys = [k for k in host.compact if k in panels.BUILDERS]
    built = build_panels(ctx, keys)
    out = []
    if not only:
        out += header(host, cache, lv, al, W, last_seen)
        att = attention(al, W, int(host.cfg.get("layout.attention_compact" if compact else "layout.attention", 10)))
        if att:
            out += [""] + att
        out.append("")
    out += grid(built, W, T.ACCENT, host.cfg)
    if not only:
        out.append("")
        if compact:
            more = len([k for k in host.panels if k not in keys and k in panels.BUILDERS])
            out.append(" " + T.dim("compact view · ") + T.c("porchlight all", "slate") +
                       T.dim(" for %s" % ("every panel" if more else "the full layout") + " (or widen the window)"))
        else:
            out += footer(host, cache, W, t0)
    return "\n".join(out)


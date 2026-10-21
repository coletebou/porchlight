"""Paths, cache records, process and HTTP helpers, and number formatting.

Everything here is stdlib-only and cheap to import: the login render imports
this module first and must stay in the tens of milliseconds.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

from . import __version__

HOME = os.path.expanduser("~")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_WSL = IS_LINUX and os.path.isdir("/mnt/c/Windows")
USER_AGENT = "porchlight/%s" % __version__


# ── Paths ───────────────────────────────────────────────────────────────────
# Module-level so hot paths read a plain global. use_cache_dir() rebinds them
# (tests and `porchlight demo` point the cache at a scratch directory).
def _xdg(var: str, fallback: str) -> str:
    v = os.environ.get(var, "")
    return v if os.path.isabs(v) else os.path.join(HOME, fallback)


CONFIG_DIR = os.environ.get("PORCHLIGHT_CONFIG_DIR") or os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "porchlight")
CONFIG_PATH = os.environ.get("PORCHLIGHT_CONFIG") or os.path.join(CONFIG_DIR, "config.toml")
PLUGIN_DIR = os.path.join(CONFIG_DIR, "plugins")
CACHE_DIR = SRC_DIR = HUB_DIR = STATE_DIR = LOCK_PATH = SNAPSHOT_PATH = BUNDLE_PATH = LOG_PATH = ""


def use_cache_dir(path: str) -> None:
    global CACHE_DIR, SRC_DIR, HUB_DIR, STATE_DIR, LOCK_PATH, SNAPSHOT_PATH, BUNDLE_PATH, LOG_PATH
    CACHE_DIR = path
    SRC_DIR = os.path.join(path, "src")
    HUB_DIR = os.path.join(path, "hub")
    STATE_DIR = os.path.join(path, "state")
    LOCK_PATH = os.path.join(path, "collect.lock")
    SNAPSHOT_PATH = os.path.join(path, "snapshot.json")
    BUNDLE_PATH = os.path.join(HUB_DIR, "bundle.json")
    LOG_PATH = os.path.join(path, "collect.log")


use_cache_dir(os.environ.get("PORCHLIGHT_CACHE_DIR") or os.path.join(_xdg("XDG_CACHE_HOME", ".cache"), "porchlight"))


def now() -> float:
    return time.time()


def expand(path: str) -> str:
    """~ and $VARS in a configured path."""
    return os.path.expandvars(os.path.expanduser(str(path or "")))


def ensure_dirs() -> None:
    for d in (CACHE_DIR, SRC_DIR, HUB_DIR, STATE_DIR):
        os.makedirs(d, mode=0o700, exist_ok=True)


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json_atomic(path: str, obj) -> None:
    """Write-then-rename, mode 0600: a reader never sees half a record."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), default=str)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_text(path: str, limit: int = 1 << 20) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def tail(path: str, nbytes: int = 65536) -> str:
    """The last nbytes of a (log) file, decoded leniently."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def dotenv(path: str) -> dict:
    """KEY=VALUE lines of an env file, parsed in memory: no shell, no expansion
    beyond a leading ~ or $HOME. Values never reach a log line or argv."""
    out = {}
    for line in read_text(expand(path), 1 << 20).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        if not k.replace("_", "").isalnum():
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        if v.startswith("~/"):
            v = HOME + v[1:]
        out[k] = v.replace("$HOME", HOME).replace("${HOME}", HOME)
    return out


def utc_epoch(stamp) -> int:
    """'2026-10-16T12:00:00…' (UTC; fraction and zone ignored) → epoch secs; 0 if unparseable."""
    import calendar
    try:
        return calendar.timegm(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def iso_epoch(v):
    """ISO-8601 text (any offset, any fraction) or epoch secs/ms → int epoch secs, None if unparseable."""
    import datetime
    import re
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        f = float(v)
        return int(f / 1000) if f > 1e11 else int(f)
    s = str(v).strip().replace("Z", "+00:00")
    m = re.match(r"^(.*[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", s)
    if m:  # Python 3.9's fromisoformat takes exactly 3 or 6 fraction digits
        s = m.group(1) + "." + (m.group(2) or "")[:6].ljust(6, "0") + m.group(3)
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


# ── Processes ───────────────────────────────────────────────────────────────
def run(argv, timeout: float = 4.0, env=None, input_text=None, cwd=None):
    """Run argv, never raising. Returns (rc, stdout, stderr); rc 124 = timeout, 127 = missing."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                           input=input_text, cwd=cwd, errors="replace",
                           stdin=None if input_text is not None else subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "not found: %s" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %.1fs" % timeout
    except OSError as e:
        return 126, "", str(e)


_WHICH = {}


def which(name: str, extra=()):
    """shutil.which plus the directories launchd and systemd PATHs leave out."""
    key = (name, tuple(extra))
    if key in _WHICH:
        return _WHICH[key]
    import shutil
    found = shutil.which(name)
    if not found:
        for d in (os.path.join(HOME, ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
                  "/bin", "/usr/sbin", "/sbin", "/snap/bin") + tuple(extra):
            p = os.path.join(expand(d), name)
            if os.access(p, os.X_OK):
                found = p
                break
    _WHICH[key] = found
    return found


def parallel(jobs: dict, timeout: float = 3.0) -> dict:
    """Run zero-arg callables concurrently (subprocess and socket waits release
    the GIL). Plain daemon threads: concurrent.futures drags in logging (~4 ms of
    import) and its executor joins stragglers at exit. A raised or unfinished job
    reads as None."""
    import threading
    res = {}

    def runner(key, fn):
        try:
            res[key] = fn()
        except Exception:  # noqa: BLE001 — a failed probe renders as missing
            res[key] = None
    threads = [threading.Thread(target=runner, args=(k, f), daemon=True) for k, f in jobs.items()]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return {k: res.get(k) for k in jobs}


# ── HTTP ────────────────────────────────────────────────────────────────────
class Resp:
    __slots__ = ("status", "body", "ms", "headers")

    def __init__(self, status, body, ms, headers=None):
        self.status, self.body, self.ms, self.headers = status, body, ms, headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _no_redirect_opener():
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None  # surface the 3xx instead of forwarding credentials to another host
    return urllib.request.build_opener(NoRedirect)


def http(url, method="GET", headers=None, data=None, json_body=None, timeout=6.0, basic=None,
         raw=False, redirects=None, verify=True) -> Resp:
    """A small urllib wrapper. Transport errors (DNS, TLS, refused, timeout) raise;
    HTTP errors come back as a Resp carrying the status so callers can branch.

    Redirects are followed only for uncredentialed requests unless `redirects`
    says otherwise, so an Authorization header never leaves the host it was
    written for."""
    import base64
    import ssl
    import urllib.error
    import urllib.parse
    import urllib.request
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("unsupported URL scheme")  # urllib would also open file:// and ftp://
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    elif data is not None:
        body = data if isinstance(data, bytes) else urllib.parse.urlencode(data).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if basic:
        hdrs["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % basic).encode()).decode()
    credentialed = any(k.lower() in ("authorization", "cookie", "x-api-key", "x-auth-key") for k in hdrs)
    follow = (not credentialed) if redirects is None else redirects
    handlers = []
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    if follow:
        opener = urllib.request.build_opener(*handlers)
    else:
        opener = _no_redirect_opener()
        for h in handlers:
            opener.add_handler(h)
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    t0 = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as r:
            payload, status, rh = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            payload = e.read() or b""
        except OSError:
            payload = b""
        status, rh = e.code, dict(e.headers or {})
        e.close()
    ms = int((time.monotonic() - t0) * 1000)
    if raw:
        return Resp(status, payload, ms, rh)
    text = payload.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        parsed = text
    return Resp(status, parsed, ms, rh)


def net_reason(e) -> str:
    """A transport failure in a word or two (URLError wraps its cause in .reason)."""
    import ssl
    import urllib.error
    r = e.reason if isinstance(e, urllib.error.URLError) else e
    text = str(r).lower()
    if isinstance(r, ssl.SSLError) or "certificate" in text:
        return "tls error"
    if isinstance(r, (socket.timeout, TimeoutError)) or "timed out" in text:
        return "timeout"
    if isinstance(r, ConnectionRefusedError):
        return "refused"
    if isinstance(r, socket.gaierror):
        return "no dns"
    if isinstance(r, ConnectionResetError):
        return "reset"
    return "no route" if isinstance(r, OSError) else (type(r).__name__ or "down")


# ── Formatting ──────────────────────────────────────────────────────────────
def human_age(seconds: float, suffix: str = "") -> str:
    s = int(max(0, seconds))
    if s < 60:
        out = "%ds" % s
    elif s < 3600:
        out = "%dm" % (s // 60)
    elif s < 86400:
        h, m = divmod(s // 60, 60)
        out = "%dh%02dm" % (h, m) if h < 10 and m else "%dh" % h
    elif s < 86400 * 14:
        d, h = divmod(s // 3600, 24)
        out = "%dd%dh" % (d, h) if h and d < 3 else "%dd" % d
    else:
        out = "%dw" % (s // (86400 * 7))
    return out + suffix


def human_bytes(n: float, unit: str = "") -> str:
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024 or u == "P":
            if u == "B":
                return "%d%s" % (n, unit or "B")
            return ("%.1f%s" % (n, u)) if n < 10 else ("%.0f%s" % (n, u))
        n /= 1024.0
    return "?"


def human_num(n: float) -> str:
    n = float(n or 0)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            v = n / div
            return ("%.1f%s" % (v, suf)) if v < 10 else ("%.0f%s" % (v, suf))
    return "%d" % n


def money(n, cur: str = "$") -> str:
    if n is None:
        return "?"
    n = float(n)
    if abs(n) >= 1000:
        return "%s%s" % (cur, human_num(n))
    return ("%s%.2f" % (cur, n)) if abs(n) < 100 else ("%s%.0f" % (cur, n))


def pct(used, total) -> float:
    try:
        return 100.0 * float(used) / float(total) if total else 0.0
    except (TypeError, ValueError):
        return 0.0


def short_host() -> str:
    return socket.gethostname().split(".")[0].lower()

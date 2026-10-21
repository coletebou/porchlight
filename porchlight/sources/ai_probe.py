#!/usr/bin/env python3
"""AI subscription usage probe: Claude Pro/Max, Codex (ChatGPT plans), Cursor, Grok Build.

Standalone on purpose: stdlib only and no porchlight imports, so run_remote() can
pipe this very file to `ssh HOST python3 -I -` and read a login that lives on
another machine. The token never leaves the machine that owns it; only numbers
come back. porchlight/sources/ai_limits.py wraps each fetcher as a source.

Strictly read-only:
  * It never refreshes, rotates or writes a credential. Refresh tokens are
    single-use, so refreshing from here would rotate the token out from under
    the vendor CLI and sign it out. An expired access token is reported
    instead; the CLI renews it on its next run.
  * It never prints or returns a token. Results are built from numbers and
    labels, error text is scrubbed, and fetch() masks every secret it read
    wherever one might still appear in the result.
  * Keychain reads run `security` under a timeout, so a locked keychain or an
    access prompt can never hang a run.
  * Credentialed requests never follow redirects: urllib would replay the
    Authorization header to whatever host a 3xx names.

Every fetcher returns
  {"provider", "host", "plan", "windows": [{"label": "5h", "used_pct": 17.0, "resets_at": <epoch>, ...}],
   "credits": {...} | None, "source": "api" | "rollout" | None, "as_of": <epoch>,
   "latency_ms", "error": None | "why", "detail": {...}}
A window marked "extra" is a breakdown (per model, code review, auto/api share),
not one of the plan's own limits. detail["auth"] says why there is no reading:
"none" (not signed in), "expired" (the CLI renews it on its next run),
"relogin" (sign in again) or "api_key" (no plan limits to read).

CLI:  python3 ai_probe.py [--json] [--host SSH_ALIAS] [--claude-dir DIR] [--codex-dir DIR]
                          [--no-keychain] [claude codex cursor grok]
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import sys
import time

IS_MAC = sys.platform == "darwin"
SECURITY = "/usr/bin/security"
USER_AGENT = "porchlight-ai-probe"
SKEW = 30  # seconds: a token this close to expiry counts as expired

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_SERVICE = "Claude Code-credentials"
CODEX_WHAM_URL = "https://chatgpt.com/backend-api/wham/usage"
CURSOR_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
CURSOR_PROFILE_URL = "https://api2.cursor.sh/auth/full_stripe_profile"
CURSOR_SUMMARY_URL = "https://cursor.com/api/usage-summary"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_CLIENT_VERSION = "0.2.101"  # sent when ~/.grok/version.json is unreadable
GROK_CREDIT_KEYS = ("monthlyLimit", "includedUsed", "totalUsed", "onDemandCap", "onDemandUsed", "prepaidBalance",
                    "on_demand_enabled", "isUnifiedBillingUser")


# ── Helpers ─────────────────────────────────────────────────────────────────
def _home() -> str:
    return os.path.expanduser("~")  # per call: tests (and `sudo -H`) move $HOME


def _path(p) -> str:
    return os.path.expandvars(os.path.expanduser(str(p)))


def _tilde(p: str) -> str:
    """A path for messages: the home directory (and with it the user name) folded to ~."""
    home = _home()
    return "~" + p[len(home):] if home not in ("", "/") and (p == home or p.startswith(home + os.sep)) else p


def _ms(t0) -> int:
    return int((time.monotonic() - t0) * 1000)


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _epoch(v):
    """ISO-8601 text (any offset, any fraction) or epoch secs/ms → int epoch secs; None if unparseable."""
    import datetime
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and re.match(r"^\d+(\.\d+)?$", v)):
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


def _fmt(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(t)) if t else "?"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _jwt_claims(token) -> dict:
    """A JWT's payload, unverified: only expiry and plan hints are read from it."""
    try:
        part = str(token).split(".")[1]
        d = json.loads(base64.urlsafe_b64decode((part + "=" * (-len(part) % 4)).encode()))
    except (IndexError, ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _which(name: str):
    """shutil.which plus the directories launchd and systemd PATHs leave out."""
    import shutil
    found = shutil.which(name)
    if found:
        return found
    for d in ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        p = os.path.join(_path(d), name)
        if os.access(p, os.X_OK):
            return p
    return None


def _run(argv, timeout=5.0, input_text=None, text=True):
    """argv → (rc, stdout, stderr), never raising: rc 124 = timeout, 127 = missing."""
    empty = "" if text else b""
    try:
        p = subprocess.run(argv, input=input_text, capture_output=True, timeout=timeout, text=text,
                           stdin=None if input_text is not None else subprocess.DEVNULL,
                           **({"errors": "replace"} if text else {}))
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, empty, "not found: %s" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, empty, "timed out after %.0fs" % timeout
    except OSError as e:
        return 126, empty, str(e)


_TOKENISH = re.compile(r"(?<![A-Za-z0-9])(xai-|sk-ant-|sk_live_|sk-|gh[pousr]_|eyJ)[A-Za-z0-9._\-]{4,}")
_LONG = re.compile(r"[A-Za-z0-9_\-+/=]{40,}")


def scrub(text, secrets=()) -> str:
    """Text with the given secrets (longest first), then anything else token-shaped, masked."""
    text = str(text)
    for s in secrets:
        text = text.replace(s, "<token>")
    text = _TOKENISH.sub(lambda m: m.group(1) + "…", text)
    return _LONG.sub("<redacted>", text)


# ── Transport ───────────────────────────────────────────────────────────────
class _Resp:
    __slots__ = ("status", "body", "ms")

    def __init__(self, status, body, ms):
        self.status, self.body, self.ms = status, body, ms


def _http(url, method="GET", headers=None, data=None, timeout=10.0, **_):
    """The standalone transport (porchlight passes its own core.http in instead).
    HTTP errors come back as a status, transport errors raise, and redirects are
    never followed, so an Authorization header cannot reach another host."""
    import urllib.error
    import urllib.request
    if not str(url).startswith("https://"):
        raise ValueError("refusing a non-https URL")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None  # the 3xx surfaces as an HTTPError instead of being followed
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    t0 = time.monotonic()
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read() or b""
        except OSError:
            raw = b""
        e.close()
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text) if text.strip() else None
    except ValueError:
        body = text
    return _Resp(status, body, _ms(t0))


def _net(e) -> str:
    """A transport failure in a few words (URLError wraps its cause in .reason). Never
    truncated here: Probe.err scrubs first and cuts after, so no token half survives."""
    r = getattr(e, "reason", None) or e
    text = str(r).lower()
    if "certificate" in text or "ssl" in text:
        return "tls error"
    if isinstance(r, (socket.timeout, TimeoutError)) or "timed out" in text:
        return "timeout"
    if isinstance(r, ConnectionRefusedError):
        return "refused"
    if isinstance(r, socket.gaierror) or "nodename" in text or "name or service" in text:
        return "no dns"
    return "%s: %s" % (type(r).__name__, r)


def _result(provider, **kw) -> dict:
    r = {"provider": provider, "host": socket.gethostname().split(".")[0], "plan": None, "windows": [],
         "credits": None, "source": None, "as_of": int(time.time()), "latency_ms": None, "error": None, "detail": {}}
    r.update(kw)
    return r


def _win(label, used_pct, resets_at, **extra) -> dict:
    p = _num(used_pct)
    w = {"label": str(label), "used_pct": None if p is None else round(p, 1), "resets_at": _epoch(resets_at)}
    w.update({k: v for k, v in extra.items() if v is not None})
    return w


def _dur(seconds) -> str:
    """A window length as a label: 18000 → 5h, 604800 → 7d."""
    s = int(_num(seconds) or 0)
    if s <= 0:
        return "?"
    if s % 86400 == 0:
        return "%dd" % (s // 86400)
    if s % 3600 == 0:
        return "%dh" % (s // 3600)
    return "%dm" % (s // 60)


class Probe:
    """One fetch: its options, its transport, and every secret it read, so fetch()
    can mask each of them wherever it might appear in the result.

    opts: http (a core.http-style callable), keychain (False skips the macOS
    Keychain), keychain_timeout, timeout (per HTTP call), claude_dir, codex_dir,
    xai_api_key (True when the caller's environment has XAI_API_KEY)."""

    def __init__(self, opts=None):
        self.opts = dict(opts or {})
        self.http = self.opts.get("http") or _http
        self.timeout = float(self.opts.get("timeout") or 10)
        self.secrets = []

    def keep(self, token):
        """Remember a secret (and each long segment of a JWT) for masking; returns it."""
        if isinstance(token, str) and len(token) >= 8:
            self.secrets.append(token)
            self.secrets += [s for s in token.split(".") if len(s) >= 16 and s != token]
        return token

    def scrub(self, text) -> str:
        return scrub(text, sorted(set(self.secrets), key=len, reverse=True))

    def call(self, url, headers, method="GET", data=None):
        """(status, payload, ms); status None on a transport error, the payload then says why."""
        t0 = time.monotonic()
        try:
            r = self.http(url, method=method, headers=headers, data=data, timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 — DNS, TLS, refused, timeout: one line, no traceback
            return None, _net(e), _ms(t0)
        body = r.body
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8", "replace")
        if isinstance(body, str):  # a proxy's or WAF's HTML page says nothing useful in one line
            body = "html page instead of JSON" if body.lstrip().startswith("<") else self.scrub(body)[:300]
        ms = r.ms if isinstance(getattr(r, "ms", None), int) else _ms(t0)
        return r.status, {} if body is None else body, ms

    def err(self, status, payload) -> str:
        """One secret-free line for a failed call."""
        msg = payload
        if isinstance(payload, dict):
            e = payload.get("error")
            if isinstance(e, dict):
                msg = e.get("message") or e.get("type") or e
            else:
                msg = e or payload.get("detail") or payload.get("message") or payload
        if not isinstance(msg, str):
            msg = json.dumps(msg, default=str)
        msg = self.scrub(" ".join(msg.split()))[:160]
        return "network: " + msg if status is None else "HTTP %s: %s" % (status, msg)

    def keychain(self, service, account=None):
        """A generic password from the login keychain → (value, problem).

        `security` runs under a timeout, so a locked keychain or an access prompt
        in a GUI session can never hang the run. Any problem other than "no such
        item" would recur for every other account, so callers stop at the first.
        Callers keep() the secrets they take out of the value."""
        if not IS_MAC or self.opts.get("keychain") is False:
            return None, None
        argv = [SECURITY, "find-generic-password", "-s", service] + (["-a", account] if account else []) + ["-w"]
        rc, out, _ = _run(argv, float(self.opts.get("keychain_timeout") or 5))
        if rc == 0 and out.strip():
            return out.strip(), None
        if rc == 124:
            return None, "Keychain read timed out (locked, or waiting on an access prompt)"
        if rc == 36:  # errSecInteractionNotAllowed: ssh, cron and LaunchDaemons cannot use the login keychain
            return None, ("Keychain not readable in this session (rc 36: no GUI login, e.g. ssh or cron); collect "
                          "from the GUI session (porchlight install timer) or set keychain = false")
        if rc in (51, 128):  # errSecAuthFailed, userCanceledErr: someone refused an access prompt
            return None, "Keychain access was refused (rc %d)" % rc
        return None, None  # rc 44: no such item


# ── Claude (Pro/Max OAuth) ──────────────────────────────────────────────────
def claude_dir(opts) -> tuple:
    """(as configured, resolved): opts claude_dir, else $CLAUDE_CONFIG_DIR, else ~/.claude."""
    spec = str(opts.get("claude_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or "")
    return spec, (_path(spec) if spec else os.path.join(_home(), ".claude"))


def claude_services(spec: str, path: str) -> list:
    """The Keychain services Claude Code keeps this config dir's login under.

    With CLAUDE_CONFIG_DIR set, Claude Code appends -<8 hex of sha256(the variable
    as written)>, so a second config dir never sees the first one's login. A spec is
    hashed as written and expanded (a shell expands ~ before Claude Code sees it); the
    plain name belongs to ~/.claude used without the variable."""
    import hashlib
    import unicodedata
    names = []
    for d in ([spec, _path(spec)] if spec else []):
        digest = hashlib.sha256(unicodedata.normalize("NFC", d).encode("utf-8")).hexdigest()[:8]
        names.append("%s-%s" % (CLAUDE_SERVICE, digest))
    if os.path.realpath(path) == os.path.realpath(os.path.join(_home(), ".claude")):
        names.append(CLAUDE_SERVICE)
    return list(dict.fromkeys(names))


def keychain_accounts() -> list:
    """Accounts to try, likeliest first. Claude Code stores under $USER (claude-code-user
    when the name has unusual characters), older versions under "unknown", and None
    (no -a) takes whichever item the service holds."""
    try:
        import pwd
        login = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        login = None
    named = [a for a in (os.environ.get("USER"), login, "claude-code-user", "unknown") if a]
    return list(dict.fromkeys(named)) + [None]


def claude_logins(p) -> tuple:
    """Every readable claudeAiOauth login for the config dir, freshest expiresAt first;
    the Keychain problem that cut the search short; the resolved dir.

    The Keychain can hold several items under one service (a stale one under an old
    account name beside the live one), and `security -w` without -a returns whichever
    it meets first, so every candidate account is read."""
    spec, path = claude_dir(p.opts)
    found, problem = [], None

    def add(blob, where):
        c = blob.get("claudeAiOauth") if isinstance(blob, dict) else None
        if isinstance(c, dict) and isinstance(c.get("accessToken"), str) and c["accessToken"]:
            p.keep(c["accessToken"])
            p.keep(c.get("refreshToken"))
            found.append((c, where))
    add(_read_json(os.path.join(path, ".credentials.json")), "file")
    for svc in claude_services(spec, path) if IS_MAC and p.opts.get("keychain") is not False else []:
        for acct in keychain_accounts():
            raw, problem = p.keychain(svc, acct)
            if problem:
                break
            if raw:
                try:
                    add(json.loads(raw), "keychain:%s" % (acct or "any"))
                except ValueError:
                    pass
        if problem:
            break
    found.sort(key=lambda f: _num(f[0].get("expiresAt")) or 0, reverse=True)
    return found, problem, path


def claude_windows(d) -> list:
    """The `limits` list Claude Code's /usage renders, else the older five_hour/seven_day keys."""
    out = []
    for lim in d.get("limits") if isinstance(d.get("limits"), list) else []:
        if not isinstance(lim, dict):
            continue
        kind, model = lim.get("kind"), _dig(lim, "scope", "model", "display_name")
        label = {"session": "5h", "weekly_all": "7d"}.get(kind) or (
            "7d %s" % model if kind == "weekly_scoped" and model else str(kind))
        out.append(_win(label, lim.get("percent"), lim.get("resets_at"), severity=lim.get("severity"),
                        extra=None if kind in ("session", "weekly_all") else True))
    if out:
        return out
    for key in ["five_hour", "seven_day"] + sorted(k for k in d if str(k).startswith("seven_day_")):
        b = d.get(key)
        if isinstance(b, dict) and _num(b.get("utilization")) is not None:
            label = {"five_hour": "5h", "seven_day": "7d"}.get(key) or "7d " + key[len("seven_day_"):].replace("_", " ")
            out.append(_win(label, b["utilization"], b.get("resets_at"),
                            extra=None if key in ("five_hour", "seven_day") else True))
    return out


def _keychain_detail(problem) -> dict:
    """detail for a login the Keychain may hold but would not hand over (auth "keychain")."""
    return {"auth": "keychain", "keychain": problem.split(" (")[0]}


def fetch_claude(p) -> dict:
    logins, problem, path = claude_logins(p)
    if not logins:
        where = _tilde(os.path.join(path, ".credentials.json"))
        if IS_MAC and p.opts.get("keychain") is not False:
            where += " or the Keychain"
        return _result("claude", detail=_keychain_detail(problem) if problem else {"auth": "none"},
                       error="not signed in to a Claude plan (no claudeAiOauth in %s)%s" % (
                           where, "; " + problem if problem else ""))
    c, store = logins[0]
    plan = c.get("subscriptionType")
    if plan and c.get("rateLimitTier"):
        plan = "%s (%s)" % (plan, c["rateLimitTier"])
    exp = int((_num(c.get("expiresAt")) or 0) / 1000)
    refresh_exp = int((_num(c.get("refreshTokenExpiresAt")) or 0) / 1000)
    detail = {"token_store": store, "token_expires_at": exp}
    now = time.time()
    if exp < now + SKEW and problem:  # the Keychain we could not read may well hold a newer login
        detail.update(_keychain_detail(problem))
        return _result("claude", plan=plan, detail=detail, error="the readable login (%s) expired %s; %s" % (
            store, _fmt(exp), problem))
    if exp < now + SKEW:  # no request at all: the token is reported, never refreshed
        if refresh_exp and refresh_exp < now:
            detail["auth"] = "relogin"
            return _result("claude", plan=plan, detail=detail,
                           error="login expired %s; run `claude auth login`" % _fmt(refresh_exp))
        detail["auth"] = "expired"
        return _result("claude", plan=plan, detail=detail, error=(
            "access token expired %s; claude renews it on its next run (not refreshing here: the refresh "
            "token is single-use, and rotating it would sign Claude Code out)" % _fmt(exp)))
    status, d, ms = p.call(CLAUDE_USAGE_URL, {"Authorization": "Bearer " + c["accessToken"],
                                              "anthropic-beta": "oauth-2025-04-20"})
    if status != 200 or not isinstance(d, dict):
        if status == 401:
            detail["auth"] = "relogin"
        return _result("claude", plan=plan, latency_ms=ms, detail=detail, error=p.err(status, d))
    buckets = {k: {"used_usd": _num(v.get("used_dollars")), "limit_usd": _num(v.get("limit_dollars")),
                   "resets_at": _epoch(v.get("resets_at"))}
               for k, v in d.items() if isinstance(v, dict) and v.get("limit_dollars") is not None}
    if buckets:  # codenamed dollar budgets; their meaning is undocumented, so they stay in detail
        detail["dollar_buckets"] = buckets
    return _result("claude", plan=plan, windows=claude_windows(d), credits=claude_credits(d), source="api",
                   latency_ms=ms, detail=detail)


def _minor(m):
    """{"amount_minor": 1240, "exponent": 2} → 12.4; None for anything else."""
    if not isinstance(m, dict) or _num(m.get("amount_minor")) is None:
        return None
    e = _num(m.get("exponent"))
    return _num(m["amount_minor"]) / 10.0 ** int(2 if e is None else e)


def claude_credits(d):
    """Extra usage (pay-as-you-go past the plan's limits) from extra_usage, whose amounts are
    in minor units, or from the newer spend object."""
    ex = d.get("extra_usage") if isinstance(d.get("extra_usage"), dict) else {}
    sp = d.get("spend") if isinstance(d.get("spend"), dict) else {}
    if not ex and not sp:
        return None
    dp = _num(ex.get("decimal_places"))
    scale = 10.0 ** int(2 if dp is None else dp)
    used, limit = _num(ex.get("used_credits")), _num(ex.get("monthly_limit"))
    used = used / scale if used is not None else _minor(sp.get("used"))
    limit = limit / scale if limit is not None else _minor(sp.get("limit"))
    return {"extra_usage_enabled": bool(ex["is_enabled"] if "is_enabled" in ex else sp.get("enabled")),
            "currency": ex.get("currency") or _dig(sp, "used", "currency"),
            "used": None if used is None else round(used, 2),
            "monthly_limit": None if limit is None else round(limit, 2),
            "limit_reached": bool(ex.get("spend_limit_reached"))}


# ── Codex (ChatGPT plans) ───────────────────────────────────────────────────
def codex_home(opts) -> str:
    spec = opts.get("codex_dir") or os.environ.get("CODEX_HOME") or ""
    return _path(spec) if spec else os.path.join(_home(), ".codex")


def _wham_windows(rl, prefix="", extra=None) -> list:
    """primary_window/secondary_window → windows labelled by their length (5h, 7d), not by slot:
    a plan with a single weekly limit reports it as the primary window."""
    out, now = [], time.time()
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key) if isinstance(rl, dict) else None
        if isinstance(w, dict):
            reset, after = w.get("reset_at"), _num(w.get("reset_after_seconds"))
            if reset is None and after is not None:
                reset = now + after
            out.append(_win(prefix + _dur(w.get("limit_window_seconds")), w.get("used_percent"), reset, extra=extra))
    return out


def _codex_credits(cr):
    if not isinstance(cr, dict):
        return None
    return {"has_credits": cr.get("has_credits"), "unlimited": cr.get("unlimited"), "balance": _num(cr.get("balance"))}


def codex_from_wham(d, ms, detail) -> dict:
    rl = d.get("rate_limit") if isinstance(d.get("rate_limit"), dict) else {}
    windows = _wham_windows(rl) + _wham_windows(d.get("code_review_rate_limit"), "review ", True)
    for item in d.get("additional_rate_limits") if isinstance(d.get("additional_rate_limits"), list) else []:
        if isinstance(item, dict):
            name = str(item.get("limit_name") or item.get("metered_feature") or "extra")[:24]
            windows += _wham_windows(item.get("rate_limit") or item, name + " ", True)
    detail.update({"limit_reached": rl.get("limit_reached"), "allowed": rl.get("allowed"),
                   "rate_limit_reached_type": d.get("rate_limit_reached_type"),
                   "spend_control_reached": _dig(d, "spend_control", "reached")})
    return _result("codex", plan=d.get("plan_type"), windows=windows, credits=_codex_credits(d.get("credits")),
                   source="api", latency_ms=ms, detail=detail)


def _mtime(path) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def newest_rollouts(root, days=3, limit=6) -> list:
    """The newest rollout files under sessions/YYYY/MM/DD/ without a recursive walk:
    date directories newest first, until `days` of them held rollouts (a long session
    that started on an earlier day can hold the newest write)."""
    def subdirs(path):
        try:
            return sorted((d for d in os.listdir(path) if d.isdigit()), reverse=True)
        except OSError:
            return []
    files, held = [], 0
    for y in subdirs(root):
        for m in subdirs(os.path.join(root, y)):
            for dd in subdirs(os.path.join(root, y, m)):
                day = os.path.join(root, y, m, dd)
                try:
                    got = [os.path.join(day, f) for f in os.listdir(day)
                           if f.startswith("rollout-") and f.endswith((".jsonl", ".jsonl.zst"))]
                except OSError:
                    got = []
                if got:
                    files += got
                    held += 1
                    if held >= days:
                        return sorted(files, key=_mtime, reverse=True)[:limit]
    return sorted(files, key=_mtime, reverse=True)[:limit]


def _unzstd_tail(path, keep):
    """The last `keep` decompressed bytes of a .zst file (the newest events sit at the end):
    the stdlib module on Python 3.14+, else the zstd binary; None when neither can read it."""
    try:
        from compression import zstd
    except ImportError:
        exe = _which("zstd")
        if not exe:
            return None
        rc, out, _ = _run([exe, "-dcq", path], 15, text=False)
        return out[-keep:] if rc == 0 else None
    tail = b""
    try:
        with zstd.open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                tail = (tail + chunk)[-keep:]
    except (OSError, ValueError, EOFError, zstd.ZstdError):
        return None
    return tail


def _lines_reversed(path, max_bytes=8 << 20):
    """A rollout's lines, last first, from at most the last max_bytes. Plain .jsonl is read
    from the end in 64 KB blocks (a long session's file runs to hundreds of MB); .jsonl.zst,
    which Codex writes for finished sessions on some platforms, has to be decompressed."""
    if path.endswith(".zst"):
        raw = _unzstd_tail(path, max_bytes)
        for line in reversed(raw.split(b"\n") if raw else []):
            yield line
        return
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            pos, buf, read = fh.tell(), b"", 0
            while pos > 0 and read < max_bytes:
                n = min(65536, pos)
                pos -= n
                fh.seek(pos)
                buf = fh.read(n) + buf
                read += n
                lines = buf.split(b"\n")
                buf = lines[0]
                for line in reversed(lines[1:]):
                    yield line
            if buf and pos == 0:
                yield buf
    except OSError:  # vanished or unreadable mid-read: the snapshot simply ends here
        return


def _rollout_windows(rl, ts, now, prefix="", extra=None) -> list:
    out = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not isinstance(w, dict):
            continue
        reset, after = w.get("resets_at"), _num(w.get("resets_in_seconds"))
        if reset is None and after is not None and ts:
            reset = ts + after
        win = _win(prefix + _dur((_num(w.get("window_minutes")) or 0) * 60), w.get("used_percent"), reset, extra=extra)
        if win["resets_at"] and win["resets_at"] < now:  # the window rolled over after the snapshot
            win.update(used_pct=None, note="reset since snapshot")
        out.append(win)
    return out


def codex_rollout_snapshot(home):
    """Zero-network fallback: the rate limits Codex recorded in its newest token_count event.
    The main limit (limit_id "codex") gives the plan's windows; other limit ids met on the
    way back to it become extra windows. None when there is no snapshot, or when every
    window in it has reset since (it then says nothing about now)."""
    now = time.time()
    for path in newest_rollouts(os.path.join(home, "sessions")):
        seen = {}
        for line in _lines_reversed(path):
            if b'"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            pl = ev.get("payload") if isinstance(ev, dict) else None
            rl = (pl.get("rate_limits") or _dig(pl, "info", "rate_limits")) if isinstance(pl, dict) else None
            if isinstance(rl, dict):
                seen.setdefault(str(rl.get("limit_id") or "codex"), (ev, rl))
                if "codex" in seen:
                    break
        if not seen:
            continue
        ev, rl = seen.get("codex") or next(iter(seen.values()))
        ts = _epoch(ev.get("timestamp"))
        windows = _rollout_windows(rl, ts, now)
        for key, (ev2, other) in seen.items():
            if other is not rl:
                windows += _rollout_windows(other, _epoch(ev2.get("timestamp")), now,
                                            "%s " % str(other.get("limit_name") or key)[:24], True)
        if not any(w.get("used_pct") is not None for w in windows):
            return None
        return _result("codex", plan=rl.get("plan_type"), windows=windows, credits=_codex_credits(rl.get("credits")),
                       source="rollout", as_of=ts or int(now), detail={
                           "snapshot_age_s": int(now - ts) if ts else None,
                           "rate_limit_reached_type": rl.get("rate_limit_reached_type")})
    return None


def fetch_codex(p) -> dict:
    home = codex_home(p.opts)
    auth = _read_json(os.path.join(home, "auth.json"))
    auth = auth if isinstance(auth, dict) else {}
    tok = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    at, plan, detail = tok.get("access_token"), None, {}
    if isinstance(at, str) and at:
        for t in (at, tok.get("refresh_token"), tok.get("id_token")):
            p.keep(t)
        claims = _jwt_claims(at)
        plan = _dig(claims, "https://api.openai.com/auth", "chatgpt_plan_type")
        exp = int(_num(claims.get("exp")) or 0)
        detail["token_expires_at"] = exp
        if exp and exp < time.time() + SKEW:  # no request: reported, never refreshed
            detail["auth"] = "expired"
            err = "access token expired %s; codex renews it on its next run (not refreshing here)" % _fmt(exp)
        else:
            hdrs = {"Authorization": "Bearer " + at}
            if tok.get("account_id"):
                hdrs["ChatGPT-Account-Id"] = str(tok["account_id"])
            status, d, ms = p.call(CODEX_WHAM_URL, hdrs)
            if status == 200 and isinstance(d, dict):
                return codex_from_wham(d, ms, detail)
            if status == 401:
                detail["auth"] = "relogin"
            err = p.err(status, d)
    elif auth.get("OPENAI_API_KEY"):
        p.keep(auth.get("OPENAI_API_KEY"))
        detail["auth"] = "api_key"
        err = "codex is signed in with an API key (no ChatGPT plan limits)"
    else:
        detail["auth"] = "none"
        err = "not signed in to ChatGPT (no tokens in %s)" % _tilde(os.path.join(home, "auth.json"))
    snap = codex_rollout_snapshot(home)
    if snap:
        snap["detail"].update({k: v for k, v in detail.items() if k in ("auth", "token_expires_at")}, api_error=err)
        return snap
    return _result("codex", plan=plan, error=err, detail=detail)


# ── Cursor ──────────────────────────────────────────────────────────────────
def cursor_paths() -> tuple:
    """(cursor-agent's auth.json, the desktop app's state database) on this platform."""
    home = _home()
    if IS_MAC:
        return (os.path.join(home, ".cursor", "auth.json"),
                os.path.join(home, "Library", "Application Support", "Cursor", "User", "globalStorage", "state.vscdb"))
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(xdg, "cursor", "auth.json"), os.path.join(xdg, "Cursor", "User", "globalStorage", "state.vscdb")


def _vscdb(path) -> dict:
    """The desktop app's cursorAuth rows, from its state database opened read-only
    (mode=ro: this process can never write to it, even while Cursor holds it open)."""
    try:
        import sqlite3
        from urllib.parse import quote
    except ImportError:  # a Python built without sqlite
        return {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % quote(path), uri=True, timeout=3)
        try:
            rows = con.execute("SELECT key, value FROM ItemTable WHERE key IN (?, ?)",
                               ("cursorAuth/accessToken", "cursorAuth/stripeMembershipType")).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {k: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v) for k, v in rows}


def cursor_tokens(p) -> tuple:
    """([(token, where, cached plan)], Keychain problem): cursor-agent's login first (its
    Keychain item on macOS, then its auth.json), then the desktop app's."""
    out = []
    raw, problem = p.keychain("cursor-access-token", "cursor-user")
    if raw:
        out.append((p.keep(raw), "cursor-agent keychain", None))
    auth_file, db = cursor_paths()
    a = _read_json(auth_file)
    tok = a.get("accessToken") if isinstance(a, dict) else None
    if isinstance(tok, str) and tok:
        p.keep(a.get("refreshToken"))
        out.append((p.keep(tok), "cursor-agent auth.json", None))
    if os.path.exists(db):
        kv = _vscdb(db)
        tok = kv.get("cursorAuth/accessToken")
        if isinstance(tok, str) and tok:
            plan = kv.get("cursorAuth/stripeMembershipType")
            out.append((p.keep(tok), "desktop app", plan if isinstance(plan, str) else None))
    return out, problem


def _cents(v):
    v = _num(v)
    return None if v is None else round(v / 100.0, 2)


def _cursor_windows(total, auto, api, end) -> list:
    """The cycle's included usage; auto and api are its two shares (extra windows)."""
    wins = [_win("total", total, end), _win("auto", auto, end, extra=True), _win("api", api, end, extra=True)]
    return [w for w in wins if w["used_pct"] is not None]


def cursor_summary(p, token) -> tuple:
    """Fallback: the web dashboard's endpoint, authenticated by the session cookie the site
    builds from the same JWT (the user id from its subject, then the token itself)."""
    from urllib.parse import quote
    uid = str(_jwt_claims(token).get("sub", "")).split("|")[-1]
    cookie = "WorkosCursorSessionToken=" + p.keep(quote(uid + "::" + token, safe=""))
    status, d, ms = p.call(CURSOR_SUMMARY_URL, {"Cookie": cookie})
    if status != 200 or not isinstance(d, dict):
        return None, p.err(status, d), ms
    plan_u = _dig(d, "individualUsage", "plan") or {}
    od = _dig(d, "individualUsage", "onDemand") or {}
    end = d.get("billingCycleEnd")
    windows = _cursor_windows(plan_u.get("totalPercentUsed"), plan_u.get("autoPercentUsed"),
                              plan_u.get("apiPercentUsed"), end)
    credits = {"included_used_usd": _cents(plan_u.get("used")), "included_limit_usd": _cents(plan_u.get("limit")),
               "bonus_usd": _cents(_dig(plan_u, "breakdown", "bonus")), "on_demand_used_usd": _cents(od.get("used")),
               "on_demand_enabled": od.get("enabled")}
    return _result("cursor", plan=d.get("membershipType"), windows=windows, credits=credits, source="api",
                   latency_ms=ms, error=None if windows else "Cursor reported no usage percentages",
                   detail={"cycle_start": _epoch(d.get("billingCycleStart")),
                           "message": d.get("autoModelSelectedDisplayMessage")}), None, ms


def fetch_cursor(p) -> dict:
    cands, problem = cursor_tokens(p)
    if not cands:
        return _result("cursor", detail=_keychain_detail(problem) if problem else {"auth": "none"},
                       error="not signed in to Cursor (no cursor-agent login, no desktop app token)%s" % (
                           "; " + problem if problem else ""))
    now = time.time()
    exps = [int(_num(_jwt_claims(c[0]).get("exp")) or 0) for c in cands]
    live = [c for c, e in zip(cands, exps) if e > now + SKEW]
    if not live and problem:  # cursor-agent's Keychain item, unread, may be the live one
        return _result("cursor", detail=dict(_keychain_detail(problem), token_expires_at=max(exps)),
                       error="the readable Cursor logins expired %s; %s" % (_fmt(max(exps)), problem))
    if not live:  # no request: reported, never refreshed
        return _result("cursor", detail={"auth": "relogin", "token_expires_at": max(exps)}, error=(
            "login expired %s; open Cursor or run `cursor-agent login`" % _fmt(max(exps))))
    token, where, plan = live[0]
    detail = {"token_source": where, "token_expires_at": exps[cands.index(live[0])]}
    status, d, ms = p.call(CURSOR_USAGE_URL, {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                              "Connect-Protocol-Version": "1"}, "POST", b"{}")
    if status != 200 or not isinstance(d, dict):
        res, err2, ms2 = cursor_summary(p, token)
        if res:
            res["detail"].update(detail, api2_error=p.err(status, d))
            res["plan"] = res.get("plan") or plan
            return res
        if status == 401:
            detail["auth"] = "relogin"
        return _result("cursor", plan=plan, latency_ms=ms + ms2, detail=detail,
                       error="%s; fallback %s" % (p.err(status, d), err2))
    if not plan:  # cursor-agent logins carry no cached plan: ask the billing profile, keep one field
        st, prof, ms2 = p.call(CURSOR_PROFILE_URL, {"Authorization": "Bearer " + token})
        ms += ms2
        if st == 200 and isinstance(prof, dict):
            plan = prof.get("individualMembershipType") or prof.get("membershipType")
    pu = d.get("planUsage") if isinstance(d.get("planUsage"), dict) else {}
    end = d.get("billingCycleEnd")
    windows = _cursor_windows(pu.get("totalPercentUsed"), pu.get("autoPercentUsed"), pu.get("apiPercentUsed"), end)
    credits = {"included_used_usd": _cents(pu.get("includedSpend")), "included_limit_usd": _cents(pu.get("limit")),
               "bonus_usd": _cents(pu.get("bonusSpend")), "total_spend_usd": _cents(pu.get("totalSpend")),
               "on_demand_used_usd": _cents(_dig(d, "spendLimitUsage", "individualUsed"))}
    detail.update({"cycle_start": _epoch(d.get("billingCycleStart")), "message": d.get("displayMessage"),
                   "auto_message": d.get("autoModelSelectedDisplayMessage")})
    return _result("cursor", plan=plan if isinstance(plan, str) else None, windows=windows, credits=credits,
                   source="api", latency_ms=ms, detail=detail,
                   error=None if windows else "Cursor reported no usage percentages")


# ── Grok Build CLI (grok.com plans) ─────────────────────────────────────────
def fetch_grok(p) -> dict:
    """The grok CLI's /usage view: GET <its proxy>/v1/billing?format=credits. The path,
    headers and field names come from the grok 0.2 CLI; the body is parsed defensively."""
    gdir = os.path.join(_home(), ".grok")
    auth = _read_json(os.path.join(gdir, "auth.json"))
    entries = [v for v in (auth.values() if isinstance(auth, dict) else [])
               if isinstance(v, dict) and isinstance(v.get("key"), str) and v["key"]]
    detail = {}
    if p.opts.get("xai_api_key") or os.environ.get("XAI_API_KEY"):
        detail["note"] = ("XAI_API_KEY is set, and the grok CLI prefers an API key to this login, "
                          "so its usage bills the xAI API team instead")
    if not entries:
        detail["auth"] = "none"
        return _result("grok", detail=detail, error="not signed in to grok.com (no login in ~/.grok/auth.json)")
    for e in entries:
        p.keep(e["key"])
        p.keep(e.get("refresh_token"))
    e = max(entries, key=lambda v: _epoch(v.get("expires_at")) or 0)
    claims = _jwt_claims(e["key"])
    plan = "tier %s" % claims["tier"] if claims.get("tier") is not None else None
    exp = _epoch(e.get("expires_at")) or int(_num(claims.get("exp")) or 0)
    detail.update(token_expires_at=exp, auth_mode=e.get("auth_mode"))
    if exp < time.time() + SKEW:  # grok.com sessions last 7 days and need `grok login` again
        detail["auth"] = "relogin"
        return _result("grok", plan=plan, detail=detail, error="login expired %s; run `grok login`" % _fmt(exp))
    version = _dig(_read_json(os.path.join(gdir, "version.json")), "version") or GROK_CLIENT_VERSION
    status, d, ms = p.call(GROK_BILLING_URL, {"Authorization": "Bearer " + e["key"], "X-XAI-Token-Auth": "xai-grok-cli",
                                              "x-grok-client-version": str(version)})
    if status != 200 or not isinstance(d, dict):
        if status == 401:
            detail["auth"] = "relogin"
        return _result("grok", plan=plan, latency_ms=ms, detail=detail, error=p.err(status, d))
    b = d.get("credits") if isinstance(d.get("credits"), dict) else d
    end = _dig(b, "currentPeriod", "end") or b.get("billingPeriodEnd")
    windows = []
    if _num(b.get("creditUsagePercent")) is not None:
        windows.append(_win(str(b.get("billingCycle") or "period").lower()[:8], b["creditUsagePercent"], end))
    credits = {k: b[k] for k in GROK_CREDIT_KEYS if isinstance(b.get(k), (int, float, bool))}
    detail["response_keys"] = sorted(str(k) for k in b)[:20]
    tier = b.get("subscription_tier")
    return _result("grok", plan=tier if isinstance(tier, str) and tier else plan, windows=windows,
                   credits=credits or None, source="api", latency_ms=ms, detail=detail,
                   error=None if windows else "no usage percentage in the billing response")


# ── Orchestration ───────────────────────────────────────────────────────────
FETCHERS = {"claude": fetch_claude, "codex": fetch_codex, "cursor": fetch_cursor, "grok": fetch_grok}


def _seal(obj, p):
    """Every string in a result, scrubbed of the secrets this fetch read (and of anything token-shaped)."""
    if isinstance(obj, str):
        return p.scrub(obj)
    if isinstance(obj, dict):
        return {k: _seal(v, p) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_seal(v, p) for v in obj]
    return obj


def fetch(name: str, opts=None) -> dict:
    """One provider's reading. Never raises, and never returns a token."""
    p = Probe(opts)
    try:
        res = FETCHERS[name](p)
    except Exception as e:  # noqa: BLE001 — one provider must never break the collector
        res = _result(name, error="internal %s: %s" % (type(e).__name__, e))
    return _seal(res, p)


def fetch_many(names, opts=None, timeout=60.0) -> list:
    import threading
    out = {}

    def one(n):
        out[n] = fetch(n, opts)
    threads = [threading.Thread(target=one, args=(n,), daemon=True) for n in names]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return [out.get(n) or _result(n, error="timed out") for n in names]


_SSH_HOST = re.compile(r"^[A-Za-z0-9_.@%:\[\]][A-Za-z0-9_.@%:\[\]-]*$")  # no leading "-": an alias, never an option


def run_remote(host, names, opts=None, ssh=None, timeout=25.0) -> list:
    """Run this module on `host` over ssh and return its results.

    The source goes on stdin to `python3 -I -`: nothing is installed there, and -I
    keeps the remote's working directory and PYTHON* variables out of the import
    path. The login is read on the host that holds it; only the numbers come back.
    Each result's detail["via"] names the host."""
    import shlex
    opts, host = opts or {}, str(host or "")
    names = [n for n in names if n in FETCHERS]

    def fail(why):
        return [_result(n, error="via %s: %s" % (host or "?", why), detail={"via": host}) for n in names]
    if not _SSH_HOST.match(host):
        return fail("not a usable ssh host name")
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as fh:
            src = fh.read()
    except (NameError, OSError) as e:  # NameError: this module itself arrived on stdin
        return fail("cannot read the probe module (%s)" % type(e).__name__)
    argv = list(ssh or [_which("ssh") or "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "LogLevel=ERROR"])
    argv += [host, "python3", "-I", "-", "--json"]
    for flag, key in (("--claude-dir", "claude_dir"), ("--codex-dir", "codex_dir")):
        if opts.get(key):  # ssh hands the words to the remote shell: quote them for it
            argv += [flag, shlex.quote(str(opts[key]))]
    if opts.get("keychain") is False:
        argv.append("--no-keychain")
    rc, out, err = _run(argv + names, timeout, input_text=src)
    try:
        res = json.loads(out or "null")
    except ValueError:
        res = None
    if not (isinstance(res, list) and len(res) == len(names) and all(isinstance(r, dict) for r in res)):
        lines = [ln for ln in (err or "").splitlines() if ln.strip()]
        return fail("ssh timed out" if rc == 124 else "rc %d: %s" % (rc, scrub(lines[-1] if lines else "no output")[:120]))
    for r in res:
        if not isinstance(r.get("detail"), dict):
            r["detail"] = {}
        r["detail"]["via"] = host
    return res


def summary_line(r) -> str:
    def when(t):
        return time.strftime("%a %H:%M", time.localtime(t)) if t else "?"
    parts = ["%s %s%% (%s)" % (w.get("label"), "?" if w.get("used_pct") is None else "%g" % w["used_pct"],
                               when(w.get("resets_at"))) for w in r.get("windows") or []]
    ms = "%sms" % r["latency_ms"] if r.get("latency_ms") is not None else "-"
    return "%-7s %-28s %-8s %7s  %s" % (r.get("provider"), str(r.get("plan"))[:28], r.get("source") or "-", ms,
                                        r["error"] if r.get("error") else " | ".join(parts))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, names, host, as_json, i = {}, [], None, False, 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        if a == "--json":
            as_json = True
        elif a == "--no-keychain":
            opts["keychain"] = False
        elif a in ("--host", "--claude-dir", "--codex-dir") and i + 1 < len(argv):
            i += 1
            if a == "--host":
                host = argv[i]
            else:
                opts[a[2:].replace("-", "_")] = argv[i]
        elif a in FETCHERS:
            names.append(a)
        else:
            print("ai_probe: unknown argument %r (try --help)" % a, file=sys.stderr)
            return 2
        i += 1
    names = names or list(FETCHERS)
    results = run_remote(host, names, opts) if host else fetch_many(names, opts)
    if as_json:
        print(json.dumps(results, default=str))
    else:
        for r in results:
            print(summary_line(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())

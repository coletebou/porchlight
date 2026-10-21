"""AI plan limits: the standalone probe (Claude, Codex, Cursor, Grok), its sources,
alerts, the ssh fallback, the no-secret guarantee, and the AI LIMITS panel.

Every test runs in a scratch $HOME holding fake credential files, and every
subprocess the probe starts (Keychain, ssh, zstd) goes through a fake: nothing
here can read a real login, touch the real Keychain or reach the network. Each
fake secret carries the marker CANARY, so a leak shows up as that word."""
import ast
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import support  # noqa: F401 — must be first
from support import FakeHTTP, ctx, host, put, resp

from porchlight import collect, core, registry, render, theme as T
from porchlight.panels import ai as ai_panel
from porchlight.sources import ai_limits, ai_probe
from porchlight.ui import RCtx

NOW = time.time()
FAR = 1893474000  # 2030-01-01 05:00 UTC


def b64(d) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def jwt(claims, sig) -> str:
    """A JWT-shaped token (unsigned: the probe only reads its claims)."""
    return "%s.%s.%s" % (b64({"alg": "RS256", "typ": "JWT"}), b64(claims), sig)


def iso(t) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".250Z"


CLAUDE_TOKEN = "sk-ant-oat01-CANARYclaude0123456789abcdefABCDEF"
CLAUDE_REFRESH = "sk-ant-ort01-CANARYrefresh0123456789abcdef"


def claude_creds(expires_in=3600, refresh_in=None, token=CLAUDE_TOKEN, plan="max", tier="default_claude_max_20x"):
    c = {"accessToken": token, "refreshToken": CLAUDE_REFRESH, "expiresAt": int((NOW + expires_in) * 1000),
         "scopes": ["user:inference", "user:profile"], "subscriptionType": plan, "rateLimitTier": tier}
    if refresh_in is not None:
        c["refreshTokenExpiresAt"] = int((NOW + refresh_in) * 1000)
    # Other entries in the same file are never read, let alone returned.
    return {"claudeAiOauth": c, "mcpOAuth": {"example|1": {"accessToken": "CANARY-mcp-server-token-0123456789"}}}


USAGE_LIMITS = {
    "limits": [
        {"kind": "session", "group": "session", "percent": 17, "severity": "normal",
         "resets_at": "2030-01-01T05:00:00.123456+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 91, "severity": "critical", "resets_at": FAR, "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 40, "severity": "normal", "resets_at": FAR,
         "scope": {"model": {"display_name": "Opus"}}}],
    "five_hour": {"utilization": 99.0, "resets_at": None},  # the older keys: ignored when the list is there
    "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1240, "currency": "USD", "decimal_places": 2},
    "sample_budget": {"utilization": 17.3, "limit_dollars": 250, "used_dollars": 43.3, "resets_at": "2030-01-31T00:00:00Z"},
}


def codex_auth(exp_in=86400, plan="pro"):
    claims = {"exp": int(NOW + exp_in), "https://api.openai.com/auth": {"chatgpt_plan_type": plan,
                                                                       "chatgpt_account_id": "acct-fake"}}
    at = jwt(claims, "CANARYcodexSignature0123456789abcdefghijk")
    return at, {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-10-01T00:00:00Z",
                "tokens": {"id_token": jwt({"email": "someone@example.com"}, "CANARYcodexIdSignature0123456789"),
                           "access_token": at, "refresh_token": "rt_CANARYcodexRefresh0123456789abcdef",
                           "account_id": "acct-fake"}}


WHAM = {
    "plan_type": "pro",
    "rate_limit": {"allowed": True, "limit_reached": False,
                   "primary_window": {"used_percent": 12, "limit_window_seconds": 18000, "reset_after_seconds": 3600,
                                      "reset_at": None},
                   "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800, "reset_at": FAR}},
    "code_review_rate_limit": {"primary_window": {"used_percent": 3, "limit_window_seconds": 604800, "reset_at": FAR},
                               "secondary_window": None},
    "additional_rate_limits": [{"limit_name": "codex-mini", "metered_feature": "codex_mini", "rate_limit": {
        "primary_window": {"used_percent": 55, "limit_window_seconds": 18000, "reset_at": FAR}}}],
    "credits": {"has_credits": True, "unlimited": False, "balance": "1520.5"},
    "rate_limit_reached_type": None, "spend_control": {"reached": False},
}

CURSOR_SIG = "CANARYcursorSignature0123456789abcdefghij"


def cursor_token(exp_in=86400) -> str:
    return jwt({"sub": "auth0|user_FAKE01", "exp": int(NOW + exp_in)}, CURSOR_SIG)


CURSOR_USAGE = {
    "billingCycleStart": str(int((NOW - 10 * 86400) * 1000)), "billingCycleEnd": str(int((NOW + 20 * 86400) * 1000)),
    "planUsage": {"totalPercentUsed": 88.2, "autoPercentUsed": 61.0, "apiPercentUsed": 27.2, "includedSpend": 1764,
                  "limit": 2000, "bonusSpend": 450, "totalSpend": 2214, "remainingBonus": True},
    "spendLimitUsage": {"totalSpend": 310, "individualUsed": 310, "limitType": "user"},
    "displayMessage": "You've used 88% of your included usage", "autoModelSelectedDisplayMessage": "Auto is selected",
}


class Sandbox(unittest.TestCase):
    """A scratch $HOME, no inherited credential variables, and a fake for every subprocess."""
    ENV = ("HOME", "USER", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "XAI_API_KEY")

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="home-", dir=support.TMP)
        self.saved_env = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ.update(HOME=self.home, USER="tester")
        self.saved = (ai_probe._run, ai_probe.IS_MAC)
        self.runs, self.on_run = [], None
        ai_probe._run = self.fake_run
        ai_probe.IS_MAC = False  # tests that want the macOS paths opt in

    def tearDown(self):
        ai_probe._run, ai_probe.IS_MAC = self.saved
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def fake_run(self, argv, timeout=5.0, input_text=None, text=True):
        self.runs.append({"argv": list(argv), "input": input_text, "timeout": timeout})
        if self.on_run:
            return self.on_run(list(argv), input_text)
        if argv[0] == ai_probe.SECURITY:
            return 44, "", "security: The specified item could not be found in the keychain."
        raise AssertionError("unexpected subprocess in a test: %r" % (argv,))

    def write(self, rel, obj) -> str:
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        return path

    def security_calls(self) -> list:
        return [r["argv"] for r in self.runs if r["argv"][0] == ai_probe.SECURITY]

    def assertNoSecrets(self, obj, *secrets):
        blob = json.dumps(obj, default=str)
        self.assertNotIn("CANARY", blob)
        for s in secrets:
            self.assertNotIn(s, blob)
            for seg in s.split("."):
                if len(seg) >= 16:
                    self.assertNotIn(seg, blob)


def opt(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


# ── Claude ──────────────────────────────────────────────────────────────────
class Claude(Sandbox):
    def test_limits_list_extra_usage_and_dollar_buckets(self):
        self.write(".claude/.credentials.json", claude_creds())
        fake = FakeHTTP({"api.anthropic.com/api/oauth/usage": resp(200, USAGE_LIMITS)})
        r = ai_probe.fetch("claude", {"http": fake})
        self.assertIsNone(r["error"])
        self.assertEqual([(w["label"], w["used_pct"]) for w in r["windows"]], [("5h", 17.0), ("7d", 91.0), ("7d Opus", 40.0)])
        self.assertEqual(r["windows"][0]["resets_at"], FAR)  # six fraction digits and an offset, on Python 3.9 too
        self.assertEqual([w.get("extra") for w in r["windows"]], [None, None, True])
        self.assertEqual(r["windows"][1]["severity"], "critical")
        self.assertEqual(r["plan"], "max (default_claude_max_20x)")
        self.assertEqual((r["source"], r["latency_ms"]), ("api", 12))
        self.assertEqual(r["credits"], {"extra_usage_enabled": True, "currency": "USD", "used": 12.4,
                                        "monthly_limit": 50.0, "limit_reached": False})
        self.assertEqual(r["detail"]["dollar_buckets"]["sample_budget"]["limit_usd"], 250)
        self.assertEqual(r["detail"]["token_store"], "file")
        (method, url, headers, kw), = fake.calls
        self.assertEqual((method, url), ("GET", ai_probe.CLAUDE_USAGE_URL))
        self.assertEqual(headers["Authorization"], "Bearer " + CLAUDE_TOKEN)
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertNoSecrets(r, CLAUDE_TOKEN, CLAUDE_REFRESH)

    def test_older_response_shape(self):
        self.write(".claude/.credentials.json", claude_creds(plan="pro", tier="default_claude_ai"))
        body = {"five_hour": {"utilization": 22.5, "resets_at": "2030-01-01T05:00:00Z"},
                "seven_day": {"utilization": 64, "resets_at": "2030-01-05T00:00:00Z"},
                "seven_day_opus": {"utilization": 12, "resets_at": "2030-01-05T00:00:00Z"},
                "seven_day_sonnet": None, "seven_day_oauth_apps": None}
        r = ai_probe.fetch("claude", {"http": FakeHTTP({"oauth/usage": resp(200, body)})})
        self.assertEqual([(w["label"], w["used_pct"], w.get("extra")) for w in r["windows"]],
                         [("5h", 22.5, None), ("7d", 64.0, None), ("7d opus", 12.0, True)])
        self.assertEqual(r["windows"][0]["resets_at"], FAR)
        self.assertIsNone(r["credits"])

    def test_newer_spend_object(self):
        self.write(".claude/.credentials.json", claude_creds())
        body = {"limits": USAGE_LIMITS["limits"][:1],
                "extra_usage": {"is_enabled": True, "monthly_limit": None, "used_credits": None, "spend_limit_reached": True},
                "spend": {"used": {"amount_minor": 1999, "currency": "USD", "exponent": 2},
                          "limit": {"amount_minor": 2000, "currency": "USD", "exponent": 2}, "enabled": True}}
        r = ai_probe.fetch("claude", {"http": FakeHTTP({"oauth/usage": resp(200, body)})})
        self.assertEqual(r["credits"], {"extra_usage_enabled": True, "currency": "USD", "used": 19.99,
                                        "monthly_limit": 20.0, "limit_reached": True})

    def test_expired_token_is_reported_without_any_request(self):
        self.write(".claude/.credentials.json", claude_creds(expires_in=-60, refresh_in=30 * 86400))
        fake = FakeHTTP()
        r = ai_probe.fetch("claude", {"http": fake})
        self.assertEqual(fake.calls, [])
        self.assertEqual((r["windows"], r["detail"]["auth"]), ([], "expired"))
        self.assertIn("renews it on its next run", r["error"])
        self.assertIn("not refreshing", r["error"])
        self.assertTrue(collect._soft_failure(r))  # the collector keeps the last good reading
        self.assertNoSecrets(r, CLAUDE_TOKEN, CLAUDE_REFRESH)

    def test_expired_refresh_token_asks_for_a_new_login(self):
        self.write(".claude/.credentials.json", claude_creds(expires_in=-60, refresh_in=-10))
        fake = FakeHTTP()
        r = ai_probe.fetch("claude", {"http": fake})
        self.assertEqual((fake.calls, r["detail"]["auth"]), ([], "relogin"))
        self.assertIn("`claude auth login`", r["error"])

    def test_rejected_token_is_scrubbed_from_the_error(self):
        self.write(".claude/.credentials.json", claude_creds())
        body = {"error": {"type": "authentication_error", "message": "Invalid bearer token " + CLAUDE_TOKEN}}
        r = ai_probe.fetch("claude", {"http": FakeHTTP({"oauth/usage": resp(401, body)})})
        self.assertTrue(r["error"].startswith("HTTP 401: Invalid bearer token"), r["error"])
        self.assertEqual(r["detail"]["auth"], "relogin")
        self.assertNoSecrets(r, CLAUDE_TOKEN)

    def test_not_signed_in(self):
        fake = FakeHTTP()
        r = ai_probe.fetch("claude", {"http": fake})
        self.assertEqual((fake.calls, r["windows"], r["detail"]["auth"]), ([], [], "none"))
        self.assertIn("~/.claude/.credentials.json", r["error"])  # the home directory folded to ~
        self.assertNotIn(self.home, json.dumps(r))


class Keychain(Sandbox):
    def setUp(self):
        super().setUp()
        ai_probe.IS_MAC = True

    def test_rc36_explains_and_stops_asking(self):
        self.on_run = lambda argv, inp: (36, "", "security: User interaction is not allowed.")
        fake = FakeHTTP()
        r = ai_probe.fetch("claude", {"http": fake})
        self.assertEqual(len(self.security_calls()), 1)  # the same answer for every account: asked once
        self.assertIn("rc 36", r["error"])
        self.assertIn("GUI", r["error"])
        self.assertEqual(fake.calls, [])
        self.assertEqual(r["detail"], {"auth": "keychain", "keychain": "Keychain not readable in this session"})
        self.assertEqual(ai_panel.hint("claude", r), "Keychain not readable in this session")

    def test_rc36_with_an_expired_file_copy_blames_the_keychain(self):
        # Claude Code on a Mac keeps the live login in the Keychain; a file copy can be days old.
        self.write(".claude/.credentials.json", claude_creds(expires_in=-3600))
        self.on_run = lambda argv, inp: (36, "", "")
        fake = FakeHTTP()
        r = ai_probe.fetch("claude", {"http": fake})
        self.assertEqual((fake.calls, r["detail"]["auth"], r["detail"]["token_store"]), ([], "keychain", "file"))
        self.assertIn("rc 36", r["error"])
        self.assertTrue(collect._soft_failure(r))

    def test_cursor_keychain_problem(self):
        self.on_run = lambda argv, inp: (36, "", "")
        r = ai_probe.fetch("cursor", {"http": FakeHTTP()})
        self.assertEqual(r["detail"]["auth"], "keychain")
        self.write(".cursor/auth.json", {"accessToken": cursor_token(exp_in=-5)})
        r = ai_probe.fetch("cursor", {"http": FakeHTTP()})
        self.assertEqual(r["detail"]["auth"], "keychain")  # the unread Keychain item may be the live login

    def test_rc36_still_uses_the_file_login(self):
        self.write(".claude/.credentials.json", claude_creds())
        self.on_run = lambda argv, inp: (36, "", "")
        fake = FakeHTTP({"oauth/usage": resp(200, USAGE_LIMITS)})
        r = ai_probe.fetch("claude", {"http": fake})
        self.assertIsNone(r["error"])
        self.assertEqual(r["detail"]["token_store"], "file")
        self.assertEqual(len(fake.calls), 1)

    def test_timeout_stops_asking(self):
        self.on_run = lambda argv, inp: (124, "", "timed out after 5s")
        r = ai_probe.fetch("claude", {"http": FakeHTTP()})
        self.assertEqual(len(self.security_calls()), 1)
        self.assertIn("timed out", r["error"])

    def test_reads_every_account_and_takes_the_freshest(self):
        stale = json.dumps(claude_creds(expires_in=600, token="sk-ant-oat01-CANARYstale0000000000000000000"))
        fresh = json.dumps(claude_creds(expires_in=7 * 3600, token="sk-ant-oat01-CANARYfresh0000000000000000000"))

        def on_run(argv, inp):
            acct = opt(argv, "-a")
            if acct == "claude-code-user":
                return 0, fresh + "\n", ""
            if acct in ("tester", None):
                return 0, stale + "\n", ""
            return 44, "", "not found"
        self.on_run = on_run
        self.write(".claude/.credentials.json", claude_creds(expires_in=300, token="sk-ant-oat01-CANARYfile00000000000000000000"))
        fake = FakeHTTP({"oauth/usage": resp(200, USAGE_LIMITS)})
        r = ai_probe.fetch("claude", {"http": fake})
        self.assertEqual(fake.calls[0][2]["Authorization"], "Bearer sk-ant-oat01-CANARYfresh0000000000000000000")
        self.assertEqual(r["detail"]["token_store"], "keychain:claude-code-user")
        calls = self.security_calls()
        accounts = [opt(c, "-a") for c in calls]
        self.assertEqual(accounts[0], "tester")
        self.assertEqual(accounts[-1], None)  # finally, whichever item the service holds
        self.assertIn("unknown", accounts)
        self.assertEqual({opt(c, "-s") for c in calls}, {"Claude Code-credentials"})
        self.assertTrue(all(c[-1] == "-w" for c in calls))
        self.assertTrue(all(r_["timeout"] <= 5 for r_ in self.runs))
        self.assertNoSecrets(r, "sk-ant-oat01-CANARYfresh0000000000000000000", "sk-ant-oat01-CANARYstale0000000000000000000")

    def test_config_dir_reads_its_own_hashed_service(self):
        work = os.path.join(self.home, ".claude-work")
        os.environ["CLAUDE_CONFIG_DIR"] = work
        r = ai_probe.fetch("claude", {"http": FakeHTTP()})
        digest = hashlib.sha256(work.encode()).hexdigest()[:8]
        self.assertEqual({opt(c, "-s") for c in self.security_calls()}, {"Claude Code-credentials-" + digest})
        self.assertIn("~/.claude-work/.credentials.json", r["error"])

    def test_config_dir_setting_hashes_as_written_and_expanded(self):
        ai_probe.fetch("claude", {"http": FakeHTTP(), "claude_dir": "~/.claude-work"})
        want = {"Claude Code-credentials-" + hashlib.sha256(d.encode()).hexdigest()[:8]
                for d in ("~/.claude-work", os.path.join(self.home, ".claude-work"))}
        self.assertEqual({opt(c, "-s") for c in self.security_calls()}, want)

    def test_default_dir_named_explicitly_also_reads_the_plain_service(self):
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.home, ".claude")
        ai_probe.fetch("claude", {"http": FakeHTTP()})
        services = {opt(c, "-s") for c in self.security_calls()}
        self.assertIn("Claude Code-credentials", services)
        self.assertEqual(len(services), 2)

    def test_keychain_false_never_runs_security(self):
        r = ai_probe.fetch("claude", {"http": FakeHTTP(), "keychain": False})
        self.assertEqual(self.runs, [])
        self.assertNotIn("Keychain", r["error"])


# ── Codex ───────────────────────────────────────────────────────────────────
def token_count(ts, primary=None, secondary=None, limit_id="codex", limit_name=None, plan="plus", balance="88"):
    return {"timestamp": iso(ts), "type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": {"input_tokens": 1200}},
        "rate_limits": {"limit_id": limit_id, "limit_name": limit_name, "primary": primary, "secondary": secondary,
                        "credits": {"has_credits": True, "unlimited": False, "balance": balance},
                        "plan_type": plan, "rate_limit_reached_type": None}}}


class Codex(Sandbox):
    def rollout(self, day, name, lines, mtime=None) -> str:
        text = "\n".join(ln if isinstance(ln, str) else json.dumps(ln) for ln in lines) + "\n"
        path = self.write(os.path.join(".codex", "sessions", day, name), text)
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def test_wham_windows_by_length_review_and_additional_limits(self):
        at, auth = codex_auth()
        self.write(".codex/auth.json", auth)
        fake = FakeHTTP({"chatgpt.com/backend-api/wham/usage": resp(200, WHAM)})
        r = ai_probe.fetch("codex", {"http": fake})
        self.assertIsNone(r["error"])
        self.assertEqual([(w["label"], w["used_pct"], w.get("extra")) for w in r["windows"]],
                         [("5h", 12.0, None), ("7d", 40.0, None), ("review 7d", 3.0, True), ("codex-mini 5h", 55.0, True)])
        self.assertLess(abs(r["windows"][0]["resets_at"] - (time.time() + 3600)), 5)  # from reset_after_seconds
        self.assertEqual(r["credits"], {"has_credits": True, "unlimited": False, "balance": 1520.5})
        self.assertEqual((r["plan"], r["source"]), ("pro", "api"))
        self.assertEqual(r["detail"]["limit_reached"], False)
        (_m, url, headers, _kw), = fake.calls
        self.assertEqual(headers["Authorization"], "Bearer " + at)
        self.assertEqual(headers["ChatGPT-Account-Id"], "acct-fake")
        self.assertNoSecrets(r, at, auth["tokens"]["refresh_token"], auth["tokens"]["id_token"])
        self.assertNotIn("acct-fake", json.dumps(r))
        self.assertNotIn("someone@example.com", json.dumps(r))

    def test_limit_reached_is_critical_and_first(self):
        _at, auth = codex_auth()
        self.write(".codex/auth.json", auth)
        wham = dict(WHAM, rate_limit=dict(WHAM["rate_limit"], limit_reached=True), rate_limit_reached_type="primary")
        h = host("")
        d = ai_limits.codex_usage(ctx(h, registry.SOURCES["codex_usage"].config(h), http=FakeHTTP({"wham/usage": resp(200, wham)})))
        self.assertEqual(d["alerts"][0], {"sev": "crit", "msg": "Codex rate limit reached (primary)"})
        self.assertNotIn("host", d)

    def test_rollout_snapshot_when_not_signed_in(self):
        self.write(".codex/auth.json", {"OPENAI_API_KEY": None, "tokens": None})
        ts = NOW - 600
        main = token_count(ts, primary={"used_percent": 23.0, "window_minutes": 300, "resets_in_seconds": 7200},
                           secondary={"used_percent": 61.0, "window_minutes": 10080, "resets_at": int(NOW + 3 * 86400)})
        older = token_count(ts - 60, primary={"used_percent": 20.0, "window_minutes": 300, "resets_in_seconds": 7260})
        spark = token_count(ts + 30, primary={"used_percent": 7.0, "window_minutes": 300, "resets_in_seconds": 9000},
                            limit_id="codex_other", limit_name="Fast Model")
        self.rollout("2026/10/20", "rollout-2026-10-20T09-00-00-a.jsonl",
                     [{"type": "session_meta"}, older, "{not json", main, spark, {"type": "response_item"}])
        fake = FakeHTTP()
        r = ai_probe.fetch("codex", {"http": fake})
        self.assertEqual(fake.calls, [])  # zero network
        self.assertEqual((r["source"], r["error"], r["plan"]), ("rollout", None, "plus"))
        self.assertEqual([(w["label"], w["used_pct"]) for w in r["windows"]], [("5h", 23.0), ("7d", 61.0), ("Fast Model 5h", 7.0)])
        self.assertEqual(r["windows"][0]["resets_at"], int(ts) + 7200)
        self.assertTrue(r["windows"][2]["extra"])
        self.assertEqual(r["as_of"], int(ts))
        self.assertEqual(r["credits"]["balance"], 88.0)
        self.assertEqual(r["detail"]["auth"], "none")
        self.assertIn("not signed in", r["detail"]["api_error"])
        self.assertGreaterEqual(r["detail"]["snapshot_age_s"], 599)

    def test_window_that_reset_since_the_snapshot_has_no_number(self):
        ts = NOW - 3 * 3600
        ev = token_count(ts, primary={"used_percent": 97.0, "window_minutes": 300, "resets_at": int(NOW - 60)},
                         secondary={"used_percent": 50.0, "window_minutes": 10080, "resets_at": int(NOW + 86400)})
        self.rollout("2026/10/21", "rollout-2026-10-21T01-00-00-b.jsonl", [ev])
        r = ai_probe.fetch("codex", {"http": FakeHTTP()})
        self.assertEqual((r["windows"][0]["used_pct"], r["windows"][0]["note"]), (None, "reset since snapshot"))
        self.assertEqual(r["windows"][1]["used_pct"], 50.0)
        # every window rolled over: the snapshot says nothing about now
        ev = token_count(ts, primary={"used_percent": 97.0, "window_minutes": 300, "resets_at": int(NOW - 60)})
        self.rollout("2026/10/21", "rollout-2026-10-21T01-00-00-b.jsonl", [ev])
        r = ai_probe.fetch("codex", {"http": FakeHTTP()})
        self.assertEqual((r["source"], r["windows"]), (None, []))

    def test_expired_token_uses_the_rollout_without_a_request(self):
        at, auth = codex_auth(exp_in=-100)
        self.write(".codex/auth.json", auth)
        self.rollout("2026/10/21", "rollout-2026-10-21T08-00-00-c.jsonl",
                     [token_count(NOW - 60, primary={"used_percent": 33.0, "window_minutes": 10080, "resets_in_seconds": 86400})])
        fake = FakeHTTP()
        r = ai_probe.fetch("codex", {"http": fake})
        self.assertEqual(fake.calls, [])
        self.assertEqual((r["source"], r["windows"][0]["label"]), ("rollout", "7d"))
        self.assertEqual(r["detail"]["auth"], "expired")
        self.assertIn("renews it on its next run", r["detail"]["api_error"])
        self.assertNoSecrets(r, at)

    def test_api_key_login_has_no_plan_limits(self):
        self.write(".codex/auth.json", {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-proj-CANARYcodexApiKey0123456789", "tokens": None})
        fake = FakeHTTP()
        r = ai_probe.fetch("codex", {"http": fake})
        self.assertEqual((fake.calls, r["detail"]["auth"]), ([], "api_key"))
        self.assertIn("API key", r["error"])
        self.assertNoSecrets(r, "sk-proj-CANARYcodexApiKey0123456789")

    def test_rollout_walk_stops_after_three_days(self):
        for day in range(1, 21):
            ev = token_count(NOW - (30 - day) * 86400, primary={"used_percent": float(day), "window_minutes": 10080,
                                                               "resets_at": int(NOW + 86400)})
            self.rollout("2026/09/%02d" % day, "rollout-2026-09-%02dT10-00-00-x.jsonl" % day, [ev], mtime=NOW - (30 - day) * 86400)
        listed = []
        real = os.listdir

        def counting(path):
            listed.append(path)
            return real(path)
        with mock.patch.object(ai_probe.os, "listdir", counting):
            r = ai_probe.codex_rollout_snapshot(os.path.join(self.home, ".codex"))
        self.assertEqual(r["windows"][0]["used_pct"], 20.0)  # the newest day
        self.assertEqual(len(listed), 6)  # sessions, year, month and three days: never a full walk

    def test_zst_rollout_through_the_zstd_binary(self):
        ev = token_count(NOW - 60, primary={"used_percent": 44.0, "window_minutes": 300, "resets_in_seconds": 600})
        path = self.write(".codex/sessions/2026/10/21/rollout-2026-10-21T07-00-00-z.jsonl.zst", "compressed bytes")
        payload = (json.dumps({"type": "session_meta"}) + "\n" + json.dumps(ev) + "\n").encode()

        def on_run(argv, inp):
            return (0, payload, b"") if argv[0] == "/fake/bin/zstd" else (44, "", "")
        self.on_run = on_run
        with mock.patch.dict(sys.modules, {"compression": None, "compression.zstd": None}), \
                mock.patch.object(ai_probe, "_which", lambda n: "/fake/bin/zstd" if n == "zstd" else None):
            r = ai_probe.codex_rollout_snapshot(os.path.join(self.home, ".codex"))
        self.assertEqual(r["windows"][0]["used_pct"], 44.0)
        self.assertEqual(self.runs[0]["argv"], ["/fake/bin/zstd", "-dcq", path])
        with mock.patch.dict(sys.modules, {"compression": None, "compression.zstd": None}), \
                mock.patch.object(ai_probe, "_which", lambda n: None):
            self.assertIsNone(ai_probe.codex_rollout_snapshot(os.path.join(self.home, ".codex")))  # no reader: no snapshot

    @unittest.skipUnless(sys.version_info >= (3, 14), "compression.zstd is new in Python 3.14")
    def test_zst_rollout_through_the_stdlib(self):
        from compression import zstd
        ev = token_count(NOW - 60, primary={"used_percent": 45.0, "window_minutes": 300, "resets_in_seconds": 600})
        path = os.path.join(self.home, ".codex/sessions/2026/10/21/rollout-2026-10-21T07-00-00-s.jsonl.zst")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(zstd.compress((json.dumps(ev) + "\n").encode()))
        r = ai_probe.codex_rollout_snapshot(os.path.join(self.home, ".codex"))
        self.assertEqual(r["windows"][0]["used_pct"], 45.0)
        self.assertEqual(self.runs, [])

    def test_codex_home_setting(self):
        _at, auth = codex_auth()
        self.write("alt-codex/auth.json", auth)
        r = ai_probe.fetch("codex", {"http": FakeHTTP({"wham/usage": resp(200, WHAM)}), "codex_dir": "~/alt-codex"})
        self.assertEqual(r["source"], "api")


# ── Cursor ──────────────────────────────────────────────────────────────────
class Cursor(Sandbox):
    def test_dashboard_usage_and_billing_profile(self):
        tok = cursor_token()
        self.write(".config/cursor/auth.json", {"accessToken": tok, "refreshToken": "CANARYcursorRefresh0123456789"})
        profile = {"membershipType": "pro", "email": "someone@example.com", "customerId": "cus_FAKE"}
        fake = FakeHTTP({"GetCurrentPeriodUsage": resp(200, CURSOR_USAGE), "full_stripe_profile": resp(200, profile)})
        r = ai_probe.fetch("cursor", {"http": fake})
        self.assertIsNone(r["error"])
        self.assertEqual(r["plan"], "pro")
        self.assertEqual([(w["label"], w["used_pct"], w.get("extra")) for w in r["windows"]],
                         [("total", 88.2, None), ("auto", 61.0, True), ("api", 27.2, True)])
        self.assertEqual(r["windows"][0]["resets_at"], int(NOW + 20 * 86400))
        self.assertEqual(r["credits"], {"included_used_usd": 17.64, "included_limit_usd": 20.0, "bonus_usd": 4.5,
                                        "total_spend_usd": 22.14, "on_demand_used_usd": 3.1})
        self.assertEqual(r["detail"]["message"], "You've used 88% of your included usage")
        self.assertEqual(r["detail"]["token_source"], "cursor-agent auth.json")
        (m1, u1, h1, kw1), (m2, u2, h2, _kw2) = fake.calls
        self.assertEqual((m1, kw1["data"], h1["Connect-Protocol-Version"], h1["Content-Type"]),
                         ("POST", b"{}", "1", "application/json"))
        self.assertEqual(h1["Authorization"], "Bearer " + tok)
        self.assertIn("full_stripe_profile", u2)
        blob = json.dumps(r)
        self.assertNotIn("someone@example.com", blob)  # one field kept from the billing profile
        self.assertNotIn("cus_FAKE", blob)
        self.assertNoSecrets(r, tok)

    def test_web_dashboard_fallback_sends_the_cookie_only_there(self):
        tok = cursor_token()
        self.write(".config/cursor/auth.json", {"accessToken": tok})
        summary = {"membershipType": "pro", "billingCycleStart": "2029-12-31T00:00:00.000Z",
                   "billingCycleEnd": "2030-01-01T05:00:00.000Z",
                   "individualUsage": {"plan": {"totalPercentUsed": 40, "autoPercentUsed": 30, "apiPercentUsed": 10,
                                                "used": 800, "limit": 2000, "breakdown": {"bonus": 150}},
                                       "onDemand": {"used": 0, "enabled": False}}}
        fake = FakeHTTP({"GetCurrentPeriodUsage": resp(500, {"error": "internal " + tok}),
                         "cursor.com/api/usage-summary": resp(200, summary)})
        r = ai_probe.fetch("cursor", {"http": fake})
        self.assertEqual([(w["label"], w["used_pct"]) for w in r["windows"]], [("total", 40.0), ("auto", 30.0), ("api", 10.0)])
        self.assertEqual(r["windows"][0]["resets_at"], FAR)
        self.assertEqual(r["credits"]["included_limit_usd"], 20.0)
        self.assertEqual(r["credits"]["bonus_usd"], 1.5)
        self.assertTrue(r["detail"]["api2_error"].startswith("HTTP 500"), r["detail"]["api2_error"])
        (_m1, u1, h1, _k1), (_m2, u2, h2, _k2) = fake.calls
        self.assertNotIn("Cookie", h1)
        self.assertTrue(u2.startswith("https://cursor.com/"))
        self.assertTrue(h2["Cookie"].startswith("WorkosCursorSessionToken=user_FAKE01%3A%3A"), h2["Cookie"][:40])
        self.assertNotIn("Authorization", h2)
        self.assertNoSecrets(r, tok)

    def test_desktop_state_database_is_opened_read_only(self):
        ai_probe.IS_MAC = True
        tok = cursor_token()
        d = os.path.join(self.home, "Library", "Application Support", "Cursor", "User", "globalStorage")
        os.makedirs(d)
        db = os.path.join(d, "state.vscdb")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        con.executemany("INSERT INTO ItemTable VALUES (?, ?)", [("cursorAuth/accessToken", tok),
                                                                  ("cursorAuth/stripeMembershipType", "pro"),
                                                                  ("cursorAuth/refreshToken", "CANARYdesktopRefresh0123456789")])
        con.commit()
        con.close()
        before = (os.stat(db).st_mtime_ns, os.stat(db).st_size, sorted(os.listdir(d)))
        fake = FakeHTTP({"GetCurrentPeriodUsage": resp(200, CURSOR_USAGE)})
        r = ai_probe.fetch("cursor", {"http": fake})
        self.assertEqual((r["detail"]["token_source"], r["plan"]), ("desktop app", "pro"))
        self.assertEqual(len(fake.calls), 1)  # the cached plan saves the profile request
        self.assertEqual([(opt(c, "-s"), opt(c, "-a")) for c in self.security_calls()], [("cursor-access-token", "cursor-user")])
        self.assertEqual(before, (os.stat(db).st_mtime_ns, os.stat(db).st_size, sorted(os.listdir(d))))
        self.assertNoSecrets(r, tok)

    def test_expired_logins_make_no_request(self):
        self.write(".config/cursor/auth.json", {"accessToken": cursor_token(exp_in=-5)})
        fake = FakeHTTP()
        r = ai_probe.fetch("cursor", {"http": fake})
        self.assertEqual((fake.calls, r["detail"]["auth"]), ([], "relogin"))
        self.assertIn("cursor-agent login", r["error"])

    def test_not_signed_in(self):
        r = ai_probe.fetch("cursor", {"http": FakeHTTP()})
        self.assertEqual((r["detail"]["auth"], r["windows"]), ("none", []))

    def alerts(self, total, message=None, api=10.0, toml=""):
        res = {"windows": [{"label": "total", "used_pct": total, "resets_at": FAR},
                           {"label": "auto", "used_pct": total, "resets_at": FAR, "extra": True},
                           {"label": "api", "used_pct": api, "resets_at": FAR, "extra": True}],
               "credits": {"on_demand_used_usd": 81.49}, "detail": {"message": message}}
        h = host(toml)
        return ai_limits.cursor_alerts(res, *ai_limits.thresholds(ctx(h, registry.SOURCES["cursor_usage"].config(h)), "cursor"))

    def test_one_alert_in_cursors_words(self):
        when = time.strftime("%a %H:%M", time.localtime(FAR))
        self.assertEqual(self.alerts(89.7, "You've used 90% of your included usage."),
                         [{"sev": "warn", "msg": "Cursor: You've used 90%% of your included usage (resets %s, on-demand $81)" % when}])
        self.assertEqual(self.alerts(99.0)[0]["sev"], "crit")
        self.assertEqual(self.alerts(50.0, "You've used 50% of your included usage"), [])  # routine, below warn
        self.assertEqual(self.alerts(50.0, "You've hit your on-demand spend limit")[0]["sev"], "warn")
        self.assertEqual(self.alerts(50.0, api=99.0), [])  # the api share is a slice of the total
        self.assertEqual(self.alerts(70.0, toml="[sources.cursor_usage]\nwarn = 60\n")[0]["sev"], "warn")


# ── Grok ────────────────────────────────────────────────────────────────────
GROK_LIVE = jwt({"tier": 2, "exp": int(NOW + 5 * 86400)}, "CANARYgrokSignature0123456789abcdefghijklm")
GROK_OLD = jwt({"tier": 1}, "CANARYgrokOldSignature0123456789abcdefghij")


def grok_auth(*entries):
    return {"https://auth.example::%d" % i: e for i, e in enumerate(entries)}


def grok_entry(key, expires_in):
    return {"key": key, "auth_mode": "oauth", "expires_at": iso(NOW + expires_in), "user_id": "u-fake",
            "email": "someone@example.com", "first_name": "Sam", "team_id": "team-fake",
            "refresh_token": "CANARYgrokRefresh0123456789abcdef"}


class Grok(Sandbox):
    def test_billing_credits(self):
        self.write(".grok/auth.json", grok_auth(grok_entry(GROK_OLD, -86400), grok_entry(GROK_LIVE, 5 * 86400)))
        self.write(".grok/version.json", {"version": "0.3.7"})
        body = {"credits": {"billingCycle": "MONTHLY", "creditUsagePercent": 42.5,
                            "currentPeriod": {"start": "2029-12-01T00:00:00Z", "end": "2030-01-01T05:00:00Z"},
                            "monthlyLimit": 1000, "includedUsed": 425, "subscription_tier": "SuperGrok", "note": "x"}}
        fake = FakeHTTP({"cli-chat-proxy.grok.com/v1/billing": resp(200, body)})
        r = ai_probe.fetch("grok", {"http": fake})
        self.assertIsNone(r["error"])
        self.assertEqual(r["plan"], "SuperGrok")
        self.assertEqual(r["windows"], [{"label": "monthly", "used_pct": 42.5, "resets_at": FAR}])
        self.assertEqual(r["credits"], {"monthlyLimit": 1000, "includedUsed": 425})
        (_m, url, headers, _kw), = fake.calls
        self.assertEqual(url, ai_probe.GROK_BILLING_URL)
        self.assertEqual(headers["Authorization"], "Bearer " + GROK_LIVE)  # the live login, not the stale one
        self.assertEqual((headers["X-XAI-Token-Auth"], headers["x-grok-client-version"]), ("xai-grok-cli", "0.3.7"))
        blob = json.dumps(r)
        for private in ("someone@example.com", "Sam", "u-fake", "team-fake"):
            self.assertNotIn(private, blob)
        self.assertNoSecrets(r, GROK_LIVE, GROK_OLD)

    def test_expired_session_makes_no_request(self):
        self.write(".grok/auth.json", grok_auth(grok_entry(GROK_OLD, -3600)))
        fake = FakeHTTP()
        r = ai_probe.fetch("grok", {"http": fake})
        self.assertEqual((fake.calls, r["detail"]["auth"], r["plan"]), ([], "relogin", "tier 1"))
        self.assertIn("`grok login`", r["error"])

    def test_api_key_note_and_missing_percentage(self):
        os.environ["XAI_API_KEY"] = "xai-CANARYgrokApiKey0123456789abcdef"
        self.write(".grok/auth.json", grok_auth(grok_entry(GROK_LIVE, 5 * 86400)))
        r = ai_probe.fetch("grok", {"http": FakeHTTP({"v1/billing": resp(200, {"credits": {"monthlyLimit": 1000}})})})
        self.assertIn("XAI_API_KEY", r["detail"]["note"])
        self.assertEqual(r["windows"], [])
        self.assertIn("no usage percentage", r["error"])
        self.assertNoSecrets(r, "xai-CANARYgrokApiKey0123456789abcdef", GROK_LIVE)


# ── The no-secret guarantee ─────────────────────────────────────────────────
class NoSecrets(Sandbox):
    def test_tokens_echoed_back_in_failures_never_reach_a_result(self):
        """Each provider's server answers with every secret of that provider's login in its
        error (the token it was sent, and the refresh and id tokens it never was)."""
        at, auth = codex_auth()
        tok = cursor_token()
        self.write(".claude/.credentials.json", claude_creds())
        self.write(".codex/auth.json", auth)
        self.write(".config/cursor/auth.json", {"accessToken": tok, "refreshToken": "CANARYcursorRefresh0123456789"})
        self.write(".grok/auth.json", grok_auth(grok_entry(GROK_LIVE, 86400)))
        held = {"claude": (CLAUDE_TOKEN, CLAUDE_REFRESH),
                "codex": (at, auth["tokens"]["refresh_token"], auth["tokens"]["id_token"]),
                "cursor": (tok, "CANARYcursorRefresh0123456789"),
                "grok": (GROK_LIVE, "CANARYgrokRefresh0123456789abcdef")}
        urls = {"claude": ("oauth/usage",), "codex": ("wham/usage",), "cursor": ("GetCurrentPeriodUsage", "usage-summary"),
                "grok": ("v1/billing",)}
        for name, secrets in held.items():
            echo = " ".join(secrets)
            for answer in (resp(403, {"error": {"message": "forbidden for " + echo}}), resp(401, {"detail": echo}),
                           resp(400, {"message": [echo]}), resp(429, "slow down " + echo), resp(500, "<html>" + echo),
                           ConnectionResetError("reset while sending Authorization: Bearer " + echo)):
                r = ai_probe.fetch(name, {"http": FakeHTTP({u: answer for u in urls[name]})})
                self.assertTrue(r["error"], (name, r))
                self.assertNoSecrets(r, *secrets)

    def test_an_internal_error_is_scrubbed_too(self):
        def boom(p):
            p.keep("sk-ant-oat01-CANARYinternal000000000000000")
            raise RuntimeError("failed with sk-ant-oat01-CANARYinternal000000000000000 in hand")
        with mock.patch.dict(ai_probe.FETCHERS, {"grok": boom}):
            r = ai_probe.fetch("grok")
        self.assertTrue(r["error"].startswith("internal RuntimeError"))
        self.assertNoSecrets(r)

    def test_scrub(self):
        self.assertEqual(ai_probe.scrub("task-runner-abc failed"), "task-runner-abc failed")  # not a key prefix
        self.assertEqual(ai_probe.scrub("key sk-ant-abc123456 bad"), "key sk-ant-… bad")
        self.assertEqual(ai_probe.scrub("x " + "A" * 44), "x <redacted>")
        self.assertEqual(ai_probe.scrub("tok=hunter2-is-long", ["hunter2-is-long"]), "tok=<token>")


# ── Over ssh: the probe runs where the login lives ──────────────────────────
def remote_result(provider="claude", host_name="far-box", **kw):
    r = {"provider": provider, "host": host_name, "plan": "max (default_claude_max_5x)", "credits": None,
         "windows": [{"label": "5h", "used_pct": 10.0, "resets_at": FAR}], "source": "api", "as_of": int(NOW),
         "latency_ms": 300, "error": None, "detail": {}}
    r.update(kw)
    return r


class Remote(Sandbox):
    def test_pipes_this_module_and_quotes_what_the_remote_shell_sees(self):
        self.on_run = lambda argv, inp: (0, json.dumps([remote_result()]), "")
        res = ai_probe.run_remote("devbox", ["claude"], {"claude_dir": "~/my claude", "keychain": False},
                                  ssh=["ssh", "-o", "BatchMode=yes"], timeout=20)
        (call,) = self.runs
        argv = call["argv"]
        self.assertEqual(argv[:8], ["ssh", "-o", "BatchMode=yes", "devbox", "python3", "-I", "-", "--json"])
        self.assertEqual(opt(argv, "--claude-dir"), "'~/my claude'")
        self.assertIn("--no-keychain", argv)
        self.assertEqual((argv[-1], call["timeout"]), ("claude", 20))
        with open(ai_probe.__file__, encoding="utf-8") as f:
            self.assertEqual(call["input"], f.read())  # the module itself, on stdin: nothing is installed there
        self.assertEqual(res[0]["detail"]["via"], "devbox")

    def test_an_alias_is_never_an_option(self):
        res = ai_probe.run_remote("-oProxyCommand=touch /tmp/x", ["claude"])
        self.assertEqual(self.runs, [])
        self.assertIn("not a usable ssh host name", res[0]["error"])

    def test_ssh_failure_is_one_line(self):
        self.on_run = lambda argv, inp: (255, "", "ssh: Could not resolve hostname devbox: nodename nor servname provided\n")
        (r,) = ai_probe.run_remote("devbox", ["codex"])
        self.assertEqual(r["error"], "via devbox: rc 255: ssh: Could not resolve hostname devbox: nodename nor servname provided")
        self.assertEqual(r["windows"], [])

    def settings(self, toml):
        h = host(toml)
        return h, registry.SOURCES["claude_usage"].config(h)

    def test_fallback_hosts_in_order_until_one_answers_live(self):
        self.write(".claude/.credentials.json", claude_creds(expires_in=-60))  # expired here: no request is made

        def on_run(argv, inp):
            target = argv[argv.index("python3") - 1]
            if target == "first":
                return 255, "", "ssh: connect to host first port 22: Connection refused"
            return 0, json.dumps([remote_result()]), ""
        self.on_run = on_run
        h, cfg = self.settings('[fleet]\nssh_options = ["IdentitiesOnly=yes"]\n'
                               '[sources.claude_usage]\nfallback_hosts = ["first", "second", "third"]\n')
        fake = FakeHTTP()
        d = ai_limits.claude_usage(ctx(h, cfg, http=fake))
        self.assertEqual(fake.calls, [])
        self.assertEqual([r["argv"][r["argv"].index("python3") - 1] for r in self.runs], ["first", "second"])
        self.assertEqual(d["detail"]["via"], "second")
        self.assertEqual((d["windows"][0]["used_pct"], d["error"]), (10.0, None))
        self.assertNotIn("host", d)
        self.assertNotIn("tried", d["detail"])
        argv = self.runs[0]["argv"]
        for want in ("BatchMode=yes", "ConnectTimeout=6", "IdentitiesOnly=yes"):
            self.assertIn(want, argv)

    def test_nobody_live_keeps_the_local_answer_and_says_who_was_tried(self):
        self.write(".claude/.credentials.json", claude_creds(expires_in=-60))
        self.on_run = lambda argv, inp: (255, "", "ssh: connect to host x port 22: Operation timed out")
        h, cfg = self.settings('[sources.claude_usage]\nfallback_hosts = ["a", "b"]\n')
        d = ai_limits.claude_usage(ctx(h, cfg, http=FakeHTTP()))
        self.assertEqual(d["detail"]["auth"], "expired")
        self.assertEqual([t.split(":")[0] for t in d["detail"]["tried"]], ["here", "a", "b"])

    def test_no_fallback_when_this_machine_answers(self):
        self.write(".claude/.credentials.json", claude_creds())
        h, cfg = self.settings('[sources.claude_usage]\nfallback_hosts = ["a"]\n')
        d = ai_limits.claude_usage(ctx(h, cfg, http=FakeHTTP({"oauth/usage": resp(200, USAGE_LIMITS)})))
        self.assertEqual((self.runs, d["source"]), ([], "api"))

    def test_codex_prefers_a_live_remote_to_a_local_snapshot(self):
        _at, auth = codex_auth(exp_in=-100)
        self.write(".codex/auth.json", auth)
        Codex.rollout(self, "2026/10/21", "rollout-2026-10-21T08-00-00-c.jsonl",
                      [token_count(NOW - 60, primary={"used_percent": 33.0, "window_minutes": 10080, "resets_in_seconds": 86400})])
        live = remote_result("codex", plan="pro", windows=[{"label": "7d", "used_pct": 35.0, "resets_at": FAR}])
        self.on_run = lambda argv, inp: (0, json.dumps([live]), "")
        h = host('[sources.codex_usage]\nfallback_hosts = ["devbox"]\nconfig_dir = "~/.codex"\n')
        d = ai_limits.codex_usage(ctx(h, registry.SOURCES["codex_usage"].config(h), http=FakeHTTP()))
        self.assertEqual((d["source"], d["windows"][0]["used_pct"], d["detail"]["via"]), ("api", 35.0, "devbox"))
        self.assertEqual(opt(self.runs[0]["argv"], "--codex-dir"), "'~/.codex'")  # as written: ~ expands over there

    def test_codex_without_a_live_answer_keeps_the_freshest_snapshot(self):
        self.write(".codex/auth.json", {"tokens": None})
        Codex.rollout(self, "2026/10/21", "rollout-2026-10-21T08-00-00-c.jsonl",
                      [token_count(NOW - 7200, primary={"used_percent": 33.0, "window_minutes": 10080, "resets_in_seconds": 86400})])
        newer = remote_result("codex", source="rollout", as_of=int(NOW - 60),
                              windows=[{"label": "7d", "used_pct": 36.0, "resets_at": FAR}])
        self.on_run = lambda argv, inp: (0, json.dumps([newer]), "")
        h = host('[sources.codex_usage]\nfallback_hosts = ["devbox"]\n')
        d = ai_limits.codex_usage(ctx(h, registry.SOURCES["codex_usage"].config(h), http=FakeHTTP()))
        self.assertEqual((d["source"], d["windows"][0]["used_pct"]), ("rollout", 36.0))
        self.assertEqual(len(d["detail"]["tried"]), 2)


class Standalone(Sandbox):
    """The ssh trick depends on the module running alone: no porchlight imports, stdlib only."""

    def test_imports_nothing_from_porchlight(self):
        with open(ai_probe.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "relative import in ai_probe")
                self.assertFalse((node.module or "").startswith("porchlight"))
            elif isinstance(node, ast.Import):
                self.assertFalse(any(a.name.startswith("porchlight") for a in node.names))

    def test_runs_from_stdin_like_the_remote_side(self):
        with open(ai_probe.__file__, encoding="utf-8") as f:
            src = f.read()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PORCHLIGHT", "PYTHON"))}
        env["HOME"] = self.home  # no logins at all: nothing to send, so nothing leaves this machine
        p = subprocess.run([sys.executable, "-I", "-", "--json", "--no-keychain"], input=src, capture_output=True,
                           text=True, timeout=60, env=env, cwd=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        res = json.loads(p.stdout)
        self.assertEqual([r["provider"] for r in res], ["claude", "codex", "cursor", "grok"])
        self.assertEqual({r["detail"].get("auth") for r in res}, {"none"})
        self.assertTrue(all(r["error"] and r["windows"] == [] for r in res))

    def test_cli_summary(self):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(ai_probe.main(["grok", "--no-keychain"]), 0)
            self.assertEqual(ai_probe.main(["--bogus"]), 2)
        self.assertTrue(out.getvalue().startswith("grok "))
        self.assertIn("unknown argument '--bogus'", err.getvalue())


# ── Alerts, settings and where the sources run ──────────────────────────────
class Alerts(unittest.TestCase):
    def test_thresholds_and_reset_in_the_message(self):
        res = {"windows": [{"label": "a", "used_pct": 74.9}, {"label": "b", "used_pct": 75},
                           {"label": "c", "used_pct": 89.9, "resets_at": FAR}, {"label": "d", "used_pct": 90},
                           {"label": "e", "used_pct": None}]}
        al = ai_limits.window_alerts("Claude", res, 75, 90)
        self.assertEqual([(a["sev"], a["msg"].split()[1]) for a in al], [("warn", "b"), ("warn", "c"), ("crit", "d")])
        self.assertEqual(al[1]["msg"], "Claude c at 89%% (resets %s)" % time.strftime("%a %H:%M", time.localtime(FAR)))

    def test_defaults_and_settings(self):
        for name, want in (("claude", (75, 90)), ("codex", (75, 90)), ("cursor", (85, 98)), ("grok", (75, 90))):
            h = host("")
            self.assertEqual(ai_limits.thresholds(ctx(h, registry.SOURCES[name + "_usage"].config(h)), name), want)
        h = host('[sources.claude_usage]\nwarn = 50\ncrit = "nonsense"\n')
        self.assertEqual(ai_limits.thresholds(ctx(h, registry.SOURCES["claude_usage"].config(h)), "claude"), (50.0, 90.0))
        cfg = registry.SOURCES["claude_usage"].config(host(""))
        self.assertEqual({k: cfg[k] for k in ("config_dir", "fallback_hosts", "keychain", "quiet_errors")},
                         {"config_dir": "", "fallback_hosts": [], "keychain": True, "quiet_errors": False})
        for name in ("claude_usage", "codex_usage", "cursor_usage", "grok_usage"):
            self.assertEqual(registry.SOURCES[name].scope, "global")


class Sources(Sandbox):
    def test_claude_source_alerts_and_drops_host(self):
        self.write(".claude/.credentials.json", claude_creds())
        h = host("")
        d = ai_limits.claude_usage(ctx(h, registry.SOURCES["claude_usage"].config(h),
                                       http=FakeHTTP({"oauth/usage": resp(200, USAGE_LIMITS)})))
        self.assertNotIn("host", d)
        self.assertEqual([a["sev"] for a in d["alerts"]], ["crit"])
        self.assertTrue(d["alerts"][0]["msg"].startswith("Claude 7d at 91% (resets "))
        self.assertEqual(sorted(d), ["alerts", "as_of", "credits", "detail", "error", "latency_ms", "plan", "provider",
                                     "source", "windows"])

    def test_spend_limit_alert(self):
        self.write(".claude/.credentials.json", claude_creds())
        body = {"limits": USAGE_LIMITS["limits"][:1], "extra_usage": {"is_enabled": True, "spend_limit_reached": True}}
        h = host("")
        d = ai_limits.claude_usage(ctx(h, registry.SOURCES["claude_usage"].config(h), http=FakeHTTP({"oauth/usage": resp(200, body)})))
        self.assertIn({"sev": "warn", "msg": "Claude extra usage hit its monthly spend limit"}, d["alerts"])

    def test_off_where_the_tool_is_not_set_up(self):
        h = host("")
        for name in ("claude_usage", "codex_usage", "cursor_usage", "grok_usage"):
            self.assertEqual(registry.state(registry.SOURCES[name], h), "other hosts", name)
        for d in (".claude", ".codex", ".cursor", ".grok"):
            os.makedirs(os.path.join(self.home, d))
        for name in ("claude_usage", "codex_usage", "cursor_usage", "grok_usage"):
            self.assertEqual(registry.state(registry.SOURCES[name], h), "on", name)

    def test_on_with_fallback_hosts_or_a_config_dir(self):
        h = host('[sources.grok_usage]\nfallback_hosts = ["devbox"]\n[sources.claude_usage]\nconfig_dir = "~/elsewhere"\n')
        self.assertEqual(registry.state(registry.SOURCES["grok_usage"], h), "on")
        self.assertEqual(registry.state(registry.SOURCES["claude_usage"], h), "other hosts")
        os.makedirs(os.path.join(self.home, "elsewhere"))
        self.assertEqual(registry.state(registry.SOURCES["claude_usage"], h), "on")

    def test_expired_login_keeps_the_last_good_reading(self):
        support.clear_cache()
        good = remote_result()
        good.pop("host")
        put("claude_usage", good)
        self.write(".claude/.credentials.json", claude_creds(expires_in=-60))
        rec = collect.run_one(registry.SOURCES["claude_usage"], host(""), {})
        self.assertEqual((rec["ok"], rec["soft"], rec["kept"]), (False, True, True))
        self.assertEqual(rec["data"]["windows"][0]["used_pct"], 10.0)
        self.assertIn("renews it on its next run", rec["error"])
        self.assertNoSecrets(core.read_json(registry.src_path("claude_usage")), CLAUDE_TOKEN, CLAUDE_REFRESH)


# ── The AI LIMITS panel ─────────────────────────────────────────────────────
def rec_data(provider, plan, windows, **kw):
    d = {"provider": provider, "plan": plan, "windows": windows, "credits": None, "source": "api", "as_of": int(NOW),
         "latency_ms": 200, "error": None, "detail": {}}
    d.update(kw)
    return d


def build_panel(width, toml=""):
    h = host(toml)
    cw = render.layout(width, h.cfg)[1]
    return ai_panel.p_ai(RCtx(h, registry.Cache(), {"vitals": {}}, [], cw)), cw


def plain(p) -> list:
    return [T.strip(line) for line in p.lines]


class Panel(Sandbox):
    def setUp(self):
        super().setUp()
        support.clear_cache()
        now = time.time()
        put("claude_usage", rec_data("claude", "max (default_claude_max_20x)", [
            {"label": "5h", "used_pct": 38.0, "resets_at": now + 8040},
            {"label": "7d", "used_pct": 82.0, "resets_at": now + 3 * 86400},
            {"label": "7d Opus", "used_pct": 64.0, "resets_at": now + 3 * 86400, "extra": True},
            {"label": "7d Sonnet", "used_pct": 21.0, "resets_at": now + 3 * 86400, "extra": True}],
            credits={"extra_usage_enabled": True, "used": 12.4, "monthly_limit": 50.0},
            alerts=[{"sev": "warn", "msg": "Claude 7d at 82% (resets Sat 09:00)"}]))
        put("codex_usage", rec_data("codex", "pro", [
            {"label": "5h", "used_pct": 4.0, "resets_at": now + 3 * 3600},
            {"label": "7d", "used_pct": 31.0, "resets_at": now + 5 * 86400},
            {"label": "review 7d", "used_pct": 3.0, "resets_at": now + 5 * 86400, "extra": True}],
            credits={"has_credits": True, "unlimited": False, "balance": 1520.5}))
        self.cursor_reset = now + 12 * 86400
        put("cursor_usage", rec_data("cursor", "pro", [
            {"label": "total", "used_pct": 61.0, "resets_at": self.cursor_reset},
            {"label": "auto", "used_pct": 34.0, "resets_at": self.cursor_reset, "extra": True},
            {"label": "api", "used_pct": 27.0, "resets_at": self.cursor_reset, "extra": True}],
            credits={"included_used_usd": 12.2, "included_limit_usd": 20.0, "bonus_usd": 4.5, "on_demand_used_usd": 3.0}))
        put("grok_usage", rec_data("grok", "tier 2", [], source=None, detail={"auth": "relogin"},
                                   error="login expired 2026-10-18 06:31 UTC; run `grok login`"), ok=False, soft=True)
        put("langfuse", {"details": {"version": "3.1", "projects": [
            {"project": "web", "traces_24h": 602, "cost_24h_usd": 2.1},
            {"project": "agents", "traces_24h": 14012, "cost_24h_usd": 31.5}]}})

    def test_fits_every_width_without_truncating(self):
        for width in (60, 90, 130):
            p, cw = build_panel(width)
            for line in p.lines:
                self.assertLessEqual(T.width(line), cw - 1, (width, T.strip(line)))
                self.assertNotIn("…", T.strip(line), width)
            for line in p.render(cw, T.ACCENT):
                self.assertLessEqual(T.width(line), cw)
            text = "\n".join(plain(p))
            for want in ("claude max20x", "codex pro", "cursor pro", "grok tier 2", "langfuse"):
                self.assertIn(want, text)

    def test_through_the_renderer(self):
        for width in (60, 90, 130):
            out = render.render(host(""), registry.Cache(), width=width, only=["ai"])
            self.assertIn("AI LIMITS", T.strip(out))
            for line in out.splitlines():
                self.assertLessEqual(T.width(line), width, (width, T.strip(line)))

    def test_rows_meters_extras_and_level(self):
        p, _ = build_panel(130)
        lines = plain(p)
        i = next(n for n, ln in enumerate(lines) if ln.startswith("claude"))
        self.assertRegex(lines[i], r"^claude max20x  5h \S+  38%  7d \S+  82% ↻2h1[34]m$")
        self.assertEqual(lines[i + 1].strip(), "7d Opus 64% · 7d Sonnet 21%")  # extra usage did not fit at this width
        codex = next(n for n, ln in enumerate(lines) if ln.startswith("codex"))
        self.assertEqual(lines[codex + 1].strip(), "review 7d 3% · 1.5k credits")
        cursor = next(n for n, ln in enumerate(lines) if ln.startswith("cursor"))
        self.assertTrue(lines[cursor].endswith("61% ↻" + time.strftime("%b %d", time.localtime(self.cursor_reset))), lines[cursor])
        self.assertEqual(lines[cursor + 1].strip(), "$12/$20 included · $4 bonus · $3 on-demand")
        self.assertIn("grok tier 2    login expired · run `grok login`", lines)
        self.assertTrue(lines[-1].startswith("langfuse       web 602 traces $2.10"))
        self.assertEqual((p.title, p.level, p.note), ("AI LIMITS", "warn", ""))
        self.assertIn("extra usage $12.40/$50.00", "\n".join(plain(build_panel(90)[0])))

    def test_hint_texts(self):
        h = ai_panel.hint
        self.assertEqual(h("claude", {"detail": {"auth": "expired"}}), "token expired · renews on the next `claude` run")
        self.assertEqual(h("codex", {"detail": {"auth": "relogin"}}), "login expired · run `codex login`")
        self.assertEqual(h("cursor", {"detail": {"auth": "none"}}), "not signed in · `cursor-agent login`")
        self.assertEqual(h("codex", {"detail": {"auth": "api_key"}}), "signed in with an API key · no plan limits")
        self.assertEqual(h("claude", {"error": "access token expired 2026-10-21; claude renews it on its next run"}),
                         "token expired · renews on the next `claude` run")  # a failed run leaves only its words
        self.assertEqual(h("grok", {"error": "HTTP 503:  upstream\nunavailable"}), "HTTP 503: upstream unavailable")
        self.assertEqual(h("claude", {"error": "not signed in to a Claude plan (no claudeAiOauth in "
                                               "~/.claude/.credentials.json or the Keychain); Keychain not readable "
                                               "in this session (rc 36: no GUI login)"}),
                         "Keychain not readable in this session")
        self.assertEqual(h("claude", {"error": "not signed in to a Claude plan (no claudeAiOauth in "
                                               "~/.claude/.credentials.json or the Keychain)"}),
                         "not signed in · `claude auth login`")
        self.assertEqual(h("cursor", {"detail": {"auth": "keychain", "keychain": "Keychain read timed out"}}),
                         "Keychain read timed out")

    def test_quiet_errors_hides_the_hint(self):
        lines = plain(build_panel(90, "[sources.grok_usage]\nquiet_errors = true\n")[0])
        self.assertFalse(any(ln.startswith("grok") for ln in lines))
        self.assertTrue(any(ln.startswith("claude") for ln in lines))

    def test_a_kept_reading_says_why_it_is_not_newer(self):
        rec = core.read_json(registry.src_path("claude_usage"))
        put("claude_usage", rec["data"], ok=False, soft=True, kept=True, last_ok=time.time() - 3 * 3600,
            error="access token expired 2026-10-21 10:00 UTC; claude renews it on its next run (not refreshing here)")
        p, _ = build_panel(90)
        lines = plain(p)
        i = next(n for n, ln in enumerate(lines) if ln.startswith("claude"))
        self.assertIn("82%", lines[i])  # the last good numbers stay
        self.assertEqual(lines[i + 2].strip(), "token expired · renews on the next `claude` run")
        self.assertEqual(p.note, "claude %s 3h" % T.g("crit"))
        put("claude_usage", rec["data"], ok=False, error="network: timeout")  # transient: the note is enough
        lines = plain(build_panel(90)[0])
        self.assertFalse(any("network" in ln for ln in lines))

    def test_a_window_that_has_reset_shows_no_number(self):
        now = time.time()
        put("codex_usage", rec_data("codex", "pro", [{"label": "5h", "used_pct": 99.0, "resets_at": now - 10},
                                                       {"label": "7d", "used_pct": 50.0, "resets_at": now + 7200}]))
        line = next(ln for ln in plain(build_panel(90)[0]) if ln.startswith("codex"))
        self.assertNotIn("99%", line)
        self.assertIn("—", line)
        self.assertRegex(line, r"↻(1h59m|2h)$")  # the soonest reset still ahead

    def test_odd_values_do_not_break_it(self):
        put("cursor_usage", rec_data("cursor", None, [{"label": "total", "used_pct": "88", "resets_at": "+3600"}, "junk",
                                                        {"used_pct": 12}], credits="nope", alerts=["junk", {"sev": "warn"}]))
        put("langfuse", {"details": {"projects": [1, None, {"project": "web", "traces_24h": "lots"}], "version": None}})
        for width in (60, 90, 130):
            p, cw = build_panel(width)
            lines = plain(p)
            self.assertTrue(any(ln.startswith("cursor") and "—" in ln for ln in lines))
            self.assertIn("langfuse       web 0 traces · 24h", lines)
            self.assertTrue(all(T.width(line) <= cw - 1 for line in p.lines))
        put("langfuse", {"details": "weird"})
        self.assertFalse(any(ln.startswith("langfuse") for ln in plain(build_panel(90)[0])))

    def test_nothing_to_show(self):
        support.clear_cache()
        self.assertIsNone(build_panel(90)[0])
        put("grok_usage", rec_data("grok", None, [], error="not signed in to grok.com", detail={"auth": "none"}), ok=False, soft=True)
        self.assertIsNone(build_panel(90, "[sources.grok_usage]\nquiet_errors = true\n")[0])
        self.assertEqual(plain(build_panel(90)[0]), ["grok           not signed in · `grok login`"])

    def test_plan_labels(self):
        L = ai_panel.plan_label
        self.assertEqual([L("claude", "max (default_claude_max_20x)"), L("claude", "max (default_claude_max_5x)"),
                          L("claude", "pro (default_claude_ai)"), L("cursor", "enterprise"), L("grok", "SuperGrok Heavy"),
                          L("grok", None), L("codex", "enterprise", 3)],
                         ["max20x", "max5x", "pro", "enterpr", "SuperGrok", "", "ent"])  # cut to the room the name leaves


class Demo(Sandbox):
    PATH = os.path.join(str(support.ROOT), "porchlight", "data", "demo", "ai.json")

    def resolve(self, v, now):
        """The demo file writes times as "+N" / "-N": seconds from the moment it loads."""
        if isinstance(v, str) and v[:1] in "+-" and v[1:].isdigit():
            return int(now) + int(v)
        if isinstance(v, dict):
            return {k: self.resolve(x, now) for k, x in v.items()}
        if isinstance(v, list):
            return [self.resolve(x, now) for x in v]
        return v

    def test_sample_records_render_and_hold_no_secrets(self):
        with open(self.PATH, encoding="utf-8") as f:
            raw = f.read()
        for marker in ("eyJ", "sk-", "Bearer"):
            self.assertNotIn(marker, raw)
        data = json.loads(raw)
        names = sorted(k for k in data if not k.startswith("_"))
        self.assertEqual(names, ["claude_usage", "codex_usage", "cursor_usage", "grok_usage"])
        for resolved in (True, False):  # unresolved, the times are simply unknown: no crash, no stale reset
            support.clear_cache()
            for name in names:
                d = self.resolve(data[name], time.time()) if resolved else data[name]
                self.assertEqual(set(d) - {"alerts"}, {"provider", "plan", "windows", "credits", "source", "as_of",
                                                       "latency_ms", "error", "detail"})
                put(name, d)
            p, cw = build_panel(90)
            text = "\n".join(plain(p))
            for name in ("claude max5x", "codex plus", "cursor pro", "grok SuperGrok"):
                self.assertIn(name, text)
            self.assertEqual(p.level, "warn")
            self.assertEqual("↻" in text, resolved)
            self.assertTrue(all(T.width(line) <= cw - 1 for line in p.lines))


class Cheap(unittest.TestCase):
    def test_the_login_render_never_imports_the_probe(self):
        code = ("import sys; sys.path.insert(0, %r); from porchlight import sources, panels; "
                "print('porchlight.sources.ai_probe' in sys.modules, 'sqlite3' in sys.modules)" % str(support.ROOT))
        p = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.stdout.split(), ["False", "False"], p.stderr)


class Helpers(unittest.TestCase):
    def test_epoch(self):
        e = ai_probe._epoch
        for v in ("2030-01-01T05:00:00Z", "2030-01-01T05:00:00.1234567Z", "2030-01-01T10:30:00+05:30",
                  "2030-01-01 05:00:00", FAR * 1000, str(FAR * 1000), "1893474000.5", FAR):
            self.assertEqual(e(v), FAR, v)
        for bad in (None, "", "soon", True, "+3600"):
            self.assertIsNone(e(bad), bad)

    def test_window_labels(self):
        self.assertEqual([ai_probe._dur(s) for s in (18000, 604800, 2592000, 5400, 0, None)], ["5h", "7d", "30d", "90m", "?", "?"])

    def test_jwt_claims(self):
        self.assertEqual(ai_probe._jwt_claims(jwt({"exp": 5}, "sig")), {"exp": 5})
        for bad in ("", "a.b", None, "x.!!!.y", jwt([1], "s")):
            self.assertEqual(ai_probe._jwt_claims(bad), {}, bad)

    def test_tilde(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/someone"}):
            self.assertEqual(ai_probe._tilde("/home/someone/.codex/auth.json"), "~/.codex/auth.json")
            self.assertEqual(ai_probe._tilde("/home/someone-else/x"), "/home/someone-else/x")


if __name__ == "__main__":
    unittest.main()

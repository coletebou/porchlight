"""SaaS and cloud sources (sources/saas.py) and their panels (panels/cloud.py).

Offline: every API is a fake routed by (method, URL), except the transport
tests, which talk to a loopback HTTP server. The payload shapes follow real
responses from each API.
"""
import calendar
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import support  # noqa: F401 — must be first
from support import ctx, host, put

from porchlight import collect, core, registry, render, theme as T
from porchlight.panels import cloud  # noqa: F401 — registers the panels
from porchlight.sources import saas as saas_core, saas_accounts
from porchlight.ui import BUILDERS, RCtx


class _Either:
    """sources/saas.py and sources/saas_accounts.py read as one namespace (the
    module was split); patches go to the real module that owns the name."""

    def __getattr__(self, name):
        return getattr(saas_core, name) if hasattr(saas_core, name) else getattr(saas_accounts, name)


saas = _Either()

NOW = float(calendar.timegm((2026, 10, 16, 12, 0, 0, 0, 0, 0)))
CANARY = "CANARYSECRETVALUE0123456789abcdefXYZ"  # 36 chars: long enough for the token mask too
SOURCES = ("cloudflare", "vercel", "uptimerobot", "hetzner", "github", "sentry", "stripe", "resend", "langfuse",
           "ai_spend", "aws_account", "twilio")
SAAS_PY = os.path.join(os.path.dirname(os.path.abspath(saas_core.__file__)), "saas.py")
DEMO = os.path.join(support.ROOT, "porchlight", "data", "demo", "saas.json")


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def ago(**kw):
    return iso(NOW - sum(v * {"minutes": 60, "hours": 3600, "days": 86400}[k] for k, v in kw.items()))


class Router:
    """A fake ctx.http routing (METHOD, URL without its query) to (status, body),
    a callable(req) -> (status, body), or an exception to raise. Every call is
    recorded with its query parsed."""

    def __init__(self, routes):
        self.routes, self.calls, self.unexpected = dict(routes), [], []

    def __call__(self, url, method="GET", headers=None, data=None, json_body=None, basic=None, timeout=None,
                 redirects=None, **kw):
        base, _, query = url.partition("?")
        params = {k: (v[0] if len(v) == 1 else v) for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        req = {"method": method, "url": base, "full": url, "params": params, "headers": dict(headers or {}), "form": data,
               "json": json_body, "basic": basic, "timeout": timeout, "redirects": redirects}
        self.calls.append(req)
        h = self.routes.get((method, base))
        if h is None:
            self.unexpected.append("%s %s" % (method, base))
            raise ConnectionRefusedError("no fake route")
        if isinstance(h, Exception):
            raise h
        status, body = h(req) if callable(h) else h
        return core.Resp(status, body, 5, {})


def assert_shape(tc, res):
    tc.assertTrue({"status", "summary", "details", "error"} <= set(res) <= {"status", "summary", "details", "error", "alerts"}, res)
    tc.assertIn(res["status"], ("ok", "warn", "crit", "unknown"))
    tc.assertIsInstance(res["summary"], str)
    tc.assertLessEqual(len(res["summary"]), 60, res["summary"])
    tc.assertIsInstance(res["details"], dict)
    tc.assertTrue(res["error"] is None or isinstance(res["error"], str))
    for a in res.get("alerts") or []:
        tc.assertIn(a["sev"], ("crit", "warn", "info"))
        tc.assertTrue(a["msg"])
    if res["status"] in ("warn", "crit"):
        tc.assertTrue(res.get("alerts"), "warn/crit must raise an alert: %s" % res)
    json.dumps(res)  # JSON-safe


class Base(unittest.TestCase):
    def run_src(self, name, env, routes, toml="", now=NOW, run=None):
        """Run one registered source through its real entry point against fake HTTP."""
        h = host(toml)
        router = Router(routes)
        c = ctx(h, registry.SOURCES[name].config(h), env, None, router)
        if run is not None:
            c.run = run
        with mock.patch.object(saas_core, "_now", lambda: now):
            res = registry.SOURCES[name].fn(c)
        assert_shape(self, res)
        self.assertEqual(router.unexpected, [], "unexpected requests")
        for call in router.calls:
            self.assertIs(call["redirects"], False, call["full"])   # a redirect is never followed
            self.assertNotIn(CANARY, call["full"])                  # no credential in any URL
            self.assertTrue(call["timeout"] and call["timeout"] <= saas.REQ_TIMEOUT)
        self.assertNotIn(CANARY, json.dumps(res))
        return res, router


# ── Transport, against a loopback server ────────────────────────────────────
class Loopback:
    def __enter__(self):
        hits = self.hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body, ctype="application/json", extra=None):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                hits.append((self.command, self.path, self.headers.get("Authorization")))
                if self.path.startswith("/ok"):
                    self.reply(200, {"a": 1})
                elif self.path.startswith("/401"):
                    self.reply(401, {"message": "bad token " + CANARY})
                elif self.path.startswith("/redir"):
                    self.reply(302, b"", extra={"Location": "/ok"})
                elif self.path.startswith("/text"):
                    self.reply(200, b"not json", ctype="text/plain")
                elif self.path.startswith("/slow"):
                    time.sleep(1.5)
                    self.reply(200, {"slow": True})
                elif self.path.startswith("/echo"):
                    self.reply(200, {"method": self.command, "path": self.path, "body": body,
                                     "auth": self.headers.get("Authorization"), "ctype": self.headers.get("Content-Type"),
                                     "ua": self.headers.get("User-Agent"), "x": self.headers.get("X-Test")})
                else:
                    self.reply(404, {"message": "nope"})
            do_GET = do_POST = handle_any

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


class Transport(unittest.TestCase):
    def setUp(self):
        clean = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        clean.update(no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost")
        p = mock.patch.dict(os.environ, clean, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.c = ctx(host())  # the real core.http

    def test_get_query_headers_and_user_agent(self):
        with Loopback() as s:
            j = saas._get(self.c, s.url("/echo"), {"X-Test": "1"}, {"q": ["a", "b"], "z": 1})
            self.assertEqual((j["method"], j["x"]), ("GET", "1"))
            self.assertIn("q=a&q=b&z=1", j["path"])
            self.assertTrue(j["ua"].startswith("porchlight/"))
            self.assertEqual(saas._get(self.c, s.url("/ok")), {"a": 1})

    def test_basic_auth_form_and_json_post(self):
        import base64
        with Loopback() as s:
            j = saas._req(self.c, "GET", s.url("/echo"), basic=("user", "pw")).body
            self.assertEqual(j["auth"], "Basic " + base64.b64encode(b"user:pw").decode())
            j = saas._req(self.c, "POST", s.url("/echo"), form={"api_key": "k", "format": "json"}).body
            self.assertEqual((j["method"], j["body"], j["ctype"]), ("POST", "api_key=k&format=json", "application/x-www-form-urlencoded"))
            j = saas._req(self.c, "POST", s.url("/echo"), body={"query": "{x}"}).body
            self.assertEqual((json.loads(j["body"]), j["ctype"]), ({"query": "{x}"}, "application/json"))

    def test_http_errors_come_back_and_their_text_is_scrubbed(self):
        with Loopback() as s:
            r = saas._req(self.c, "GET", s.url("/401"))
            self.assertEqual((r.status, r.ok), (401, False))
            with self.assertRaises(saas.ProbeError) as cm:
                saas._get(self.c, s.url("/401"), label="svc ")
            self.assertEqual(str(cm.exception), "svc HTTP 401: bad token <redacted>")
            with self.assertRaises(saas.ProbeError) as cm:
                saas._get(self.c, s.url("/text"))
            self.assertIn("non-JSON", str(cm.exception))
            with self.assertRaises(saas.ProbeError) as cm:
                saas._get(self.c, s.url("/ok"), expect=list)
            self.assertIn("unexpected response shape", str(cm.exception))

    def test_redirects_are_never_followed_with_or_without_credentials(self):
        with Loopback() as s:
            self.assertEqual(saas._req(self.c, "GET", s.url("/redir"), {"Authorization": "Bearer " + CANARY}).status, 302)
            self.assertEqual(saas._req(self.c, "GET", s.url("/redir")).status, 302)  # core.http would follow this one
            self.assertEqual([p for _m, p, _a in s.hits], ["/redir", "/redir"])  # /ok never fetched

    def test_timeout_and_refused(self):
        with Loopback() as s:
            t0 = time.monotonic()
            with self.assertRaises(saas.ProbeError) as cm:
                saas._req(self.c, "GET", s.url("/slow"), timeout=0.3)
            self.assertEqual(str(cm.exception), "timeout")
            self.assertLess(time.monotonic() - t0, 1.2)
            port = s.port
        with self.assertRaises(saas.ProbeError) as cm:  # the server is gone: nothing listens
            saas._req(self.c, "GET", "http://127.0.0.1:%d/ok" % port, timeout=1)
        self.assertIn(str(cm.exception), ("refused", "reset", "no route"))

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x"):
            with self.assertRaises(saas.ProbeError) as cm:
                saas._req(self.c, "GET", url)
            self.assertEqual(str(cm.exception), "unsupported URL scheme")


# ── Helpers ─────────────────────────────────────────────────────────────────
class Helpers(unittest.TestCase):
    def test_timestamps(self):
        p = saas._ts
        self.assertEqual(p("2026-10-16T09:40:00.1Z"), calendar.timegm((2026, 10, 16, 9, 40, 0)) + 0.1)
        self.assertEqual(p("2026-10-16 09:12:33.123456+00"), calendar.timegm((2026, 10, 16, 9, 12, 33)) + 0.123456)
        self.assertEqual(p("2026-10-16T09:40:00+05:30"), calendar.timegm((2026, 10, 16, 4, 10, 0)))
        self.assertEqual(p("2026-10-16T09:40:00-0400"), calendar.timegm((2026, 10, 16, 13, 40, 0)))
        self.assertEqual(p("2026-06-01T00:00:00.123456789Z"), calendar.timegm((2026, 6, 1, 0, 0, 0)) + 0.123456)
        for bad in ("0001-01-01T00:00:00Z", "garbage", None, 5, "", "2026-13-45T25:61:61Z", "2026-02-30T00:00:00Z"):
            self.assertIsNone(p(bad), bad)

    def test_scrub_masks_key_like_text_and_caps_length(self):
        out = saas._scrub("cannot be accessed with management key xai-...54nd because; token " + CANARY + " end")
        self.assertNotIn(CANARY, out)
        self.assertNotIn("54nd", out)
        self.assertLessEqual(len(saas._scrub("x " * 200)), 90)
        self.assertEqual(saas._scrub("  a\n\tb  "), "a b")

    def test_clip_worst_normalize(self):
        self.assertEqual(len(saas._clip("x" * 100)), 60)
        self.assertTrue(saas._clip("x" * 100).endswith("..."))
        self.assertEqual(saas._worst(["ok", "warn", "unknown"]), "warn")
        self.assertEqual(saas._worst(["ok", "crit", "warn"]), "crit")
        self.assertEqual(saas._worst(["unknown", "unknown"]), "unknown")
        self.assertEqual(saas._worst([]), "unknown")
        n = saas._normalize({"status": "bogus", "summary": "s" * 99, "details": None, "error": 5,
                             "alerts": [{"sev": "loud", "msg": "m"}, {"msg": ""}, "junk"]})
        self.assertEqual((n["status"], len(n["summary"]), n["details"], n["error"]), ("unknown", 60, {}, "5"))
        self.assertEqual(n["alerts"], [{"sev": "warn", "msg": "m"}])
        self.assertEqual(saas._normalize("garbage")["status"], "unknown")

    def test_pmap_never_raises(self):
        def boom():
            raise ValueError("x " + CANARY)
        out = saas._pmap({"a": lambda: 1, "b": boom, "c": lambda: (_ for _ in ()).throw(saas.ProbeError("net"))})
        self.assertEqual(out["a"], (1, None))
        self.assertEqual(out["c"], (None, "net"))
        self.assertTrue(out["b"][1].startswith("ValueError"))
        self.assertNotIn(CANARY, out["b"][1])
        slow = saas._pmap({"s": lambda: time.sleep(1) or 1}, timeout=0.1)
        self.assertEqual(slow["s"], (None, "timeout"))

    def test_settings_helpers(self):
        self.assertEqual(saas._strs("a, b,,c"), ["a", "b", "c"])
        self.assertEqual(saas._strs(["x", " ", 3]), ["x", "3"])
        self.assertEqual(saas._strs(None), [])
        self.assertTrue(saas._ignored("Legacy Status Page", ["status"]))
        self.assertFalse(saas._ignored("api", []))
        self.assertEqual(saas._num({"a": "7.5", "b": "x"}, "a", 1), 7.5)
        self.assertEqual(saas._num({"b": "x"}, "b", 3), 3.0)
        self.assertEqual(saas._month_start(NOW), calendar.timegm((2026, 10, 1, 0, 0, 0)))

    def test_finish_scrubs_credentials_and_always_alerts(self):
        c = ctx(host(), env={"ACME_API_TOKEN": CANARY, "PATH": "/usr/bin:/bin", "MODE": "subscription"})
        saas._cred(c, "gho_fromclitoken123")
        res = saas._finish(c, "svc", {"status": "warn", "summary": "saw " + CANARY, "error": None,
                                      "details": {"k": "gho_fromclitoken123", CANARY: ["x" + CANARY], "p": "/usr/bin:/bin subscription"}})
        text = json.dumps(res)
        self.assertNotIn(CANARY, text)
        self.assertNotIn("gho_fromclitoken123", text)
        self.assertIn("/usr/bin:/bin subscription", text)  # non-secret values and short plain words survive
        self.assertEqual(res["alerts"], [{"sev": "warn", "msg": "svc: saw <redacted>"}])
        quiet = saas._finish(c, "svc", saas._fail("token expired"))
        self.assertNotIn("alerts", quiet)
        loud = saas._finish(c, "svc", saas._fail("token expired"), alert_unknown=True)
        self.assertEqual(loud["alerts"], [{"sev": "warn", "msg": "svc: token expired"}])

    def test_guard_turns_a_bug_into_a_soft_failure(self):
        def broken(c):
            raise KeyError("boom " + CANARY)
        res = saas._guard(ctx(host()), "svc", broken)
        self.assertEqual((res["status"], res["summary"]), ("unknown", "internal error"))
        self.assertTrue(res["error"].startswith("KeyError"))
        self.assertNotIn(CANARY, json.dumps(res))
        self.assertTrue(collect._soft_failure(res))  # so the collector keeps the last good reading


# ── Cloudflare ──────────────────────────────────────────────────────────────
CF = saas.CF
Z1, Z2, Z3 = "a" * 32, "b" * 32, "d" * 32
ACCT = "c" * 32
CF_KEY_ENV = {"CF_EMAIL": "ops@example.com", "CF_GLOBAL_API_KEY": CANARY, "CLOUDFLARE_ACCOUNT_ID": ACCT, "CF_ZONE_ID": Z1}


def cf_zone(zid, name, status="active", acct=ACCT):
    return {"id": zid, "name": name, "status": status, "account": {"id": acct, "name": "Acme"}}


def healthy(n):
    return [{"name": "t%d" % i, "status": "healthy"} for i in range(n)]


def cf_routes(tunnels=None, zones=None, traffic=None, email=None, gql_error=False):
    zones = zones if zones is not None else [cf_zone(Z1, "example.com")]
    tunnels = tunnels if tunnels is not None else healthy(3)
    traffic = traffic if traffic is not None else {Z1: (10000, 20)}
    email = dict({z["id"]: {"enabled": False, "status": "unconfigured"} for z in zones},
                 **(email if email is not None else {Z1: {"enabled": True, "status": "ready", "synced": True}}))

    def gql(req):
        if gql_error:
            return 200, {"data": None, "errors": [{"message": "query failed " + CANARY}]}
        q, tags = req["json"]["query"], req["json"]["variables"]["z"]
        assert len(tags) <= 10, "more than ten zones in one query"
        if "httpRequests1hGroups" in q:
            nodes = []
            for t in tags:
                n, e5 = traffic.get(t, (0, 0))
                sm = [{"edgeResponseStatus": 200, "requests": n - e5}, {"edgeResponseStatus": 502, "requests": e5}]
                nodes.append({"zoneTag": t, "httpRequests1hGroups": [
                    {"sum": {"requests": n, "cachedRequests": 8, "threats": 3, "responseStatusMap": sm}, "uniq": {"uniques": 42}}]})
            return 200, {"data": {"viewer": {"zones": nodes}}, "errors": None}
        return 200, {"data": {"viewer": {"zones": [{"zoneTag": t, "emailRoutingAdaptiveGroups": [
            {"count": 5, "dimensions": {"status": "delivered"}}, {"count": 1, "dimensions": {"status": "dropped"}}]}
            for t in tags if email.get(t, {}).get("enabled")]}}, "errors": None}

    r = {("GET", CF + "/zones"): (200, {"success": True, "result": zones}),
         ("GET", CF + "/accounts/%s/cfd_tunnel" % ACCT): (200, {"success": True, "result": tunnels}),
         ("POST", CF + "/graphql"): gql}
    for zid, em in email.items():
        r[("GET", CF + "/zones/%s/email/routing" % zid)] = (200, {"success": True, "result": em})
    return r


class Cloudflare(Base):
    def test_healthy_ignores_retired_tunnels_and_uses_the_global_key_in_headers(self):
        tun = healthy(3) + [{"name": "old%d" % i, "status": "down", "conns_inactive_at": ago(days=60)} for i in range(2)]
        res, rt = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(tunnels=tun))
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["summary"], "3/3 tunnels up, 10k req/24h, 20 5xx (0.2%)")
        tu = res["details"]["tunnels"]
        self.assertEqual((tu["healthy"], tu["stale_down"], tu["down_recent"]), (3, 2, []))
        self.assertEqual(res["details"]["traffic_24h"]["cached_pct"], 0.1)
        self.assertEqual(res["details"]["email_routing"]["last_24h"], {"delivered": 5, "dropped": 1})
        hdr = rt.calls[0]["headers"]
        self.assertEqual((hdr["X-Auth-Email"], hdr["X-Auth-Key"]), ("ops@example.com", CANARY))
        self.assertTrue(all(c["url"].startswith("https://api.cloudflare.com/") for c in rt.calls))
        self.assertNotIn("ops@example.com", json.dumps(res))  # the login email is a credential too

    def test_api_token_is_preferred_and_the_account_comes_from_the_zones(self):
        env = {"CLOUDFLARE_API_TOKEN": CANARY, "CF_EMAIL": "ops@example.com", "CF_GLOBAL_API_KEY": "other-key-value-1234"}
        res, rt = self.run_src("cloudflare", env, cf_routes())
        self.assertEqual(res["status"], "ok", res)
        self.assertTrue(all(c["headers"] == {"Authorization": "Bearer " + CANARY} for c in rt.calls))
        self.assertIn(("GET", CF + "/accounts/%s/cfd_tunnel" % ACCT), [(c["method"], c["url"]) for c in rt.calls])
        self.assertEqual(res["details"]["auth"], "token")

    def test_two_accounts_and_no_account_id_means_no_tunnel_call(self):
        zones = [cf_zone(Z1, "example.com"), cf_zone(Z2, "example.org", acct="e" * 32)]
        res, rt = self.run_src("cloudflare", {"CLOUDFLARE_API_TOKEN": CANARY}, cf_routes(zones=zones))
        self.assertNotIn("tunnels", res["details"])
        self.assertFalse(any("cfd_tunnel" in c["url"] for c in rt.calls))

    def test_recent_down_is_crit_degraded_is_warn_ignored_is_skipped(self):
        tun = [{"name": "ok", "status": "healthy"}, {"name": "fresh", "status": "down", "conns_inactive_at": ago(hours=3)}]
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(tunnels=tun))
        self.assertEqual((res["status"], res["details"]["tunnels"]["down_recent"]), ("crit", ["fresh"]))
        self.assertEqual(res["alerts"][0], {"sev": "crit", "msg": "cloudflare: tunnel fresh down"})
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(tunnels=[{"name": "d", "status": "degraded"}]))
        self.assertEqual(res["status"], "warn")
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(tunnels=tun), '[sources.cloudflare]\nignore = ["FRESH"]')
        self.assertEqual(res["status"], "ok")
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(tunnels=tun), "[sources.cloudflare]\ntunnel_recent_days = 0.1")
        self.assertEqual((res["status"], res["details"]["tunnels"]["stale_down"]), ("ok", 1))  # 3h > 2.4h: retired

    def test_5xx_thresholds_and_their_settings(self):
        for req, e5, want in ((10000, 20, "ok"), (10000, 100, "warn"), (10000, 500, "crit"), (10, 10, "ok"), (30, 30, "warn")):
            res, _ = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(traffic={Z1: (req, e5)}))
            self.assertEqual(res["status"], want, (req, e5, res["summary"]))
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(traffic={Z1: (10000, 100)}),
                              "[sources.cloudflare]\nwarn_5xx_pct = 2\ncrit_5xx_pct = 10")
        self.assertEqual(res["status"], "ok")

    def test_each_zone_is_judged_on_its_own(self):
        zones = [cf_zone(Z1, "example.com"), cf_zone(Z2, "example.org")]
        res, _ = self.run_src("cloudflare", {"CLOUDFLARE_API_TOKEN": CANARY},
                              cf_routes(zones=zones, traffic={Z1: (100000, 10), Z2: (400, 40)}),
                              '[sources.cloudflare]\nzones = ["example.com", "example.org"]')
        self.assertEqual(res["status"], "crit")  # 10% on the small zone, 0.05% overall
        self.assertIn("cloudflare: 5xx rate 10.0% on example.org", [a["msg"] for a in res["alerts"]])
        tz = res["details"]["traffic_zones"]
        self.assertEqual((tz[0]["zone"], tz[0]["level"]), ("example.org", "crit"))  # flagged zones first
        self.assertEqual(res["details"]["traffic_24h"]["requests"], 100400)

    def test_auto_mode_chunks_graphql_by_ten_zones_and_honours_max_zones(self):
        zones = [cf_zone("%032x" % i, "z%d.example.com" % i) for i in range(1, 24)]
        res, rt = self.run_src("cloudflare", {"CLOUDFLARE_API_TOKEN": CANARY},
                               cf_routes(zones=zones, traffic={z["id"]: (100, 0) for z in zones}, email={}))
        gql = [c for c in rt.calls if c["url"].endswith("/graphql")]
        self.assertEqual(len(gql), 6)  # 23 zones → 3 chunks, for traffic and for email
        self.assertEqual(res["details"]["traffic_24h"]["zones"], 23)
        self.assertNotIn("email_routing", res["details"])  # nobody set it up: not a problem
        res, rt = self.run_src("cloudflare", {"CLOUDFLARE_API_TOKEN": CANARY},
                               cf_routes(zones=zones, traffic={}, email={}), "[sources.cloudflare]\nmax_zones = 4")
        self.assertEqual(res["details"]["zones"]["judged"], 4)
        self.assertEqual(sum(1 for c in rt.calls if "/email/routing" in c["url"]), 4)

    def test_zone_names_resolve_and_unknown_names_warn(self):
        zones = [cf_zone(Z1, "example.com"), cf_zone(Z2, "example.org")]
        res, rt = self.run_src("cloudflare", {"CLOUDFLARE_API_TOKEN": CANARY}, cf_routes(zones=zones),
                               '[sources.cloudflare]\nzones = ["EXAMPLE.org", "' + Z1 + '", "nope.example"]')
        self.assertEqual(res["status"], "warn")
        self.assertIn("cloudflare: zone nope.example not found", [a["msg"] for a in res["alerts"]])
        tags = [t for c in rt.calls if c["url"].endswith("/graphql") for t in c["json"]["variables"]["z"]]
        self.assertEqual(sorted(set(tags)), sorted([Z1, Z2]))

    def test_inactive_zone_and_email_routing_states(self):
        zones = [cf_zone(Z1, "example.com"), cf_zone(Z2, "example.org", "pending")]
        res, _ = self.run_src("cloudflare", {"CLOUDFLARE_API_TOKEN": CANARY, "CF_ZONE_ID": Z1}, cf_routes(zones=zones))
        self.assertEqual((res["status"], res["details"]["zones"]["not_active"]), ("ok", []))  # explicit zone: only it is judged
        res, _ = self.run_src("cloudflare", {"CLOUDFLARE_API_TOKEN": CANARY}, cf_routes(zones=zones))
        self.assertEqual((res["status"], res["details"]["zones"]["not_active"]), ("warn", ["example.org"]))
        broken = {Z1: {"enabled": True, "status": "misconfigured"}}
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(email=broken))
        self.assertEqual(res["alerts"], [{"sev": "warn", "msg": "cloudflare: email routing misconfigured on example.com"}])
        for em, toml, want in (({"enabled": True, "status": "unlocked"}, "", "ok"),
                               ({"enabled": False, "status": "unconfigured"}, "", "ok"),
                               ({"enabled": False, "status": "disabled"}, '[sources.cloudflare]\nemail_routing = "required"', "warn")):
            res, _ = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(email={Z1: em}), toml)
            self.assertEqual(res["status"], want, em)
        res, rt = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(), "[sources.cloudflare]\nemail_routing = false")
        self.assertFalse(any("email" in c["url"] or "emailRouting" in str(c["json"]) for c in rt.calls))

    def test_partial_and_total_failure_never_leak(self):
        r = cf_routes()
        r[("GET", CF + "/accounts/%s/cfd_tunnel" % ACCT)] = (403, {"success": False, "errors": [{"message": "Invalid key " + CANARY}]})
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, r)
        self.assertEqual(res["status"], "warn")
        self.assertIn("tunnels", res["details"]["partial_errors"])
        r = cf_routes()
        r[("GET", CF + "/zones/%s/email/routing" % Z1)] = (500, {})
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, r)
        self.assertEqual(res["status"], "ok")  # a missing email routing reading is not an outage
        dead = {k: TimeoutError("timed out") for k in cf_routes()}
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, dead)
        self.assertEqual((res["status"], res["summary"]), ("unknown", "all Cloudflare calls failed"))
        self.assertIn("timeout", res["error"])

    def test_graphql_errors_and_missing_credentials(self):
        res, _ = self.run_src("cloudflare", CF_KEY_ENV, cf_routes(gql_error=True))
        self.assertEqual(res["status"], "warn")  # tunnels and zones are fine; the traffic query failed
        self.assertIn("traffic", res["summary"] + json.dumps(res["details"]["partial_errors"]))
        res, rt = self.run_src("cloudflare", {"CF_EMAIL": "ops@example.com"}, {})
        self.assertEqual((res["status"], res["error"]), ("unknown", "missing env: CLOUDFLARE_API_TOKEN (or CF_EMAIL + CF_GLOBAL_API_KEY)"))
        self.assertEqual(rt.calls, [])
        self.assertTrue(collect._soft_failure(res))


# ── Vercel ──────────────────────────────────────────────────────────────────
VP = saas.VERCEL + "/v9/projects"


def vproj(name, state, **age):
    prod = {"readyState": state, "createdAt": int((NOW - sum(v * {"minutes": 60, "hours": 3600, "days": 86400}[k]
                                                             for k, v in age.items())) * 1000)} if state else {}
    return {"name": name, "targets": {"production": prod} if prod else {}, "env": [{"key": "SECRET", "value": CANARY}]}


class Vercel(Base):
    def go(self, projects, env=None, page2=None, toml=""):
        pages = [(200, {"projects": projects, "pagination": {"count": len(projects), "next": 123 if page2 else None}})]
        if page2:
            pages.append((200, {"projects": page2, "pagination": {"count": len(page2), "next": None}}))
        it = iter(pages)
        return self.run_src("vercel", env or {"VERCEL_TOKEN": CANARY}, {("GET", VP): lambda req: next(it)}, toml)

    def test_ok_with_canceled_and_building(self):
        res, rt = self.go([vproj("a", "READY", hours=2), vproj("b", "CANCELED", minutes=5), vproj("c", "BUILDING", minutes=1),
                           vproj("d", None)])
        self.assertEqual((res["status"], res["summary"]), ("ok", "4 projects, prod ok (1 READY, 1 building)"))
        self.assertEqual(res["details"]["prod_states"], {"READY": 1, "CANCELED": 1, "BUILDING": 1, "NONE": 1})
        self.assertEqual(rt.calls[0]["headers"]["Authorization"], "Bearer " + CANARY)
        self.assertNotIn("teamId", rt.calls[0]["params"])

    def test_error_age_decides_severity(self):
        res, _ = self.go([vproj("api", "ERROR", hours=3)])
        self.assertEqual((res["status"], res["summary"]), ("crit", "prod deploy ERROR: api"))
        res, _ = self.go([vproj("api", "ERROR", days=3)])
        self.assertEqual((res["status"], res["summary"]), ("warn", "prod deploy ERROR (<7d): api"))
        res, _ = self.go([vproj("apps", "ERROR", days=200)])
        self.assertEqual((res["status"], res["details"]["stale_errors"]), ("ok", ["apps"]))
        res, _ = self.go([vproj("api", "ERROR", hours=3)], toml="[sources.vercel]\ncrit_hours = 1")
        self.assertEqual(res["status"], "warn")

    def test_project_filters_pagination_team_and_failure(self):
        res, _ = self.go([vproj("api", "ERROR", hours=1), vproj("web", "READY", hours=1)], toml='[sources.vercel]\nignore = ["AP"]')
        self.assertEqual((res["status"], res["details"]["projects"]), ("ok", 1))
        res, _ = self.go([vproj("api", "ERROR", hours=1), vproj("web", "READY", hours=1)], toml='[sources.vercel]\nprojects = ["web"]')
        self.assertEqual(res["details"]["prod_by_project"], {"web": "READY"})
        res, rt = self.go([vproj("a", "READY", days=1)], env={"VERCEL_TOKEN": CANARY, "VERCEL_TEAM_ID": "team_x"},
                          page2=[vproj("b", "READY", days=1)])
        self.assertEqual(res["details"]["projects"], 2)
        self.assertEqual((rt.calls[0]["params"]["teamId"], rt.calls[1]["params"]["until"]), ("team_x", "123"))
        res, _ = self.go([vproj("a", "READY", days=1)], toml='[sources.vercel]\nteam_id = "team_cfg"')
        res, _ = self.run_src("vercel", {"VERCEL_TOKEN": CANARY}, {("GET", VP): (401, {"error": {"message": "Not authorized"}})})
        self.assertEqual((res["status"], res["error"]), ("unknown", "vercel HTTP 401: Not authorized"))
        self.assertEqual(self.run_src("vercel", {}, {})[0]["error"], "missing env: VERCEL_TOKEN")


# ── UptimeRobot ─────────────────────────────────────────────────────────────
V3, V2 = saas.UR + "/v3/monitors", saas.UR + "/v2/getMonitors"


def m3(name, status, dur=100):
    return {"friendlyName": name, "status": status, "currentStateDuration": dur, "httpPassword": CANARY, "apiKey": CANARY}


class UptimeRobot(Base):
    ENV = {"UPTIMEROBOT_API_KEY": CANARY}

    def test_v3_all_up_unescapes_names_and_never_copies_secret_fields(self):
        res, rt = self.run_src("uptimerobot", self.ENV, {("GET", V3): (200, {"data": [m3("a &#40;x&#41;", "UP"), m3("b", "UP")]})})
        self.assertEqual((res["status"], res["summary"]), ("ok", "2/2 monitors up"))
        self.assertEqual(res["details"]["states"], {"a (x)": "up", "b": "up"})
        self.assertEqual(rt.calls[0]["headers"]["Authorization"], "Bearer " + CANARY)

    def test_down_duration_decides_severity(self):
        res, _ = self.run_src("uptimerobot", self.ENV, {("GET", V3): (200, {"data": [m3("up", "UP"), m3("Old &#40;box&#41;", "DOWN", 40 * 86400)]})})
        self.assertEqual((res["status"], res["summary"]), ("warn", "1/2 up; down: Old (box) 40d"))
        res, _ = self.run_src("uptimerobot", self.ENV, {("GET", V3): (200, {"data": [m3("api", "DOWN", 3600), m3("p", "PAUSED")]})})
        self.assertEqual((res["status"], res["details"]["paused"]), ("crit", 1))
        self.assertEqual(res["alerts"], [{"sev": "crit", "msg": "uptime: api down 0d"}])
        res, _ = self.run_src("uptimerobot", self.ENV, {("GET", V3): (200, {"data": [m3("x", "DOWN", 3 * 86400)]})},
                              "[sources.uptimerobot]\nrecent_days = 2")
        self.assertEqual(res["status"], "warn")

    def test_ignore_list(self):
        res, _ = self.run_src("uptimerobot", self.ENV, {("GET", V3): (200, {"data": [m3("up", "UP"), m3("Legacy Box", "DOWN", 9e6)]})},
                              '[sources.uptimerobot]\nignore = ["legacy", "other"]')
        self.assertEqual((res["status"], res["details"]["ignored"], res["details"]["total"]), ("ok", 1, 1))

    def test_v2_fallback_when_v3_is_rejected(self):
        def v2(req):
            self.assertEqual((req["form"]["api_key"], req["method"]), (CANARY, "POST"))  # in the body, never the URL
            return 200, {"stat": "ok", "pagination": {"offset": 0, "limit": 50, "total": 3},
                         "monitors": [{"friendly_name": "a", "status": 2}, {"friendly_name": "b", "status": 9},
                                      {"friendly_name": "c", "status": 0}]}
        res, _ = self.run_src("uptimerobot", self.ENV, {("GET", V3): (401, {"message": "bad"}), ("POST", V2): v2})
        self.assertEqual((res["status"], res["details"]["api"], res["details"]["v3_error"]), ("crit", "v2", "v3 HTTP 401: bad"))
        res, _ = self.run_src("uptimerobot", self.ENV, {("GET", V3): (500, {}), ("POST", V2): (200, {"stat": "fail"})})
        self.assertEqual(res["status"], "unknown")
        self.assertIn("v3:", res["error"])

    def test_next_page_is_only_followed_on_its_own_host(self):
        pages = iter([(200, {"data": [m3("a", "UP")], "nextLink": "https://evil.example/steal"}), (200, {"data": [m3("b", "UP")]})])
        res, rt = self.run_src("uptimerobot", self.ENV, {("GET", V3): lambda req: next(pages)})
        self.assertEqual(res["details"]["total"], 2)
        self.assertTrue(all(c["url"] == V3 for c in rt.calls))  # the other host is never contacted
        self.assertEqual(rt.calls[1]["params"]["cursor"], "https://evil.example/steal")
        pages = iter([(200, {"data": [m3("a", "UP")], "nextLink": V3 + "?cursor=2"}), (200, {"data": [m3("b", "UP")]})])
        res, rt = self.run_src("uptimerobot", self.ENV, {("GET", V3): lambda req: next(pages)})
        self.assertEqual(rt.calls[1]["full"], V3 + "?cursor=2")


# ── Hetzner ─────────────────────────────────────────────────────────────────
HC, HZ = saas.HCLOUD, saas.HETZNER


def hz_server(name, status="running", out_gb=100, backups=False):
    return {"name": name, "status": status, "location": {"name": "ash"}, "included_traffic": 4398046511104,
            "outgoing_traffic": out_gb * 10 ** 9, "backup_window": "22-02" if backups else None, "root_password": CANARY,
            "server_type": {"name": "cpx41", "prices": [{"location": "ash", "price_monthly": {"gross": "141.4900000000000000"}}]}}


def hz_box(used, quota=5497558138880, status="active", name="box"):
    return {"name": name, "status": status, "location": {"name": "fsn1"}, "stats": {"size": used},
            "storage_box_type": {"size": quota, "prices": [{"location": "fsn1", "price_monthly": {"gross": "13.0000"}}]}}


def hz_routes(servers=None, boxes=None, currency=None):
    servers = servers if servers is not None else [hz_server("a"), hz_server("b")]
    pricing = {"pricing": {"volume": {"price_per_gb_month": {"gross": "0.0767"}}}}
    if currency:
        pricing["pricing"]["currency"] = currency
    return {("GET", HC + "/servers"): (200, {"servers": servers}),
            ("GET", HC + "/volumes"): (200, {"volumes": [{"size": 400}, {"size": 400}]}),
            ("GET", HC + "/pricing"): (200, pricing),
            ("GET", HZ + "/storage_boxes"): (200, {"storage_boxes": boxes if boxes is not None else [hz_box(1649267441664)]})}


class Hetzner(Base):
    ENV = {"HCLOUD_TOKEN": CANARY}

    def test_ok_estimate_and_storage_boxes_on_the_new_api(self):
        res, rt = self.run_src("hetzner", self.ENV, hz_routes())
        self.assertEqual((res["status"], res["summary"]), ("ok", "2/2 servers up, box 30%, ~$357/mo"))
        self.assertEqual((res["details"]["est_monthly"], res["details"]["currency"]), (357.34, "USD"))  # 2×141.49 + 800×0.0767 + 13
        self.assertEqual(res["details"]["storage_boxes"][0]["quota_tib"], 5.0)
        self.assertIn("not exposed", res["details"]["cost_mtd"])
        self.assertIn(("GET", HZ + "/storage_boxes"), [(c["method"], c["url"]) for c in rt.calls])
        res, _ = self.run_src("hetzner", self.ENV, hz_routes(servers=[hz_server("a", backups=True)], boxes=[]))
        self.assertEqual(res["details"]["est_monthly"], round(141.49 * 1.2 + 800 * 0.0767, 2))  # backups: +20%

    def test_server_states_traffic_and_box_fill(self):
        res, _ = self.run_src("hetzner", self.ENV, hz_routes(servers=[hz_server("a"), hz_server("b", "off")]))
        self.assertEqual(res["status"], "crit")
        self.assertTrue(res["summary"].startswith("server b off"))
        res, _ = self.run_src("hetzner", self.ENV, hz_routes(servers=[hz_server("a", "migrating")]))
        self.assertEqual(res["status"], "warn")
        for frac, want in ((0.5, "ok"), (0.85, "warn"), (0.95, "crit")):
            res, _ = self.run_src("hetzner", self.ENV, hz_routes(boxes=[hz_box(int(5497558138880 * frac))]))
            self.assertEqual(res["status"], want, frac)
        res, _ = self.run_src("hetzner", self.ENV, hz_routes(boxes=[hz_box(int(5497558138880 * 0.85))]),
                              "[sources.hetzner]\nbox_warn_pct = 90\nbox_crit_pct = 95")
        self.assertEqual(res["status"], "ok")
        res, _ = self.run_src("hetzner", self.ENV, hz_routes(servers=[hz_server("a", out_gb=4200)]))
        self.assertEqual((res["status"], res["details"]["servers"][0]["level"]), ("warn", "warn"))  # 95% of included traffic

    def test_several_boxes_ignore_and_a_non_usd_estimate(self):
        res, _ = self.run_src("hetzner", self.ENV, hz_routes(boxes=[hz_box(10, name="a"), hz_box(int(5497558138880 * 0.95), name="b")]))
        self.assertEqual(([b["name"] for b in res["details"]["storage_boxes"]], res["status"]), (["a", "b"], "crit"))
        self.assertIn("box 95%", res["summary"])
        res, _ = self.run_src("hetzner", self.ENV, hz_routes(servers=[hz_server("a"), hz_server("parked", "off")]),
                              '[sources.hetzner]\nignore = ["parked"]')
        self.assertEqual(res["status"], "ok")
        res, _ = self.run_src("hetzner", self.ENV, hz_routes(currency="EUR"))
        self.assertEqual((res["details"]["est_monthly"], res["details"]["currency"]), (357.34, "EUR"))
        self.assertTrue(res["summary"].endswith("~357 EUR/mo"))

    def test_partial_data_and_total_failure(self):
        r = hz_routes()
        r[("GET", HC + "/pricing")] = (500, {})
        res, _ = self.run_src("hetzner", self.ENV, r)
        self.assertEqual(res["status"], "ok")  # the estimate is an extra: its failure is not news
        self.assertNotIn("est_monthly", res["details"])
        r[("GET", HZ + "/storage_boxes")] = (401, {"message": "unauthorized"})
        res, _ = self.run_src("hetzner", self.ENV, r)
        self.assertEqual(res["status"], "warn")
        res, rt = self.run_src("hetzner", self.ENV, hz_routes(), "[sources.hetzner]\nstorage_boxes = false")
        self.assertFalse(any("storage_boxes" in c["url"] for c in rt.calls))
        res, _ = self.run_src("hetzner", self.ENV, {k: TimeoutError("timed out") for k in hz_routes()})
        self.assertEqual(res["status"], "unknown")


# ── GitHub ──────────────────────────────────────────────────────────────────
GH = saas.GH
GH_TOML = '[sources.github]\nrepos = ["acme/api", "acme/web@release"]'


def gh_run(name, concl, wid=1, event="push", **age):
    return {"name": name, "workflow_id": wid, "event": event, "conclusion": concl,
            "status": "completed" if concl else "in_progress", "updated_at": ago(**age)}


def gql_data(**over):
    d = {"p0": {"issueCount": 12}, "c0": {"issueCount": 0}, "h0": {"ref": {"target": {"statusCheckRollup": {"state": "SUCCESS"}}}},
         "p1": {"issueCount": 4}, "c1": {"issueCount": 0}, "h1": {"ref": {"target": {"statusCheckRollup": {"state": "SUCCESS"}}}}}
    d.update(over)
    return {"data": d}


class GitHub(Base):
    def routes(self, api_runs, web_runs=None, gql=None, valid=("Bearer gho_good",), dispatch=None, workflows=None):
        web_runs = web_runs if web_runs is not None else [gh_run("Web checks", "success", days=3)]

        def user(req):
            return (200, {"login": "octocat"}) if req["headers"].get("Authorization") in valid else (401, {"message": "Bad credentials"})

        def rate(req):
            return (200, {}) if req["headers"].get("Authorization") in valid else (401, {"message": "Bad credentials"})

        def by_event(runs, disp):
            return lambda req: (200, {"workflow_runs": runs if req["params"]["event"] == "push" else (disp or [])})
        r = {("GET", GH + "/user"): user, ("GET", GH + "/rate_limit"): rate,
             ("POST", GH + "/graphql"): (200, gql or gql_data()),
             ("GET", GH + "/repos/acme/api/actions/runs"): by_event(api_runs, dispatch),
             ("GET", GH + "/repos/acme/web/actions/runs"): by_event(web_runs, None)}
        for wid, runs in (workflows or {}).items():
            r[("GET", GH + "/repos/acme/api/actions/workflows/%d/runs" % wid)] = runs if isinstance(runs, tuple) else (200, {"workflow_runs": runs})
        return r

    def go(self, api_runs, env=None, toml=GH_TOML, cli="gho_good", **kw):
        with mock.patch.object(saas_core, "_gh_cli_token", return_value=cli) as gh_cli:
            res, rt = self.run_src("github", env if env is not None else {"GITHUB_TOKEN": "stale_" + CANARY}, self.routes(api_runs, **kw), toml)
        return res, rt, gh_cli

    def test_green_token_falls_back_to_gh_and_the_login_becomes_the_author(self):
        res, rt, _ = self.go([gh_run("Deploy API", "success", hours=1), gh_run("Secret Scan", "success", hours=1)])
        self.assertEqual((res["status"], res["details"]["token_source"], res["details"]["user"]), ("ok", "gh", "octocat"))
        self.assertEqual(res["summary"], "CI green x2; 16 open PRs, 0 need changes")
        tried = [c["headers"]["Authorization"] for c in rt.calls if c["url"].endswith("/user")]
        self.assertEqual(tried, ["Bearer stale_" + CANARY, "Bearer gho_good"])  # GH_TOKEN, GITHUB_TOKEN, then gh
        gql = [c for c in rt.calls if c["url"].endswith("/graphql")][0]["json"]
        self.assertNotIn("acme", gql["query"])  # repos and branches travel as variables, never spliced into the query
        self.assertEqual(gql["variables"]["p0"], "repo:acme/api is:pr is:open author:octocat")
        self.assertEqual(gql["variables"]["c1"], "repo:acme/web is:pr is:open author:octocat review:changes_requested")
        self.assertEqual((gql["variables"]["b0"], gql["variables"]["b1"]), ("refs/heads/main", "refs/heads/release"))
        runs = [c for c in rt.calls if "/actions/runs" in c["url"]]
        self.assertEqual({(c["url"].split("/")[5], c["params"]["branch"], c["params"]["event"]) for c in runs},
                         {("api", "main", "push"), ("api", "main", "repository_dispatch"),
                          ("web", "release", "push"), ("web", "release", "repository_dispatch")})

    def test_env_token_wins_when_valid_and_a_set_user_skips_the_lookup(self):
        res, rt, cli = self.go([gh_run("x", "success", hours=1)], env={"GH_TOKEN": "gho_good"})
        self.assertEqual(res["details"]["token_source"], "GH_TOKEN")
        cli.assert_not_called()
        res, rt, _ = self.go([gh_run("x", "success", hours=1)], env={"GH_TOKEN": "gho_good"}, toml=GH_TOML + '\nuser = "hubot"')
        self.assertFalse(any(c["url"].endswith("/user") for c in rt.calls))
        self.assertTrue(any(c["url"].endswith("/rate_limit") for c in rt.calls))  # free: not counted against the limit
        self.assertIn("author:hubot", [c for c in rt.calls if c["url"].endswith("/graphql")][0]["json"]["variables"]["p0"])

    def test_an_app_token_has_no_user_and_counts_every_author(self):
        routes = self.routes([gh_run("x", "success", hours=1)])
        routes[("GET", GH + "/user")] = (403, {"message": "Resource not accessible by integration"})
        with mock.patch.object(saas_core, "_gh_cli_token", return_value=None):
            res, rt = self.run_src("github", {"GITHUB_TOKEN": "gho_good"}, routes, GH_TOML)
        self.assertEqual((res["status"], res["details"]["user"]), ("ok", None))
        self.assertNotIn("author:", [c for c in rt.calls if c["url"].endswith("/graphql")][0]["json"]["variables"]["p0"])

    def test_no_valid_token_and_bad_settings(self):
        res, _, _ = self.go([], cli=None, valid=())
        self.assertEqual((res["status"], res["summary"]), ("unknown", "no valid GitHub token"))
        for toml in ('[sources.github]\nrepos = ["bad repo; DROP"]', '[sources.github]\nrepos = ["acme/../x"]',
                     '[sources.github]\nrepos = ["acme/api"]\nuser = "a b"'):
            res, rt, _ = self.go([], toml=toml)
            self.assertEqual((res["status"], rt.calls), ("unknown", []), toml)

    def test_ci_verdict_uses_the_latest_conclusive_run_per_workflow(self):
        res, _, _ = self.go([gh_run("Deploy", "failure", hours=2), gh_run("Deploy", "success", days=1)], workflows={1: [gh_run("Deploy", "failure", hours=2)]})
        self.assertEqual((res["status"], res["summary"]), ("crit", "api CI failing: Deploy"))
        self.assertEqual(res["alerts"][0]["fix"], "gh run list -R acme/api -b main")
        res, _, _ = self.go([gh_run("Deploy", "success", hours=2), gh_run("Deploy", "failure", days=1)])
        self.assertEqual(res["status"], "ok")  # recovered
        res, _, _ = self.go([gh_run("Deploy", "cancelled", minutes=5), gh_run("Deploy", "skipped", minutes=6), gh_run("Deploy", "success", hours=3)])
        self.assertEqual(res["status"], "ok")  # cancelled and skipped runs never decide
        res, _, _ = self.go([gh_run("Deploy", None, minutes=1), gh_run("Deploy", "success", hours=3)])
        self.assertEqual((res["status"], res["details"]["repos"][0]["ci_pending"]), ("ok", 1))

    def test_dispatch_rerun_supersedes_an_older_push_failure(self):
        res, _, _ = self.go([gh_run("Deploy", "failure", hours=20)],
                            dispatch=[gh_run("Deploy", "success", event="repository_dispatch", hours=2)])
        self.assertEqual(res["status"], "ok", res["summary"])
        res, _, _ = self.go([gh_run("Deploy", "success", hours=20)],
                            dispatch=[gh_run("Deploy", "failure", event="repository_dispatch", hours=2)],
                            workflows={1: [gh_run("Deploy", "failure", event="repository_dispatch", hours=2)]})
        self.assertEqual((res["status"], res["summary"]), ("crit", "api CI failing: Deploy"))

    def test_a_suspected_failure_is_confirmed_from_its_own_run_list(self):
        # The dispatch page is full of other workflows' runs, so the re-run that fixed Deploy fell off
        # it while the old push failure is still on the push page: the per-workflow list decides.
        busy = [gh_run("Other %d" % i, "success", wid=9, event="repository_dispatch", minutes=i) for i in range(30)]
        own = [gh_run("Deploy", "success", event="repository_dispatch", hours=18), gh_run("Deploy", "failure", hours=20),
               gh_run("Deploy", "success", event="schedule", hours=1)]
        res, rt, _ = self.go([gh_run("Deploy", "failure", hours=20)], dispatch=busy, workflows={1: own})
        self.assertEqual(res["status"], "ok", res["summary"])
        conf = [c for c in rt.calls if "/workflows/" in c["url"]]
        self.assertEqual([(c["params"]["branch"], c["params"]["exclude_pull_requests"]) for c in conf], [("main", "true")])
        own[0]["conclusion"] = "failure"  # still red in its own list: an incident
        res, _, _ = self.go([gh_run("Deploy", "failure", hours=20)], dispatch=busy, workflows={1: own})
        self.assertEqual(res["status"], "crit")
        res, _, _ = self.go([gh_run("Deploy", "failure", hours=20)], dispatch=busy, workflows={1: (500, {})})
        self.assertEqual(res["status"], "crit")  # the confirmation failed: keep the alarm

    def test_old_failure_is_stale_not_an_incident(self):
        res, _, _ = self.go([gh_run("Quarterly job", "failure", days=30)])
        self.assertEqual((res["status"], res["details"]["repos"][0]["ci_stale_failing"]), ("ok", ["Quarterly job"]))
        res, _, _ = self.go([gh_run("Weekly job", "failure", days=3)], toml=GH_TOML + "\nstale_days = 2")
        self.assertEqual(res["status"], "ok")

    def test_changes_requested_and_head_rollup(self):
        gql = gql_data(c0={"issueCount": 2}, h1={"ref": {"target": {"statusCheckRollup": {"state": "FAILURE"}}}})
        res, _, _ = self.go([gh_run("x", "success", hours=1)], gql=gql)
        self.assertEqual(res["status"], "crit")  # web's failed HEAD checks outrank api's PR warning
        self.assertEqual(res["summary"], "web HEAD checks FAILURE")
        self.assertEqual([a["sev"] for a in res["alerts"]], ["crit", "warn"])
        res, _, _ = self.go([gh_run("x", "success", hours=1)], gql=gql_data(c0={"issueCount": 2}))
        self.assertEqual((res["status"], res["summary"]), ("warn", "api: 2 PR(s) need changes"))

    def test_graphql_failures_are_partial(self):
        res, _, _ = self.go([gh_run("x", "success", hours=1)], gql={"errors": [{"message": "rate limited " + CANARY}]})
        self.assertEqual((res["status"], res["summary"]), ("warn", "partial data (gql)"))
        partial = dict(gql_data(), errors=[{"message": "Could not resolve to a Repository"}])
        partial["data"]["h1"] = None
        res, _, _ = self.go([gh_run("x", "success", hours=1)], gql=partial)
        self.assertEqual(res["details"]["repos"][0]["open_prs"], 12)  # the repos that resolved still count
        self.assertIn("gql", res["details"]["partial_errors"])

    def test_labels_disambiguate_same_named_repos(self):
        self.assertEqual([r[2] for r in saas._gh_repos({"repos": ["a/api", "b/api", "a/web", {"repo": "a/x", "label": "ex"}]})],
                         ["a/api", "b/api", "web", "ex"])
        self.assertEqual(saas._gh_repos({"repos": ["a/api", "a/api@dev"], "branch": "trunk"}), [("a/api", "trunk", "api")])


# ── Sentry ──────────────────────────────────────────────────────────────────
SB = "https://sentry.io/api/0/organizations/"
US = "https://us.sentry.io/api/0/organizations/acme"


class Sentry(Base):
    ENV = {"SENTRY_AUTH_TOKEN": CANARY}

    def routes(self, rows, orgs=None):
        orgs = orgs if orgs is not None else [{"slug": "acme", "links": {"regionUrl": "https://us.sentry.io"}}]
        return {("GET", SB): (200, orgs), ("GET", US + "/events/"): (200, {"data": rows}),
                ("GET", US + "/projects/"): (200, [{"slug": "p%d" % i} for i in range(5)])}

    def test_quiet_and_query_shape(self):
        res, rt = self.run_src("sentry", self.ENV, self.routes([]))
        self.assertEqual((res["status"], res["summary"]), ("ok", "0 unresolved issues in 24h (5 projects)"))
        ev = [c for c in rt.calls if c["url"].endswith("/events/")][0]["params"]
        self.assertEqual((ev["statsPeriod"], ev["query"], ev["dataset"], ev["project"]), ("24h", "is:unresolved", "errors", "-1"))
        self.assertEqual(ev["field"], ["project", "count_unique(issue)", "count()"])
        self.assertTrue(all(c["headers"]["Authorization"] == "Bearer " + CANARY for c in rt.calls))

    def test_severity_by_volume_and_its_settings(self):
        res, _ = self.run_src("sentry", self.ENV, self.routes([{"project": "web", "count_unique(issue)": 3, "count()": 10}]))
        self.assertEqual((res["status"], res["summary"]), ("warn", "3 unresolved issues, 10 events/24h (web)"))
        res, _ = self.run_src("sentry", self.ENV, self.routes([{"project": "api", "count_unique(issue)": 12, "count()": 40}]))
        self.assertEqual(res["status"], "crit")
        res, _ = self.run_src("sentry", self.ENV, self.routes([{"project": "api", "count_unique(issue)": 2, "count()": 500}]))
        self.assertEqual(res["status"], "crit")
        res, _ = self.run_src("sentry", self.ENV, self.routes([{"project": "web", "count_unique(issue)": 3, "count()": 10}]),
                              "[sources.sentry]\nissues_warn = 5")
        self.assertEqual(res["status"], "ok")

    def test_token_never_goes_to_a_host_the_api_merely_names(self):
        for region in ("https://evil.example.com", "http://us.sentry.io", "https://us.sentry.io:8443", "https://sentry.io.evil.example"):
            res, rt = self.run_src("sentry", self.ENV, {("GET", SB): (200, [{"slug": "acme", "links": {"regionUrl": region}}])})
            self.assertEqual(res["status"], "unknown", region)
            self.assertEqual([c["url"] for c in rt.calls], [SB], region)

    def test_org_filter_and_self_hosted(self):
        orgs = [{"slug": "acme", "links": {"regionUrl": "https://us.sentry.io"}}, {"slug": "other", "links": {}}]
        res, rt = self.run_src("sentry", self.ENV, self.routes([], orgs), '[sources.sentry]\norgs = ["acme"]')
        self.assertEqual(res["details"]["orgs"], ["acme"])
        base = "https://errors.example.com/api/0/organizations/"
        routes = {("GET", base): (200, [{"slug": "acme", "links": {"regionUrl": "https://errors.example.com"}}]),
                  ("GET", base + "acme/events/"): (200, {"data": []}), ("GET", base + "acme/projects/"): (200, [])}
        res, rt = self.run_src("sentry", self.ENV, routes, '[sources.sentry]\nurl = "https://errors.example.com"')
        self.assertEqual(res["status"], "ok")
        res, rt = self.run_src("sentry", self.ENV, {}, '[sources.sentry]\nurl = "http://errors.example.com"')
        self.assertEqual((res["status"], rt.calls), ("unknown", []))  # never a token over plain http

    def test_failures(self):
        res, _ = self.run_src("sentry", self.ENV, {("GET", SB): (401, {"detail": "Invalid token"})})
        self.assertEqual((res["status"], res["error"]), ("unknown", "sentry HTTP 401: Invalid token"))
        r = self.routes([])
        r[("GET", US + "/events/")] = (403, {"detail": "no event:read"})
        res, _ = self.run_src("sentry", self.ENV, r)
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(self.run_src("sentry", {}, {})[0]["error"], "missing env: SENTRY_AUTH_TOKEN")


# ── Stripe ──────────────────────────────────────────────────────────────────
STR = saas.STRIPE


def sub(status, amount=1000, interval="month", count=1, qty=1, cur="usd", sid="sub_1"):
    return {"id": sid, "status": status, "customer": "cus_x", "items": {"data": [{"quantity": qty, "price": {
        "currency": cur, "unit_amount": amount, "recurring": {"interval": interval, "interval_count": count}}}]}}


class Stripe(Base):
    ENV = {"STRIPE_SECRET_KEY": "sk_live_" + CANARY}

    def routes(self, subs, has_more=False):
        return {("GET", STR + "/balance"): (200, {"available": [{"currency": "usd", "amount": 12345}], "pending": [{"currency": "usd", "amount": 0}]}),
                ("GET", STR + "/subscriptions"): (200, {"data": subs, "has_more": has_more})}

    def test_ok_summary_mode_and_mrr(self):
        res, rt = self.run_src("stripe", self.ENV, self.routes([sub("active"), sub("canceled"), sub("canceled")]))
        self.assertEqual((res["status"], res["summary"]), ("ok", "live, $123.45 avail, 1 active, 0 past due"))
        self.assertEqual((res["details"]["key_mode"], res["details"]["subscriptions"]["mrr_est"]), ("sk_live", {"usd": 10.0}))
        self.assertEqual([c["params"] for c in rt.calls if "subscriptions" in c["url"]], [{"status": "all", "limit": "100"}])
        self.assertTrue(all(c["method"] == "GET" for c in rt.calls))  # never writes

    def test_mrr_normalisation(self):
        subs = [sub("active", 12000, "year"), sub("active", 500, "month", qty=3), sub("active", 700, "week"), sub("active", 3000, "month", count=3)]
        res, _ = self.run_src("stripe", self.ENV, self.routes(subs))
        want = 12000 / 12.0 / 100 + 1500 / 100.0 + 700 * 52 / 12.0 / 100 + 3000 / 3.0 / 100
        self.assertAlmostEqual(res["details"]["subscriptions"]["mrr_est"]["usd"], round(want, 2), places=2)

    def test_past_due_pagination_truncation_and_test_mode(self):
        pages = iter([(200, {"data": [sub("active", sid="sub_a"), sub("past_due", sid="sub_b")], "has_more": True}),
                      (200, {"data": [sub("unpaid", sid="sub_c")], "has_more": True})])
        r = self.routes([])
        r[("GET", STR + "/subscriptions")] = lambda req: next(pages)
        res, rt = self.run_src("stripe", self.ENV, r, "[sources.stripe]\nmax_pages = 2")
        self.assertEqual(res["status"], "warn")
        self.assertIn("2 past due", res["summary"])
        self.assertIn("1 active+", res["summary"])  # more pages exist than were read
        self.assertEqual([c["params"].get("starting_after") for c in rt.calls if "subscriptions" in c["url"]], [None, "sub_b"])
        res, _ = self.run_src("stripe", {"STRIPE_SECRET_KEY": "rk_test_" + CANARY}, self.routes([]))
        self.assertEqual((res["details"]["key_mode"], res["summary"].split(",")[0]), ("rk_test", "test"))

    def test_partial_restricted_key_and_total_failure(self):
        r = self.routes([])
        r[("GET", STR + "/subscriptions")] = (403, {"error": {"message": "This key lacks permission"}})
        res, _ = self.run_src("stripe", self.ENV, r)
        self.assertEqual(res["status"], "warn")
        self.assertIn("subs", res["details"]["partial_errors"])
        res, rt = self.run_src("stripe", self.ENV, self.routes([]), "[sources.stripe]\nsubscriptions = false")
        self.assertEqual((res["status"], len(rt.calls)), ("ok", 1))
        res, _ = self.run_src("stripe", self.ENV, {k: (401, {"error": {"message": "Invalid API Key"}}) for k in self.routes([])})
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(self.run_src("stripe", {}, {})[0]["error"], "missing env: STRIPE_SECRET_KEY")


# ── Resend ──────────────────────────────────────────────────────────────────
RS = saas.RESEND
RESEND_ENV = {"RESEND_API_KEY": "re_" + CANARY}
RESTRICTED = (401, {"statusCode": 401, "name": "restricted_api_key", "message": "This API key is restricted to only send emails"})


def email(event, **age):
    return {"id": "e", "to": ["someone@example.com"], "subject": "private", "last_event": event,
            "created_at": ago(**age).replace("T", " ").replace("Z", "+00")}  # Postgres-style, as Resend sends


class Resend(Base):
    def routes(self, domains, emails):
        return {("GET", RS + "/domains"): (200, {"data": domains}), ("GET", RS + "/emails"): emails}

    def test_send_only_key_is_valid_but_unreadable(self):
        res, rt = self.run_src("resend", RESEND_ENV, {("GET", RS + "/domains"): RESTRICTED})
        self.assertEqual((res["status"], res["error"]), ("unknown", "restricted_api_key"))
        self.assertEqual((res["details"]["key_valid"], res["details"]["can_read"]), (True, False))
        self.assertEqual(len(rt.calls), 1)  # nothing else is tried with a key that cannot read
        self.assertTrue(collect._soft_failure(res))

    def test_invalid_key_is_crit(self):
        res, _ = self.run_src("resend", RESEND_ENV, {("GET", RS + "/domains"): (400, {"message": "API key is invalid", "name": "validation_error"})})
        self.assertEqual((res["status"], res["summary"]), ("crit", "Resend key rejected"))
        res, _ = self.run_src("resend", RESEND_ENV, {("GET", RS + "/domains"): (429, {"message": "slow down"})})
        self.assertEqual(res["status"], "unknown")

    def test_full_access_paths(self):
        dom = [{"name": "example.com", "status": "verified"}]
        res, _ = self.run_src("resend", RESEND_ENV, self.routes(dom, (200, {"data": [email("delivered", hours=1), email("delivered", days=3)]})))
        self.assertEqual((res["status"], res["summary"]), ("ok", "1/1 domains verified, 1 emails/24h"))
        self.assertEqual(res["details"]["emails_24h"]["by_last_event"], {"delivered": 1})
        self.assertNotIn("someone@example.com", json.dumps(res))  # recipients and subjects are never copied
        self.assertNotIn("private", json.dumps(res))
        res, _ = self.run_src("resend", RESEND_ENV, self.routes([{"name": "x.example", "status": "pending"}], (200, {"data": []})))
        self.assertEqual(res["status"], "warn")
        res, _ = self.run_src("resend", RESEND_ENV, self.routes([{"name": "x.example", "status": "failed"}], (200, {"data": []})))
        self.assertEqual(res["status"], "crit")
        many = [email("bounced", minutes=5)] * 3 + [email("delivered", minutes=5)] * 27
        res, _ = self.run_src("resend", RESEND_ENV, self.routes(dom, (200, {"data": many})))
        self.assertEqual(res["status"], "crit")  # 10% bounced across 30 sends
        res, _ = self.run_src("resend", RESEND_ENV, self.routes(dom, (200, {"data": many})), "[sources.resend]\nbounce_crit_pct = 20")
        self.assertEqual(res["status"], "warn")
        res, _ = self.run_src("resend", RESEND_ENV, self.routes(dom, (200, {"data": [email("bounced", hours=1), email("delivered", hours=1)]})))
        self.assertEqual(res["status"], "warn")  # a small sample: warn, not crit
        res, _ = self.run_src("resend", RESEND_ENV, self.routes(dom, (500, {})))
        self.assertEqual((res["status"], "emails_error" in res["details"]), ("ok", True))


# ── Langfuse ────────────────────────────────────────────────────────────────
H1, H2 = "http://10.0.0.5:3000", "https://langfuse.example.com"
PK1, PK2 = "pk-lf-" + "1" * 12, "pk-lf-" + "2" * 12
LF_ENV = {"LANGFUSE_HOST": H1, "LANGFUSE_BASE_URL": H2, "LANGFUSE_PUBLIC_KEY": PK1, "LANGFUSE_SECRET_KEY": "sk-lf-" + CANARY,
          "ACME_LF_PUBLIC": PK2, "ACME_LF_SECRET": "sk-lf-" + CANARY[::-1]}
LF_TWO = ('[sources.langfuse]\nkeys = [{ public_env = "LANGFUSE_PUBLIC_KEY", secret_env = "LANGFUSE_SECRET_KEY" },\n'
          '        { public_env = "ACME_LF_PUBLIC", secret_env = "ACME_LF_SECRET", label = "acme-dev" }]')


def lf_routes(base=H1, metrics=True, bad_key=None):
    def project(req):
        who = req["basic"][0]
        if who == bad_key:
            return 401, {"message": "Invalid credentials"}
        return 200, {"data": [{"name": "acme" if who == PK1 else "acme-two"}]}

    def mx(req):
        n = 0 if req["basic"][0] == PK1 else 7
        q = json.loads(req["params"]["query"])
        assert q["view"] == "traces" and q["fromTimestamp"] < q["toTimestamp"]
        return 200, {"data": [{"count_count": n, "sum_totalCost": None if n == 0 else 1.5}]}
    return {("GET", base + "/api/public/health"): (200, {"status": "OK", "version": "3.120.0"}),
            ("GET", base + "/api/public/projects"): project,
            ("GET", base + "/api/public/metrics"): mx if metrics else (404, {"message": "no"}),
            ("GET", base + "/api/public/traces"): (200, {"meta": {"totalItems": 4}})}


class Langfuse(Base):
    def test_two_projects_basic_auth_and_the_details_contract(self):
        r = lf_routes()
        r[("GET", H2 + "/api/public/health")] = TimeoutError("timed out")
        res, rt = self.run_src("langfuse", LF_ENV, r, LF_TWO)
        self.assertEqual((res["status"], res["summary"]), ("ok", "v3.120.0 ok; acme 0tr/$0.00; acme-two 7tr/$1.50"))
        # Another panel reads exactly this shape.
        self.assertEqual(res["details"]["version"], "3.120.0")
        self.assertEqual(res["details"]["projects"], [{"project": "acme", "traces_24h": 0, "cost_24h_usd": 0.0},
                                                      {"project": "acme-two", "traces_24h": 7, "cost_24h_usd": 1.5}])
        self.assertFalse(res["details"]["used_fallback_url"])
        self.assertEqual({c["basic"][0] for c in rt.calls if c["url"].endswith("/projects")}, {PK1, PK2})
        self.assertTrue(all(c["basic"] is None for c in rt.calls if c["url"].endswith("/health")))  # health is unauthenticated
        self.assertNotIn("sk-lf-", json.dumps(res))
        self.assertNotIn(PK2, json.dumps(res))  # custom-named key variables are scrubbed too

    def test_fallback_url_host_setting_and_dead(self):
        r = lf_routes(base=H2)
        r[("GET", H1 + "/api/public/health")] = ConnectionRefusedError("refused")
        res, _ = self.run_src("langfuse", LF_ENV, r)
        self.assertEqual((res["status"], res["details"]["used_fallback_url"]), ("ok", True))
        res, rt = self.run_src("langfuse", LF_ENV, lf_routes(base=H2), '[sources.langfuse]\nhost = "%s"' % H2)
        self.assertTrue(all(c["url"].startswith(H2) for c in rt.calls))
        dead = {("GET", H1 + "/api/public/health"): TimeoutError("timed out"), ("GET", H2 + "/api/public/health"): (503, {})}
        res, _ = self.run_src("langfuse", LF_ENV, dead)
        self.assertEqual((res["status"], res["summary"]), ("crit", "Langfuse unreachable"))
        self.assertEqual(res["alerts"][0]["sev"], "crit")
        res, _ = self.run_src("langfuse", LF_ENV, {("GET", H1 + "/api/public/health"): (200, {"status": "DEGRADED"}),
                                                   ("GET", H2 + "/api/public/health"): (503, {})})
        self.assertEqual(res["status"], "crit")

    def test_metrics_fallback_bad_key_and_no_keys(self):
        env = dict(LF_ENV, LANGFUSE_BASE_URL="")
        res, _ = self.run_src("langfuse", env, lf_routes(metrics=False), LF_TWO)
        self.assertEqual([p["traces_24h"] for p in res["details"]["projects"]], [4, 4])  # traces?limit=1 → meta.totalItems
        res, _ = self.run_src("langfuse", env, lf_routes(bad_key=PK2), LF_TWO)
        self.assertEqual(res["status"], "warn")
        self.assertIn("HTTP 401", res["summary"])
        self.assertEqual(res["details"]["projects"][1]["project"], "acme-dev")  # the label stands in for the name
        res, _ = self.run_src("langfuse", {"LANGFUSE_HOST": H1}, lf_routes())
        self.assertEqual(res["status"], "warn")  # a healthy server, but no key pair set
        self.assertEqual(self.run_src("langfuse", {}, {})[0]["error"], "missing env: LANGFUSE_HOST")


# ── AI API spend ────────────────────────────────────────────────────────────
OR, AN, OA = "https://openrouter.ai/api/v1", "https://api.anthropic.com/v1/organizations/cost_report", "https://api.openai.com/v1/organization/costs"
XAI = "https://management-api.x.ai"
TEAM = "01234567-89ab-cdef-0123-456789abcdef"
AI_ENV = {"OPENROUTER_MGMT_KEY": CANARY, "ANTHROPIC_ADMIN_KEY": "sk-ant-admin01-" + CANARY, "OPENAI_ADMIN_KEY": "sk-admin-" + CANARY,
          "XAI_MGMT_KEY": "xai-" + CANARY}


def or_routes(total=500, used=457.03):
    return {("GET", OR + "/credits"): (200, {"data": {"total_credits": total, "total_usage": used}}),
            ("GET", OR + "/key"): (200, {"data": {"label": "sk-or-v1-a1b...", "name": CANARY, "limit": None, "usage_monthly": 0.12,
                                                  "is_management_key": True}})}


def an_page(day, amount, more=None):
    return (200, {"data": [{"starting_at": "2026-10-%02dT00:00:00Z" % day, "results": [{"currency": "USD", "amount": amount}]}],
                  "has_more": bool(more), "next_page": more})


def oa_page(day, value, more=None):
    return (200, {"data": [{"start_time": calendar.timegm((2026, 10, day, 0, 0, 0)), "results": [{"amount": {"value": value, "currency": "usd"}}]}],
                  "has_more": bool(more), "next_page": more})


def xai_routes(balance=(403, {"code": 7, "message": "BillingRead denied for management key xai-...54nd"})):
    return {("GET", XAI + "/auth/management-keys/validation"): (200, {"teamId": TEAM, "acls": []}),
            ("GET", "%s/v1/billing/teams/%s/prepaid/balance" % (XAI, TEAM)): balance}


class AISpend(Base):
    def only(self, name, routes, env=None, toml=""):
        keys = {"openrouter": ("OPENROUTER_MGMT_KEY",), "anthropic": ("ANTHROPIC_ADMIN_KEY",), "openai": ("OPENAI_ADMIN_KEY",),
                "xai": ("XAI_MGMT_KEY",)}[name]
        res, rt = self.run_src("ai_spend", env or {k: AI_ENV[k] for k in keys}, routes, toml)
        self.assertEqual(set(k for k in res["details"] if k != "mtd_usd"), {name})  # only the configured provider runs
        return res, res["details"][name], rt

    def test_openrouter_thresholds_and_its_key_label_is_never_read(self):
        for total, used, want in ((500, 495, "warn"), (500, 499, "crit"), (500, 100, "ok")):
            res, sub_, _ = self.only("openrouter", or_routes(total, used))
            self.assertEqual((res["status"], sub_["status"]), (want, want), used)
        res, sub_, _ = self.only("openrouter", or_routes(500, 457.03), toml="[sources.ai_spend]\nopenrouter_warn_usd = 50")
        self.assertEqual((sub_["summary"], sub_["details"]["remaining_usd"], sub_["details"]["remaining_pct"]),
                         ("OpenRouter $42.97 left of $500", 42.97, 8.6))
        self.assertEqual(res["alerts"], [{"sev": "warn", "msg": "ai spend: OpenRouter $42.97 left of $500"}])
        self.assertNotIn("sk-or-v1", json.dumps(res))
        res, rt = self.run_src("ai_spend", {"OPENROUTER_API_KEY": "plain_key_value_99"}, or_routes())
        self.assertEqual(rt.calls[0]["headers"]["Authorization"], "Bearer plain_key_value_99")  # no mgmt key: the API key

    def test_anthropic_sums_cents_over_pages_and_ignores_other_currencies(self):
        pages = iter([(200, {"data": [{"starting_at": "2026-10-01T00:00:00Z", "results": [{"currency": "USD", "amount": "12.5"}]},
                                      {"starting_at": "2026-10-02T00:00:00Z", "results": []}], "has_more": True, "next_page": "tok2"}),
                      (200, {"data": [{"starting_at": "2026-10-15T00:00:00Z", "results": [{"currency": "USD", "amount": "27.5"},
                                                                                           {"currency": "EUR", "amount": "999"}]}],
                             "has_more": False, "next_page": None})])
        res, sub_, rt = self.only("anthropic", {("GET", AN): lambda req: next(pages)})
        self.assertEqual((sub_["status"], sub_["summary"]), ("ok", "Anthropic MTD $0.40 (through 2026-10-15)"))  # 40 cents
        self.assertEqual(rt.calls[0]["params"]["starting_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(rt.calls[1]["params"]["page"], "tok2")
        self.assertEqual(rt.calls[0]["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(rt.calls[0]["headers"]["x-api-key"], AI_ENV["ANTHROPIC_ADMIN_KEY"])

    def test_openai_sums_dollars_over_pages(self):
        pages = iter([oa_page(1, 0.017, "p2"), oa_page(16, 1.753)])
        res, sub_, rt = self.only("openai", {("GET", OA): lambda req: next(pages)})
        self.assertEqual((sub_["details"]["mtd_usd"], sub_["details"]["last_bucket"]), (1.77, "2026-10-16"))
        self.assertEqual(rt.calls[0]["params"]["start_time"], str(calendar.timegm((2026, 10, 1, 0, 0, 0))))
        self.assertEqual(res["details"]["mtd_usd"], 1.77)

    def test_xai_billing_permission_missing_vs_granted(self):
        res, sub_, _ = self.only("xai", xai_routes())
        self.assertEqual((sub_["status"], sub_["summary"], sub_["error"]),
                         ("unknown", "xAI mgmt key lacks BillingRead permission", "HTTP 403 BillingRead"))
        self.assertNotIn("54nd", json.dumps(res))
        res, sub_, _ = self.only("xai", xai_routes((200, {"total": {"val": "-5000"}})))
        self.assertEqual((sub_["status"], sub_["details"]["prepaid_total_raw"]), ("ok", "-5000"))
        routes = {("GET", XAI + "/auth/management-keys/validation"): (200, {"teamId": "../../other"})}
        res, sub_, rt = self.only("xai", routes)
        self.assertEqual((sub_["status"], len(rt.calls)), ("unknown", 1))  # a team id that is not a UUID never enters a path

    def test_aggregate_is_the_worst_known_status(self):
        routes = or_routes()
        routes.update(xai_routes())
        routes.update({("GET", AN): an_page(15, "391"), ("GET", OA): oa_page(15, 6.18)})
        res, _ = self.run_src("ai_spend", AI_ENV, routes, "[sources.ai_spend]\nopenrouter_warn_usd = 50")
        self.assertEqual(res["status"], "warn")  # the low OpenRouter balance; xAI's unknown is ignored
        self.assertEqual(res["summary"], "MTD A$3.91 O$6.18 | OR $43 left | xAI n/a")
        self.assertEqual(set(res["details"]), {"openrouter", "anthropic", "openai", "xai", "mtd_usd"})
        self.assertEqual(res["details"]["mtd_usd"], 10.09)
        dead = {k: (401, {"error": {"message": "invalid x-api-key"}}) for k in routes}
        res, _ = self.run_src("ai_spend", AI_ENV, dead)
        self.assertEqual(res["status"], "unknown")
        self.assertIn("anthropic", res["error"])
        self.assertEqual(res["summary"], "MTD A n/a O n/a | OR n/a | xAI n/a")  # no invented "$0 left" for a missing reading
        res, rt = self.run_src("ai_spend", {}, {})
        self.assertEqual((res["status"], rt.calls), ("unknown", []))
        self.assertTrue(res["error"].startswith("missing env: "))


# ── AWS ─────────────────────────────────────────────────────────────────────
AWS_CFG = ("[default]\nregion = us-east-1\n\n[sso-session corp]\nsso_start_url = https://example.awsapps.com/start\nregion = eu-west-1\n\n"
           "[profile admin]\nsso_session = corp\nsso_account_id = 123456789012\nregion = eu-central-1\n\n[profile lab]\n")
SSO_EXPIRED = "\nError when retrieving token from sso: Token has expired and refresh failed\n"
NO_CREDS = 'Unable to locate credentials. You can configure credentials by running "aws configure".\n'


class AWS(Base):
    def setUp(self):
        fd, self.cfg = tempfile.mkstemp(suffix=".cfg")
        os.write(fd, AWS_CFG.encode())
        os.close(fd)
        self.addCleanup(os.unlink, self.cfg)
        self.env = {"AWS_CONFIG_FILE": self.cfg}
        self.calls = []
        p = mock.patch.object(saas_accounts, "_aws_bin", return_value="/fake/aws")
        p.start()
        self.addCleanup(p.stop)

    def fake(self, ok=("admin",), accounts=None, ec2=None, ce="123.456"):
        accounts = accounts or {}

        def run(argv, timeout=None, env=None, **kw):
            self.calls.append(argv)
            self.assertEqual((env["AWS_EC2_METADATA_DISABLED"], env["AWS_PAGER"]), ("true", ""))
            self.assertLessEqual(timeout, saas.CLI_TIMEOUT)
            prof = argv[argv.index("--profile") + 1] if "--profile" in argv else None
            region = argv[argv.index("--region") + 1] if "--region" in argv else None
            if argv[1] == "sts":
                if prof in ok:
                    acct = accounts.get(prof, "123456789012")
                    return 0, json.dumps({"Account": acct, "Arn": "arn:aws:sts::%s:assumed-role/Admin/someone@example.com" % acct}), ""
                return 254, "", SSO_EXPIRED if prof == "admin" else NO_CREDS
            if argv[1] == "ec2":
                v = (ec2 or {}).get(region, [])
                return (0, json.dumps(v), "") if v != "ERR" else (255, "", "An error occurred (UnauthorizedOperation)")
            if argv[1] == "ce":
                return 0, json.dumps({"ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": ce}}}]}), ""
            raise AssertionError(argv)
        return run

    def test_profiles_come_from_the_config_file_without_sso_sessions(self):
        c = ctx(host(), {}, self.env)
        self.assertEqual(saas._aws_profiles(c), [("default", "us-east-1"), ("admin", "eu-central-1"), ("lab", "")])
        c = ctx(host(), {}, dict(self.env, AWS_PROFILE="lab"))
        self.assertEqual([p for p, _r in saas._aws_profiles(c)], ["lab", "default", "admin"])
        c = ctx(host(), {"profiles": ["admin"]}, self.env)
        self.assertEqual(saas._aws_profiles(c), [("admin", "eu-central-1")])

    def test_expired_sso_is_a_warning_carrying_the_fix(self):
        res, _ = self.run_src("aws_account", self.env, {}, run=self.fake(ok=()))
        self.assertEqual((res["status"], res["error"]), ("warn", "sso-expired"))
        self.assertEqual(res["summary"], "SSO token expired: aws sso login --profile admin")
        self.assertEqual(res["alerts"][0]["fix"], "aws sso login --profile admin")
        self.assertEqual(res["details"]["profile_errors"], {"default": "no-credentials", "admin": "sso-expired", "lab": "no-credentials"})

    def test_no_credentials_anywhere_is_unknown(self):
        res, _ = self.run_src("aws_account", self.env, {}, "[sources.aws_account]\nprofiles = [\"lab\"]", run=self.fake(ok=()))
        self.assertEqual((res["status"], res["error"]), ("unknown", "no-credentials"))
        self.assertTrue(collect._soft_failure(res))

    def test_success_counts_ec2_in_the_profile_region_hides_the_account_and_skips_cost_explorer(self):
        ec2 = {"eu-central-1": [{"n": "a", "t": "m5.large"}, {"n": None, "t": "m5.large"}], "us-east-1": [{"n": "w", "t": "c5"}]}
        res, _ = self.run_src("aws_account", self.env, {}, run=self.fake(ec2=ec2))
        self.assertEqual((res["status"], res["summary"]), ("ok", "acct ...9012, 2 EC2 running"))
        a = res["details"]["accounts"][0]
        self.assertEqual((a["profile"], a["account_last4"], a["principal_type"]), ("admin", "9012", "assumed-role"))
        self.assertEqual(a["ec2_running"], {"eu-central-1": {"count": 2, "names": ["a"]}})  # the profile's own region only
        text = json.dumps(res)
        self.assertNotIn("123456789012", text)
        self.assertNotIn("someone@example.com", text)  # the role session name is not copied
        self.assertFalse(any(c[1] == "ce" for c in self.calls), "Cost Explorer bills per call: opt-in only")

    def test_regions_cost_explorer_opt_in_and_partial_ec2(self):
        toml = '[sources.aws_account]\nregions = ["us-east-1", "us-west-2"]\ncost_explorer = true'
        res, _ = self.run_src("aws_account", self.env, {}, toml, run=self.fake(ec2={"us-east-1": [{"n": "a"}], "us-west-2": "ERR"}))
        a = res["details"]["accounts"][0]
        self.assertEqual(a["mtd_unblended_usd"], 123.46)
        self.assertEqual(sum(1 for c in self.calls if c[1] == "ce"), 1)
        self.assertEqual((res["status"], a["ec2_running"]["us-west-2"]), ("warn", {"error": "access-denied"}))
        ce = [c for c in self.calls if c[1] == "ce"][0]
        self.assertEqual(ce[ce.index("--time-period") + 1], "Start=2026-10-01,End=2026-10-17")

    def test_two_profiles_into_one_account_count_once(self):
        res, _ = self.run_src("aws_account", self.env, {}, run=self.fake(ok=("default", "admin"), ec2={"us-east-1": [{"n": "x"}]}))
        self.assertEqual(len(res["details"]["accounts"]), 1)
        res, _ = self.run_src("aws_account", self.env, {}, run=self.fake(ok=("default", "admin"), accounts={"admin": "210987654321"}))
        self.assertEqual(res["summary"], "2 accounts, 0 EC2 running")

    def test_cli_missing(self):
        with mock.patch.object(saas_accounts, "_aws_bin", return_value=None):
            res, _ = self.run_src("aws_account", self.env, {}, run=self.fake())
        self.assertEqual((res["status"], res["summary"]), ("unknown", "aws CLI not installed"))
        self.assertEqual(self.calls, [])


# ── Twilio ──────────────────────────────────────────────────────────────────
SID = "AC" + "0123456789abcdef" * 2
TW = "https://api.twilio.com/2010-04-01/Accounts/" + SID


class Twilio(Base):
    ENV = {"TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": CANARY}

    def routes(self, balance="31.2500", count="4"):
        return {("GET", TW + "/Balance.json"): (200, {"balance": balance, "currency": "USD", "account_sid": SID}),
                ("GET", TW + "/Usage/Records/Today.json"): (200, {"usage_records": [{"category": "sms", "count": count}]})}

    def test_without_credentials_it_is_unknown_and_makes_no_request(self):
        res, rt = self.run_src("twilio", {}, {})
        self.assertEqual((res["status"], res["error"]), ("unknown", "missing env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN"))
        self.assertEqual((rt.calls, res.get("alerts")), ([], None))

    def test_basic_auth_and_balance_thresholds(self):
        res, rt = self.run_src("twilio", self.ENV, self.routes())
        self.assertEqual((res["status"], res["summary"]), ("ok", "$31.25 balance, 4 SMS today"))
        self.assertTrue(all(c["basic"] == (SID, CANARY) and not c["headers"] for c in rt.calls))  # the secret: Basic only
        self.assertEqual(rt.calls[1]["params"], {"Category": "sms"})
        self.assertNotIn(SID, json.dumps(res))
        for bal, want in (("3.00", "crit"), ("15.00", "warn"), ("50.00", "ok")):
            res, _ = self.run_src("twilio", self.ENV, self.routes(balance=bal))
            self.assertEqual((res["status"], res["details"]["balance"]["level"]), (want, want), bal)
        res, _ = self.run_src("twilio", self.ENV, self.routes(balance="15.00"), "[sources.twilio]\nbalance_warn = 10")
        self.assertEqual(res["status"], "ok")

    def test_credentials_from_an_env_file(self):
        fd, path = tempfile.mkstemp()
        os.write(fd, ("# the service's own file\nexport TWILIO_ACCOUNT_SID=%s\nTWILIO_AUTH_TOKEN='%s'\n" % (SID, CANARY)).encode())
        os.close(fd)
        self.addCleanup(os.unlink, path)
        res, rt = self.run_src("twilio", {}, self.routes(), '[sources.twilio]\nenv_file = "%s"' % path)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(all(c["basic"] == (SID, CANARY) for c in rt.calls))
        self.assertNotIn(SID, json.dumps(res))  # not in a secret-named env var here, scrubbed all the same
        res, _ = self.run_src("twilio", {}, {}, '[sources.twilio]\nenv_file = "/nonexistent/.env"')
        self.assertEqual(res["alerts"][0]["sev"], "warn")  # an env_file was named: missing credentials are news

    def test_bad_sid_partial_data_and_scope_setting(self):
        res, rt = self.run_src("twilio", {"TWILIO_ACCOUNT_SID": "../../etc", "TWILIO_AUTH_TOKEN": CANARY}, {})
        self.assertEqual((res["status"], rt.calls), ("unknown", []))
        r = self.routes()
        r[("GET", TW + "/Usage/Records/Today.json")] = (500, {})
        res, _ = self.run_src("twilio", self.ENV, r)
        self.assertEqual(res["status"], "warn")
        self.assertEqual(registry.SOURCES["twilio"].scope, "global")
        # The scope override now lives in the registry (for every source), not in an import-time read.
        src = registry.SOURCES["twilio"]
        local = support.host('[sources.twilio]\nscope = "local"\nshare = true')
        self.assertEqual((registry.scope_of(src, local), registry.shares(src, local)), ("local", True))
        self.assertEqual((registry.scope_of(src, support.host("")), registry.shares(src, support.host(""))), ("global", False))


# ── Guard rails: read-only audit, leak fuzzing, collector integration ──────
FUZZ_ENV = {k: CANARY for k in ("CF_GLOBAL_API_KEY", "VERCEL_TOKEN", "UPTIMEROBOT_API_KEY", "HCLOUD_TOKEN", "SENTRY_AUTH_TOKEN",
                                "STRIPE_SECRET_KEY", "RESEND_API_KEY", "OPENROUTER_MGMT_KEY", "ANTHROPIC_ADMIN_KEY",
                                "OPENAI_ADMIN_KEY", "XAI_MGMT_KEY", "GH_TOKEN", "LANGFUSE_SECRET_KEY", "TWILIO_AUTH_TOKEN")}
FUZZ_ENV.update({"CF_EMAIL": "ops@example.com", "CLOUDFLARE_ACCOUNT_ID": ACCT, "CF_ZONE_ID": Z1, "LANGFUSE_HOST": "http://lf.example:3000",
                 "LANGFUSE_PUBLIC_KEY": "pk-lf-public", "TWILIO_ACCOUNT_SID": SID, "AWS_CONFIG_FILE": "/nonexistent"})
FUZZ_TOML = '[sources.github]\nrepos = ["acme/api"]'


class GuardRails(unittest.TestCase):
    def test_source_audit_only_read_only_verbs_and_commands(self):
        """A future edit must not add a mutating verb, a second transport or a new subprocess unnoticed."""
        src = ""
        for path in (SAAS_PY, SAAS_PY.replace("saas.py", "saas_accounts.py")):  # the module was split in two
            with open(path, encoding="utf-8") as f:
                src += f.read()
        verbs = re.findall(r'_req\(ctx, "([A-Z]+)"', src)
        self.assertEqual(set(verbs), {"GET", "POST"})
        self.assertEqual(verbs.count("POST"), 3)  # Cloudflare GraphQL, GitHub GraphQL, UptimeRobot v2 getMonitors
        self.assertEqual(re.findall(r'"(?:PUT|PATCH|DELETE)"', src), [])
        self.assertEqual(src.count("ctx.http("), 1)  # only inside _req, which always passes redirects=False
        self.assertIn("redirects=False", src)
        self.assertIsNone(re.search(r"^\s*(?:import|from)\s+(?:subprocess|socket|http\.client|urllib\.request|requests)\b", src, re.M))
        for banned in ("core.http(", "urlopen(", "os.system(", "os.popen(", "Popen(", "core.run("):
            self.assertNotIn(banned, src)
        self.assertEqual(len(re.findall(r"ctx\.run\(", src)), 2)  # gh auth token, and _aws
        self.assertIn('ctx.run([gh, "auth", "token"]', src)
        aws = set(re.findall(r'_aws\(ctx, \["(\w+)", "([\w-]+)"', src))
        self.assertEqual(aws, {("sts", "get-caller-identity"), ("ec2", "describe-instances"), ("ce", "get-cost-and-usage")})

    def test_every_source_is_registered_global_and_documented(self):
        with open(os.path.join(support.ROOT, "docs", "reference", "saas.md"), encoding="utf-8") as f:
            doc = f.read()
        for name in SOURCES:
            s = registry.SOURCES[name]
            self.assertEqual(s.scope, "global", name)
            self.assertTrue(s.desc, name)
            self.assertIn("## `%s`" % name, doc)
            for key in s.defaults:
                self.assertIn(key, doc, "%s.%s is undocumented" % (name, key))

    def fuzz(self, fake, run_out=(1, "", CANARY)):
        """Every source against one fake answer for every URL; none may raise or leak."""
        out = {}
        h = host(FUZZ_TOML)
        with mock.patch.object(saas_core, "_gh_cli_token", return_value=None), mock.patch.object(saas_accounts, "_aws_bin", return_value="/fake/aws"), \
                mock.patch.object(saas_core, "_now", lambda: NOW):
            for name in SOURCES:
                c = ctx(h, registry.SOURCES[name].config(h), FUZZ_ENV, None, fake)
                c.run = lambda *a, **k: run_out
                res = registry.SOURCES[name].fn(c)
                assert_shape(self, res)
                self.assertNotIn(CANARY, json.dumps(res), name)
                self.assertNotIn("ops@example.com", json.dumps(res), name)
                out[name] = res
        return out

    def test_garbage_errors_and_wrong_shapes_never_raise_or_leak(self):
        for mode in ("garbage", "error", "empty", "list", "none"):
            def fake(url, method="GET", **kw):
                self.assertNotIn(CANARY, url)
                if mode == "garbage":
                    return core.Resp(200, "<html>oops " + CANARY + "</html>", 1)
                if mode == "error":
                    return core.Resp(500, {"message": "boom " + CANARY, "errors": [{"message": CANARY}]}, 1)
                if mode == "list":
                    return core.Resp(200, [1, 2, 3], 1)
                if mode == "none":
                    return core.Resp(204, None, 1)
                return core.Resp(200, {}, 1)
            self.fuzz(fake)
            with mock.patch.object(saas_core, "_scrubbed", lambda obj, vals: obj):
                self.fuzz(fake)  # the fetchers' own hygiene, without the final scrub

    def test_deeply_nested_junk_degrades_without_internal_errors(self):
        bad = [1, None, "x", [], {"targets": 5, "status": 5, "price": 5, "recurring": 5, "items": 5, "location": 5, "stats": 5,
                                  "storage_box_type": 5, "server_type": 5, "links": 5, "amount": 5, "dimensions": 5, "uniq": 5, "sum": 5}]
        junk = {k: bad for k in ("projects", "data", "monitors", "servers", "volumes", "storage_boxes", "workflow_runs", "usage_records",
                                 "results", "available", "pending", "responseStatusMap", "emailRoutingAdaptiveGroups",
                                 "httpRequests1hGroups", "zones", "prices", "items", "result")}
        junk.update({"pagination": 5, "meta": 5, "stat": "ok", "total": 5, "balance": "x", "viewer": 5, "pricing": 5, "status": "OK",
                     "teamId": TEAM, "has_more": False, "version": 5, "success": True})

        def fake(url, method="GET", **kw):
            return core.Resp(200, bad if url.split("?")[0].endswith("/organizations/") else junk, 1)
        for name, res in self.fuzz(fake, run_out=(0, json.dumps(bad), "")).items():
            self.assertNotEqual(res["summary"], "internal error", "%s raised on nested junk: %s" % (name, res["error"]))

    def test_transport_failures_everywhere(self):
        def fake(url, method="GET", **kw):
            raise TimeoutError("timed out")
        for name, res in self.fuzz(fake).items():
            if name not in ("aws_account",):
                self.assertIn(res["status"], ("unknown", "crit"), name)  # langfuse unreachable is crit; the rest soft-fail

    def test_the_collector_keeps_the_last_good_reading_through_a_soft_failure(self):
        h = host()
        core.ensure_dirs()
        good = Router({("GET", VP): (200, {"projects": [vproj("web", "READY", hours=1)]})})
        with mock.patch.object(registry.Ctx, "http", staticmethod(good)), mock.patch.dict(os.environ, {"VERCEL_TOKEN": CANARY}), \
                mock.patch.object(collect.secrets, "_ENV", None):
            rec = collect.run_one(registry.SOURCES["vercel"], h, {"VERCEL_TOKEN": CANARY})
            self.assertTrue(rec["ok"])
            bad = Router({("GET", VP): (401, {"error": {"message": "token expired"}})})
            with mock.patch.object(registry.Ctx, "http", staticmethod(bad)):
                rec = collect.run_one(registry.SOURCES["vercel"], h, {"VERCEL_TOKEN": CANARY})
        self.assertEqual((rec["ok"], rec["soft"], rec["kept"]), (False, True, True))
        self.assertEqual(rec["data"]["summary"], "1 projects, prod ok (1 READY, 0 building)")
        self.assertEqual(rec["error"], "vercel HTTP 401: token expired")
        stored = core.read_text(registry.src_path("vercel"))
        self.assertNotIn(CANARY, stored)
        rec = collect.run_one(registry.SOURCES["vercel"], h, {})
        self.assertEqual((rec["skip"], rec["error"]), (True, "missing env: VERCEL_TOKEN"))  # needs= gates it before any call


# ── Panels ──────────────────────────────────────────────────────────────────
PANELS = ("edge", "infra", "comms", "money")
SITES = {"rows": [{"label": "www", "url": "https://example.com", "status": 200, "ms": 142, "up": True, "slow": False},
                  {"label": "api", "url": "https://api.example.com/health", "status": 200, "ms": 2311, "up": True, "slow": True,
                   "info": "v2.4.1"},
                  {"label": "docs", "url": "https://docs.example.com", "status": 503, "ms": 90, "up": False, "slow": False,
                   "error": "HTTP 503"}],
         "up": 2, "total": 3, "alerts": [{"sev": "crit", "msg": "docs down (HTTP 503)"}]}
CURSOR = {"credits": {"on_demand_used_usd": 81.49, "included_usd": 20.0}}


def demo():
    with open(DEMO, encoding="utf-8") as f:
        return json.load(f)


class Panels(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.addCleanup(support.clear_cache)
        self.addCleanup(T.set_mode, "truecolor")
        self.addCleanup(T.set_ascii, False)

    def load(self, data=None):
        for name, d in (data or demo()).items():
            put(name, d)
        put("sites", SITES)
        put("cursor_usage", CURSOR)

    def build(self, key, cw=90):
        return BUILDERS[key](RCtx(host(), registry.Cache(), {"vitals": {}}, [], cw))

    def text(self, key, cw=130):
        return T.strip("\n".join(self.build(key, cw).lines))

    def test_every_panel_fits_its_column_at_every_width_and_color_depth(self):
        self.load()
        for mode, ascii_ in (("truecolor", False), ("256", False), ("16", False), ("none", True)):
            T.set_mode(mode)
            T.set_ascii(ascii_)
            for cw in (60, 90, 130):
                for key in PANELS:
                    p = self.build(key, cw)
                    self.assertIsNotNone(p, key)
                    for ln in p.render(cw, T.ACCENT):
                        self.assertLessEqual(T.width(ln), cw, "%s at %d (%s): %r" % (key, cw, mode, T.strip(ln)))
                        if ascii_:  # a non-UTF-8 terminal gets plain ASCII: glyphs, separators, currency
                            self.assertTrue(all(ord(ch) < 128 for ch in ln), "%s: %r" % (key, ln))

    def test_the_full_layout_fits_the_terminal(self):
        self.load()
        for width in (60, 90, 130):
            out = render.render(host(), registry.Cache(), width=width, only=list(PANELS))
            for ln in out.splitlines():
                self.assertLessEqual(T.width(ln), width)
            for title in ("EDGE", "INFRA", "COMMS", "MONEY"):
                self.assertIn(title, T.strip(out))

    def test_no_data_or_no_credentials_means_no_panel(self):
        for key in PANELS:
            self.assertIsNone(self.build(key), key)
        for name in SOURCES:
            put(name, saas._missing("X_TOKEN"), ok=False, soft=True)
        for key in PANELS:
            self.assertIsNone(self.build(key), key)

    def test_a_reason_without_data_shows_dimmed_and_raises_no_stale_note(self):
        put("resend", {"status": "unknown", "summary": "send-only key: cannot read domains or emails",
                       "details": {"key_valid": True, "can_read": False}, "error": "restricted_api_key"}, ok=False, soft=True)
        p = self.build("comms")
        self.assertIn("send-only key", T.strip("\n".join(p.lines)))
        self.assertEqual((p.note, p.level), ("", "ok"))

    def test_kept_data_from_a_failing_source_is_named_in_the_note(self):
        d = demo()
        put("cloudflare", d["cloudflare"], ok=False, last_ok=time.time() - 7200)  # the last good reading, kept
        put("vercel", d["vercel"])
        p = self.build("edge")
        self.assertTrue(p.note.startswith("cloudflare ") and p.note.endswith("2h"), p.note)

    def test_rows_and_levels_follow_the_data(self):
        self.load()
        edge, infra, comms, money = (self.text(k) for k in PANELS)
        for needle in ("11/12 monitors up", "1 paused", "3/3 up", "2 retired", "48k req", "61 5xx (0.1%)", "4 ready", "1 building",
                       "www 142", "docs HTTP 503"):
            self.assertIn(needle, edge)
        for needle in ("web-1 cpx31", "backups", "62%", "api", "5 open PRs by octocat", "1 changes requested", "3 unresolved",
                       "prod", "4821", "3 EC2 running"):
            self.assertIn(needle, infra)
        for needle in ("2/2 domains verified", "41 emails 24h", "routing 1/2 ready", "14 delivered", "example.org misconfigured",
                       "12 SMS today"):
            self.assertIn(needle, comms)
        for needle in ("$1.3k available", "42 active", "MRR $1.6k", "anthropic $38.20", "openai $12.75", "= $50.95", "$7.40 left",
                       "BillingRead", "~$61.37/mo", "$212 month to date", "$81.49 on-demand this cycle", "$63.80 balance"):
            self.assertIn(needle, money)
        self.assertEqual([self.build(k).level for k in PANELS], ["crit", "warn", "warn", "warn"])  # docs is down: crit

    def test_failures_turn_the_panels_red(self):
        d = demo()
        gh = d["github"]
        gh["status"], gh["details"]["repos"][0]["level"] = "crit", "crit"
        gh["details"]["repos"][0]["ci_failing"] = ["Deploy production"]
        tw = d["twilio"]
        tw["status"], tw["details"]["balance"] = "crit", {"amount": 3.1, "currency": "EUR", "level": "crit"}
        cf = d["cloudflare"]
        cf["details"]["tunnels"].update(healthy=2, down_recent=["acme-edge"])
        cf["details"]["traffic_zones"][1].update(pct_5xx=7.5, level="crit")
        self.load(d)
        self.assertEqual(self.build("infra").level, "crit")
        self.assertIn("Deploy production", self.text("infra"))
        self.assertEqual(self.build("money").level, "crit")
        self.assertIn("€3.10 balance", self.text("money"))
        self.assertEqual(self.build("edge").level, "crit")
        self.assertIn("down: acme-edge", self.text("edge"))
        self.assertIn("example.org 7.5% 5xx", self.text("edge").replace("%s " % T.g("crit"), ""))
        T.set_ascii(True)
        self.assertIn("3.10 EUR balance", self.text("money"))

    def test_malformed_records_never_break_a_panel(self):
        """A record kept from another version, or a hand-edited cache, may hold anything."""
        class Mem:  # an in-memory cache: thousands of builds in well under a second
            def __init__(self, recs):
                self.recs = recs

            def rec(self, name):
                return {"ok": True, "ts": time.time(), "data": self.recs[name]} if name in self.recs else None

            def data(self, name, default=None):
                return self.recs.get(name, default)

        def paths(obj, path=()):
            items = obj.items() if isinstance(obj, dict) else enumerate(obj[:2]) if isinstance(obj, list) else ()
            for k, v in items:
                yield path + (k,)
                yield from paths(v, path + (k,))
        base = dict(demo(), sites=SITES, cursor_usage=CURSOR)
        h, builds = host(), 0
        for name in base:
            for path in list(paths(base[name])) + [()]:
                for junk in (5, "x", None, [1, "a", None, {"k": 5}], {"k": 5}, True):
                    recs = json.loads(json.dumps(base))
                    if path:
                        node = recs[name]
                        for part in path[:-1]:
                            node = node[part]
                        node[path[-1]] = junk
                    else:
                        recs[name] = junk
                    for key in PANELS:
                        p = BUILDERS[key](RCtx(h, Mem(recs), {"vitals": {}}, [], 90))
                        if p:
                            p.render(90, T.ACCENT)
                        builds += 1
        self.assertGreater(builds, 5000)


# ── Demo data ───────────────────────────────────────────────────────────────
class Demo(Base):
    def live(self):
        """Each source's real output from the fixtures above: the shapes the demo copies."""
        self.calls = []
        fd, cfg = tempfile.mkstemp()
        os.write(fd, AWS_CFG.encode())
        os.close(fd)
        self.addCleanup(os.unlink, cfg)
        ai = or_routes()
        ai.update(xai_routes())
        ai.update({("GET", AN): an_page(15, "391"), ("GET", OA): oa_page(15, 6.18)})
        with mock.patch.object(saas_core, "_gh_cli_token", return_value=None), mock.patch.object(saas_accounts, "_aws_bin", return_value="/fake/aws"):
            return {
                "cloudflare": self.run_src("cloudflare", CF_KEY_ENV, cf_routes())[0],
                "vercel": self.run_src("vercel", {"VERCEL_TOKEN": CANARY}, {("GET", VP): (200, {"projects": [vproj("a", "READY", hours=1)]})})[0],
                "uptimerobot": self.run_src("uptimerobot", UptimeRobot.ENV, {("GET", V3): (200, {"data": [m3("a", "UP")]})})[0],
                "hetzner": self.run_src("hetzner", Hetzner.ENV, hz_routes())[0],
                "github": self.run_src("github", {"GH_TOKEN": "gho_good"}, GitHub.routes(self, [gh_run("x", "success", hours=1)]), GH_TOML)[0],
                "sentry": self.run_src("sentry", Sentry.ENV, Sentry.routes(self, [{"project": "api", "count_unique(issue)": 1, "count()": 3}]))[0],
                "stripe": self.run_src("stripe", Stripe.ENV, Stripe.routes(self, [sub("active")]))[0],
                "resend": self.run_src("resend", RESEND_ENV, Resend.routes(self, [{"name": "example.com", "status": "verified"}],
                                                                           (200, {"data": [email("delivered", hours=1)]})))[0],
                "langfuse": self.run_src("langfuse", dict(LF_ENV, LANGFUSE_BASE_URL=""), lf_routes(), LF_TWO)[0],
                "ai_spend": self.run_src("ai_spend", AI_ENV, ai)[0],
                "aws_account": self.run_src("aws_account", {"AWS_CONFIG_FILE": cfg}, {}, "[sources.aws_account]\ncost_explorer = true",
                                            run=AWS.fake(self, ec2={"eu-central-1": [{"n": "a"}]}))[0],
                "twilio": self.run_src("twilio", Twilio.ENV, Twilio.routes(self))[0],
            }

    def test_the_demo_covers_every_source_in_its_real_shape(self):
        d, live = demo(), self.live()
        self.assertEqual(set(d), set(SOURCES))
        nested = {"cloudflare": ("tunnels", "traffic_24h", "zones", "email_routing"), "stripe": ("subscriptions",),
                  "resend": ("emails_24h",), "twilio": ("balance",), "ai_spend": ("openrouter", "anthropic", "openai", "xai")}
        rows = {"cloudflare": ("traffic_zones",), "hetzner": ("servers", "storage_boxes"), "github": ("repos",), "langfuse": ("projects",),
                "aws_account": ("accounts",), "resend": ("domains",)}
        for name in SOURCES:
            rec, real = d[name], live[name]
            assert_shape(self, rec)
            self.assertEqual(saas._normalize(rec), {k: v for k, v in rec.items()}, name)  # already in the normalized form
            dd, ld = rec["details"], real["details"]
            self.assertEqual(set(dd) - set(ld), set(), "%s: demo-only detail keys" % name)
            for key in nested.get(name, ()):
                self.assertEqual(set(dd[key]) - set(ld[key]), set(), "%s.%s" % (name, key))
            for key in rows.get(name, ()):
                self.assertEqual({k for it in dd[key] for k in it} - {k for it in ld[key] for k in it}, set(), "%s.%s[]" % (name, key))
        er = d["cloudflare"]["details"]["email_routing"]
        self.assertEqual({k for z in er["zones"] for k in z}, {k for z in live["cloudflare"]["details"]["email_routing"]["zones"] for k in z})

    def test_the_demo_is_a_believable_mix_and_entirely_fictional(self):
        d = demo()
        statuses = sorted(rec["status"] for rec in d.values())
        self.assertGreaterEqual(statuses.count("ok"), 7)
        self.assertGreaterEqual(statuses.count("warn"), 3)
        text = json.dumps(d)
        for host_ in re.findall(r"[a-z0-9.-]+\.(?:com|org|net|io|ai|dev)\b", text):
            self.assertTrue(host_.endswith(("example.com", "example.org", "example.net")) or host_ == "console.x.ai", host_)
        self.assertIsNone(re.search(r"\b\d{12}\b", text))  # no full AWS account id


if __name__ == "__main__":
    unittest.main()

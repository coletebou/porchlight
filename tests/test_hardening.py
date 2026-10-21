"""Regression tests for the pre-release review: credential handling, failure
retention, service states, login-time network use, backups and money."""
import os
import time
import unittest
import urllib.request
from unittest import mock

import support  # noqa: F401 — must be first
from support import ctx, host

from porchlight import alerts, collect, core, live, registry, secrets
from porchlight.sources import custom, saas_accounts, system


def _redirect_handlers(**kw):
    seen = []
    real = urllib.request.OpenerDirector.open

    def fake_open(opener, req, *a, **k):
        seen.append(sorted(type(h).__name__ for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)))
        raise OSError("stop")
    urllib.request.OpenerDirector.open = fake_open
    try:
        with unittest.TestCase().assertRaises(OSError):
            core.http("https://example.com/", **kw)
    finally:
        urllib.request.OpenerDirector.open = real
    return seen[0]


class Credentials(unittest.TestCase):
    def test_explicit_redirects_never_override_credentials(self):
        self.assertEqual(_redirect_handlers(headers={"Authorization": "Bearer x"}, redirects=True), ["NoRedirect"])
        self.assertEqual(_redirect_handlers(redirects=True), ["HTTPRedirectHandler"])

    def test_short_letter_passwords_are_masked(self):
        self.assertNotIn("hunterxy", secrets.redact("login failed for hunterxy", {"DB_PASSWORD": "hunterxy"}))
        self.assertIn("subscription", secrets.redact("auth subscription", {"CLAUDE_AUTH": "subscription"}))

    def test_child_env_keeps_aws_settings_only(self):
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_x_1234567890", "AWS_PROFILE": "dev", "HOME_X": "1"}):
            env = secrets.child_env({"AWS_REGION": "eu-west-1", "OPENAI_ADMIN_KEY": "k" * 30,
                                     "AWS_BEARER_TOKEN_BEDROCK": "b" * 30}, keep=("AWS_",))
        self.assertNotIn("STRIPE_SECRET_KEY", env)
        self.assertNotIn("OPENAI_ADMIN_KEY", env)
        self.assertNotIn("AWS_BEARER_TOKEN_BEDROCK", env)
        self.assertEqual((env["AWS_PROFILE"], env["AWS_REGION"], env["HOME_X"]), ("dev", "eu-west-1", "1"))

    def test_custom_command_output_is_masked_before_caching(self):
        h = host('[[custom]]\nname = "leaky"\ncommand = "echo token=$LEAKY_TOKEN"\n')
        custom.register(h)
        try:
            src = registry.SOURCES["leaky"]
            secret = "Zq9" + "x" * 30
            d = src.fn(ctx(h, src.config(h), env=dict(os.environ, LEAKY_TOKEN=secret)))
            self.assertNotIn(secret, repr(d))
        finally:
            registry.SOURCES.pop("leaky", None)

    def test_register_all_marks_the_config_not_its_id(self):
        from porchlight import sources
        h1 = host('[[custom]]\nname = "one"\ncommand = "true"\n')
        sources.register_all(h1)
        self.assertTrue(getattr(h1.cfg, "_porchlight_registered", False))
        h2 = host('[[custom]]\nname = "two"\ncommand = "true"\n')
        sources.register_all(h2)
        self.assertIn("two", registry.SOURCES)
        registry.SOURCES.pop("one", None)
        registry.SOURCES.pop("two", None)


class Retention(unittest.TestCase):
    def test_hard_then_soft_failure_keeps_the_good_reading(self):
        support.clear_cache()
        state = {"n": 0}

        def fn(ctx_):
            state["n"] += 1
            if state["n"] == 1:
                return {"windows": [{"label": "5h"}]}
            if state["n"] == 2:
                raise OSError("network down")
            return {"error": "login expired", "windows": []}
        registry.source("flappy")(fn)
        try:
            h = host("")
            for _ in range(3):
                rec = collect.run_one(registry.SOURCES["flappy"], h, {})
            self.assertEqual(rec["data"]["windows"][0]["label"], "5h")
        finally:
            registry.SOURCES.pop("flappy")


class Services(unittest.TestCase):
    def lv(self, units):
        v = {"cores": 4, "load": [0, 0, 0], "mem": {"used": 1, "total": 10}, "swap": {"used": 0, "total": 0},
             "disks": [], "stalled": []}
        return {"vitals": v, "units": units, "failed": [], "launchd": [], "tmux": {}}

    def test_optional_services_do_not_alert(self):
        support.clear_cache()
        h = host('[services]\noptional = ["dev-db"]\n')
        found = alerts.gather(h, registry.Cache(), self.lv([("dev-db", "crit", "inactive", ["dev-db.service"])]))
        self.assertFalse([a for a in found if "dev-db" in a["msg"]])

    def test_configured_unit_that_vanished_is_reported(self):
        h = host('[services]\nsystemd = ["gone"]\n')
        with mock.patch.object(core, "IS_MAC", False), mock.patch.object(core, "which", return_value="/bin/systemctl"), \
                mock.patch.object(live, "_systemctl_show", return_value={"gone.service": {"Id": "gone.service", "LoadState": "not-found"}}):
            out = live.units(h)
        self.assertEqual(out[0][:3], ("gone", "crit", "not found"))

    def test_named_ports_are_not_probed_at_login(self):
        with mock.patch("socket.create_connection", side_effect=AssertionError("network at login")):
            self.assertEqual(live.ports([(5432, "db", "db.example.com")]), [(5432, "db", False)])


class Certs(unittest.TestCase):
    def test_transport_failure_needs_two_strikes_bad_cert_is_crit(self):
        import ssl
        h = host()
        cfg = {"targets": ["example.com"], "warn_days": 21, "crit_days": 10}
        with mock.patch.object(system, "cert_days", side_effect=OSError("no route")):
            d1 = system.certs(ctx(h, cfg))
            d2 = system.certs(ctx(h, cfg, prev=d1))
        self.assertNotIn("alerts", d1)
        self.assertEqual(d2["alerts"][0]["sev"], "warn")
        bad = ssl.SSLCertVerificationError("certificate verify failed")
        with mock.patch.object(system, "cert_days", side_effect=bad):
            self.assertEqual(system.certs(ctx(h, cfg))["alerts"][0]["sev"], "crit")


class Backups(unittest.TestCase):
    def test_learned_cadence_catches_missed_runs(self):
        now = time.time()
        cfg = {"units": ["nightly"], "files": []}
        prev = {"rows": [{"name": "nightly", "last": now - 4 * 86400}], "cadence": {"nightly": 86400}}
        runs = {"nightly": {"result": "success", "last": now - 3 * 86400, "next": now + 3600}}
        with mock.patch.object(system, "unit_runs", return_value=runs), mock.patch.object(core, "which", return_value="/bin/x"):
            d = system.backups(ctx(host(), cfg, prev=prev))
        self.assertTrue(any("overdue" in a["msg"] for a in d.get("alerts") or []))
        self.assertEqual(d["cadence"]["nightly"], 86400)


class Money(unittest.TestCase):
    def test_zero_and_three_decimal_currencies(self):
        self.assertEqual(saas_accounts._unit("jpy"), 1.0)
        self.assertEqual(saas_accounts._unit("KWD"), 1000.0)
        self.assertEqual(saas_accounts._unit("usd"), 100.0)
        self.assertEqual(saas_accounts._cents(10000, "jpy"), "10000.00 JPY")


if __name__ == "__main__":
    unittest.main()

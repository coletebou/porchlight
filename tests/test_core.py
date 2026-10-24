"""Core behavior: width math, config resolution, identities, cache ranking,
collector scheduling, alerts, and rendering at phone, laptop and wide widths."""
import os
import time
import unittest

import support  # noqa: F401 — must be first: isolates cache/config
from support import host, put

from porchlight import alerts, collect, config, core, font, registry, render, secrets, theme as T
from porchlight.sources import fleet, system  # noqa: F401


class Width(unittest.TestCase):
    def test_ansi_and_emoji_width(self):
        self.assertEqual(T.width(T.c("hello", "gold")), 5)
        self.assertEqual(T.width("☀️ 61°F"), 7)  # VS16 forces emoji width 2
        self.assertEqual(T.width("━━╸──"), 5)
        self.assertEqual(T.width("日本"), 4)

    def test_fit_and_pad_keep_width_and_escapes(self):
        s = T.fit(T.c("hello world", "gold"), 7)
        self.assertEqual(T.width(s), 7)
        self.assertTrue(T.strip(s).endswith("…"))
        self.assertEqual(T.width(T.pad(T.c("ab", "red"), 6, ">")), 6)
        self.assertEqual(T.width(T.pad("abc", 7, "^")), 7)

    def test_bar_is_exactly_w_cells(self):
        for p in (0, 1, 49.9, 50, 99.9, 100, 140, -5):
            self.assertEqual(T.width(T.bar(p, 12)), 12, p)

    def test_color_depths(self):
        for mode in ("truecolor", "256", "16", "none"):
            T.set_mode(mode)
            out = T.c("x", "amber")
            self.assertEqual(T.strip(out), "x")
            if mode == "none":
                self.assertEqual(out, "x")
        T.set_mode("truecolor")

    def test_256_grey_ramp_stays_in_range(self):
        for v in range(0, 256, 5):
            self.assertTrue(16 <= T._rgb256(v, v, v) <= 255)

    def test_ascii_mode_glyphs(self):
        T.set_ascii(True)
        try:
            self.assertEqual(T.strip(T.dot("ok")), "*")
            self.assertTrue(all(ord(ch) < 128 for ch in T.strip(T.bar(55, 10))))
            self.assertEqual(T.ellipsis(), "...")
        finally:
            T.set_ascii(False)

    def test_themes_configure(self):
        for name in T.THEMES:
            self.assertEqual(T.configure(name), [])
            self.assertEqual(len(T.ACCENT), 3)
        self.assertTrue(T.configure("nope"))
        self.assertTrue(T.configure("nord", colors={"green": "zzz"}))
        T.configure("porchlight")
        self.assertEqual(T.hexrgb("#fff"), (255, 255, 255))
        self.assertIsNone(T.hexrgb("#12"))


class Fonts(unittest.TestCase):
    def test_every_font_has_every_glyph_with_consistent_rows(self):
        for name, f in font.FONTS.items():
            rows = {len(g.split("|")) for g in f.values()}
            self.assertEqual(len(rows), 1, name)
            for ch, g in f.items():
                widths = {len(r) for r in g.split("|")}
                self.assertEqual(len(widths), 1, "%s %r" % (name, ch))

    def test_render_widths(self):
        for name in font.FONTS:
            lines = font.render("web-01.prod", name)
            self.assertLessEqual(max(T.width(ln) for ln in lines), font.width_of("web-01.prod", name))
        self.assertEqual(len(font.render("ab", "block")), 4)
        self.assertEqual(len(font.render("ab", "small")), 3)
        self.assertEqual(len(font.render("ab", "big")), 7)
        self.assertTrue(all(set(ln) <= {"#", " "} for ln in font.render("ab", "block", ascii=True)))


class Config(unittest.TestCase):
    TOML = """
[general]
identity = "personal"
label = "base"
[layout]
panels = ["system", "agents"]
[hosts."web-*"]
general.label = "web"
general.role = "server"
layout.panels = ["system"]
[hosts."web-1"]
general.label = "first"
[identities.work]
allow = ["procs", "docker"]
"""

    def test_host_globs_merge_in_file_order(self):
        h = host(self.TOML, "web-1")
        self.assertEqual(h.label, "first")
        self.assertEqual(h.role, "server")
        self.assertEqual(h.panels, ["system"])
        self.assertEqual(host(self.TOML, "db-1").label, "base")

    def test_unknown_and_mistyped_settings_are_reported(self):
        cfg = config.loads('[general]\nlabel = "x"\nbogus = 1\n[layout]\ncolumns = "three"\n[nope]\na = 1\n')
        joined = " | ".join(cfg.problems)
        self.assertIn("general.bogus", joined)
        self.assertIn("layout.columns should be a number", joined)
        self.assertIn("unknown setting nope", joined)

    def test_auto_panels_and_tags(self):
        h = host("")
        self.assertTrue(h.auto_panels)
        self.assertIn("#" + h.role, h.tags)
        self.assertIn("#default", h.tags)
        self.assertFalse(h.hub)
        self.assertTrue(host('[fleet]\npeers = ["a"]').hub)

    def test_env_overrides(self):
        os.environ["PORCHLIGHT_IDENTITY"] = "work"
        try:
            self.assertEqual(host("").identity, "work")
        finally:
            del os.environ["PORCHLIGHT_IDENTITY"]


class Identities(unittest.TestCase):
    def setUp(self):
        registry.source("t_personal", identities=("personal",))(lambda ctx: {})
        registry.source("t_any")(lambda ctx: {})
        registry.source("t_optin", optin=True)(lambda ctx: {})
        registry.source("t_needs_cfg", requires=("targets",), defaults={"targets": []})(lambda ctx: {})
        registry.source("t_bin", binaries=("definitely-not-a-real-binary-xyz",))(lambda ctx: {})

    def tearDown(self):
        for n in ("t_personal", "t_any", "t_optin", "t_needs_cfg", "t_bin"):
            registry.SOURCES.pop(n, None)

    def st(self, toml, name):
        return registry.state(registry.SOURCES[name], host(toml))

    def test_identity_scoping(self):
        self.assertEqual(self.st('[general]\nidentity = "work"', "t_personal"), "identity")
        self.assertEqual(self.st('[general]\nidentity = "personal"', "t_personal"), "on")

    def test_allowlist_denylist(self):
        toml = '[general]\nidentity = "work"\n[identities.work]\nallow = ["t_any"]\n'
        self.assertEqual(self.st(toml, "t_any"), "on")
        self.assertEqual(self.st(toml, "t_optin"), "not allowed")
        toml = '[general]\nidentity = "work"\n[identities.work]\ndeny = ["t_any"]\n'
        self.assertEqual(self.st(toml, "t_any"), "denied")
        self.assertFalse(registry.serves(registry.SOURCES["t_any"], host(toml)))

    def test_optin_requires_binaries_disabled(self):
        self.assertEqual(self.st("", "t_optin"), "opt-in")
        self.assertEqual(self.st("[sources.t_optin]\n", "t_optin"), "on")
        self.assertEqual(self.st("", "t_needs_cfg"), "needs targets")
        self.assertEqual(self.st('[sources.t_needs_cfg]\ntargets = ["x"]', "t_needs_cfg"), "on")
        self.assertEqual(self.st("", "t_bin"), "no definitely-not-a-real-binary-xyz")
        self.assertEqual(self.st("[sources.t_any]\nenabled = false", "t_any"), "disabled")

    def test_host_filters(self):
        registry.source("t_hub", hosts=("#hub",))(lambda ctx: {})
        try:
            self.assertEqual(self.st("", "t_hub"), "not #hub")
            self.assertEqual(self.st('[fleet]\npeers = ["x"]', "t_hub"), "on")
        finally:
            registry.SOURCES.pop("t_hub")


class CacheRanking(unittest.TestCase):
    def setUp(self):
        support.clear_cache()

    def test_soft_error_never_shadows_good_hub_data(self):
        put("claude_usage", {"error": "token expired", "windows": []}, age=0)
        core.write_json_atomic(core.BUNDLE_PATH, {"ts": time.time(), "sources": {"claude_usage": {
            "ts": time.time() - 300, "ok": True, "data": {"windows": [{"label": "5h", "used_pct": 10}]}}}})
        self.assertEqual(registry.Cache().data("claude_usage")["windows"][0]["used_pct"], 10)

    def test_newest_wins_among_equals(self):
        put("weather", {"temp": 1}, age=10)
        core.write_json_atomic(core.BUNDLE_PATH, {"sources": {"weather": {"ts": time.time(), "ok": True, "data": {"temp": 2}}}})
        self.assertEqual(registry.Cache().data("weather")["temp"], 2)

    def test_peer_shared_records(self):
        put("fleet", {"hosts": {"db-1": {"ok": True, "snap": {"shared": {"mongodb": {
            "ts": time.time(), "ok": True, "data": {"summary": "primary up"}}}}}}})
        self.assertEqual(registry.Cache().data("mongodb")["summary"], "primary up")


class Collector(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.h = host("")

    def test_due_ttl_retry_skip(self):
        src = registry.Source("x", None, 600, 5, "local", None, (), None, None, "", "*", {}, (), False, False, ())
        now = time.time()
        self.assertTrue(collect.is_due(src, self.h, now, registry.Cache()))
        put("x", {}, ok=True, age=100)
        self.assertFalse(collect.is_due(src, self.h, now, registry.Cache()))
        put("x", {}, ok=False, age=200)
        self.assertTrue(collect.is_due(src, self.h, now, registry.Cache()))  # retry = min(ttl, 180)
        put("x", {}, ok=False, age=200, skip=True)
        self.assertFalse(collect.is_due(src, self.h, now, registry.Cache()))  # missing env: recheck after 6h

    def test_peer_defers_to_fresh_hub_copy(self):
        src = registry.Source("g", None, 600, 5, "global", None, (), None, None, "", "*", {}, (), False, False, ())
        core.write_json_atomic(core.BUNDLE_PATH, {"sources": {"g": {"ts": time.time() - 60, "ok": True, "data": {}}}})
        self.assertFalse(collect.is_due(src, self.h, time.time(), registry.Cache()))
        core.write_json_atomic(core.BUNDLE_PATH, {"sources": {"g": {"ts": time.time() - 9000, "ok": True, "data": {}}}})
        self.assertTrue(collect.is_due(src, self.h, time.time(), registry.Cache()))

    def test_soft_failure_keeps_last_good_reading(self):
        state = {"n": 0}

        def fn(ctx):
            state["n"] += 1
            return {"windows": [{"label": "5h"}]} if state["n"] == 1 else {"error": "expired", "windows": []}
        registry.source("softy")(fn)
        try:
            s = registry.SOURCES["softy"]
            collect.run_one(s, self.h, {})
            rec = collect.run_one(s, self.h, {})
            self.assertFalse(rec["ok"])
            self.assertTrue(rec["kept"])
            self.assertEqual(rec["data"]["windows"][0]["label"], "5h")
        finally:
            registry.SOURCES.pop("softy")

    def test_exceptions_are_redacted(self):
        secret = "sk-ant-api03-" + "A1b2C3d4" * 6

        def boom(ctx):
            raise RuntimeError("bad key %s" % secret)
        registry.source("boom", needs=())(boom)
        try:
            rec = collect.run_one(registry.SOURCES["boom"], self.h, {"ANTHROPIC_API_KEY": secret})
            self.assertNotIn(secret, rec["error"])
            self.assertIn("redacted", rec["error"])
        finally:
            registry.SOURCES.pop("boom")

    def test_missing_env_is_a_skip(self):
        registry.source("needy", needs=("SOME_TOKEN_X",))(lambda ctx: {"x": 1})
        try:
            rec = collect.run_one(registry.SOURCES["needy"], self.h, {})
            self.assertTrue(rec["skip"])
            self.assertIn("SOME_TOKEN_X", rec["error"])
        finally:
            registry.SOURCES.pop("needy")


class Transport(unittest.TestCase):
    def test_verify_false_replaces_the_default_https_handler(self):
        """verify=False must win with redirects off too (the default for credentialed calls)."""
        import ssl
        import urllib.request
        seen = []
        real = urllib.request.OpenerDirector.open

        def fake_open(opener, req, *a, **k):
            https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
            seen.append([getattr(h, "_context", None) for h in https])
            raise OSError("stop before the network")
        urllib.request.OpenerDirector.open = fake_open
        try:
            for redirects in (False, True):
                with self.assertRaises(OSError):
                    core.http("https://example.com/", verify=False, redirects=redirects)
        finally:
            urllib.request.OpenerDirector.open = real
        for ctxs in seen:
            self.assertEqual(len(ctxs), 1)  # exactly one HTTPS handler: ours
            self.assertEqual(ctxs[0].verify_mode, ssl.CERT_NONE)

    def test_credentialed_requests_do_not_follow_redirects(self):
        import urllib.request
        kinds = []
        real = urllib.request.OpenerDirector.open

        def fake_open(opener, req, *a, **k):
            kinds.append(sorted(type(h).__name__ for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)))
            raise OSError("stop")
        urllib.request.OpenerDirector.open = fake_open
        try:
            for hdrs in ({"Authorization": "Bearer x"}, {}):
                with self.assertRaises(OSError):
                    core.http("https://example.com/", headers=hdrs)
        finally:
            urllib.request.OpenerDirector.open = real
        self.assertEqual(kinds, [["NoRedirect"], ["HTTPRedirectHandler"]])


class AutoUnits(unittest.TestCase):
    def test_present_units_are_remembered(self):
        from unittest import mock
        from porchlight import live
        support.clear_cache()
        calls = []

        def show(units, user=False):
            calls.append(list(units))
            return {"ssh.service": {"Id": "ssh.service", "LoadState": "loaded", "ActiveState": "active"},
                    "ssh": {"Id": "ssh.service", "LoadState": "loaded", "ActiveState": "active"}} if not user else {}
        h = host("")
        with mock.patch.object(core, "which", return_value="/bin/systemctl"), \
                mock.patch.object(live, "_systemctl_show", side_effect=show), \
                mock.patch.object(live, "_expand_globs", return_value={}), mock.patch.object(core, "IS_MAC", False):
            first = live.units(h)
            second = live.units(h)
        self.assertEqual([r[0] for r in first], [r[0] for r in second])
        self.assertGreater(len(calls[0]), 20)        # the first login asks about every candidate
        self.assertEqual(calls[2], ["ssh.service"])  # later ones only about what exists (calls[1] is the user scope)


class Secrets(unittest.TestCase):
    def test_redact_patterns(self):
        env = {"STRIPE_SECRET_KEY": "sk_live_" + "9" * 30, "PLAIN": "default"}
        uri = "mongodb://" + "user:pw" + "@db.example.com/x"  # built at runtime: secret scanners flag the literal
        text = "failed sk_live_%s and ghp_%s and %s and default" % ("9" * 30, "a" * 36, uri)
        out = secrets.redact(text, env)
        self.assertNotIn("9" * 30, out)
        self.assertNotIn("a" * 36, out)
        self.assertNotIn("user:pw", out)
        self.assertIn("default", out)

    def test_redact_masks_short_and_spaced_secrets_and_spares_words(self):
        env = {"DB_PASSWORD": "hunter7", "PG_PASS": "tab\tsep  arated pw", "X_TOKEN": "none",
               "SOME_AUTH": "subscription", "PIN_SECRET": "abc"}
        out = secrets.redact("pw=hunter7 tab\tsep  arated pw; token none; mode subscription; pin abc; "
                             "/mnt/disk-data down; key sk-abcdefghij", env)
        self.assertNotIn("hunter7", out)  # seven characters, under a strong name
        self.assertIn("<redacted:PG_PASS>", out)  # tabs and a run of spaces inside the value
        self.assertNotIn("arated", out)
        for kept in ("token none", "subscription", "pin abc", "/mnt/disk-data"):
            self.assertIn(kept, out)  # a placeholder, a setting, too short to mask, a path
        self.assertNotIn("abcdefghij", out)  # a token-shaped string is still cut
        self.assertNotIn("arated", secrets.redact("pw tab sep arated pw", env))  # the value arrived flattened
        self.assertNotIn("hunter7", secrets.mask("pw=hunter7\n", env))  # mask() uses the same rules
        self.assertNotIn("quiet", secrets.redact("pw amber\tquiet\nriver", {"DB_PASSWORD": "amber quiet river"}))
        settings = {"MAX_TOKENS": "4096", "TOKEN_TTL": "3600", "SECRET_BACKEND": "file", "SORT_KEY": "name",
                    "SUDO_PASSWORD": "abcd"}
        parsed = '{"max_tokens": 4096, "ttl": 3600, "backend": "file", "by": "name"}'
        self.assertEqual(secrets.mask(parsed, settings), parsed)  # settings under TOKEN/SECRET/_KEY names
        self.assertNotIn("abcd", secrets.redact("sudo abcd", settings))  # four characters under a password name

    def test_dotenv_parsing(self):
        p = os.path.join(support.TMP, "s.env")
        with open(p, "w") as f:
            f.write('export A="1 2"\nB=x # comment\n# C=3\nD=\'q\'\nbad line\nE=$HOME/x\n')
        d = core.dotenv(p)
        self.assertEqual(d["A"], "1 2")
        self.assertEqual(d["B"], "x")
        self.assertEqual(d["D"], "q")
        self.assertNotIn("C", d)
        self.assertEqual(d["E"], core.HOME + "/x")

    def test_files_win_over_inherited_env(self):
        p = os.path.join(support.TMP, "s2.env")
        with open(p, "w") as f:
            f.write("PL_TEST_KEY=fromfile\n")
        os.environ["PL_TEST_KEY"] = "stale"
        try:
            cfg = config.loads('[general]\nsecrets_files = ["%s"]' % p)
            self.assertEqual(secrets.load(cfg, refresh=True)["PL_TEST_KEY"], "fromfile")
        finally:
            del os.environ["PL_TEST_KEY"]
            secrets._ENV = None


def vitals(disk=50.0, mem=40.0, swap=0.0, load=0.5, cores=4):
    return {"cores": cores, "load": [load, load, load], "ip": "10.0.0.2", "os": "TestOS", "uptime": 3600,
            "mem": {"used": mem, "total": 100.0}, "swap": {"used": swap, "total": 100.0 if swap else 0},
            "disks": [{"mount": "/", "used": disk, "total": 100.0, "pct": disk}], "stalled": []}


class Alerts(unittest.TestCase):
    def setUp(self):
        support.clear_cache()

    def lv(self, **kw):
        return {"vitals": vitals(**kw), "units": [], "failed": [], "launchd": [], "tmux": {}}

    def test_thresholds_and_config(self):
        h = host("")
        found = alerts.gather(h, registry.Cache(), self.lv(disk=93, swap=85, load=9, cores=4))
        sev = {a["msg"].split()[0]: a["sev"] for a in found}
        self.assertEqual(sev["disk"], "crit")
        self.assertEqual(sev["swap"], "warn")
        self.assertEqual(sev["load"], "crit")
        h2 = host("[thresholds]\ndisk_crit = 99\ndisk_warn = 98\n")
        self.assertFalse([a for a in alerts.gather(h2, registry.Cache(), self.lv(disk=93)) if a["msg"].startswith("disk")])

    def test_source_alerts_ranked_deduped_and_muted(self):
        put("procs", {"alerts": [{"sev": "warn", "msg": "w1"}, {"sev": "crit", "msg": "c1"}, {"sev": "warn", "msg": "w1"}]})
        found = alerts.gather(host(""), registry.Cache(), self.lv())
        msgs = [a["msg"] for a in found]
        self.assertEqual(msgs[0], "c1")
        self.assertEqual(msgs.count("w1"), 1)
        muted = alerts.gather(host('[alerts]\nignore = ["^c1$"]'), registry.Cache(), self.lv())
        self.assertNotIn("c1", [a["msg"] for a in muted])

    def test_failed_unit_not_double_reported(self):
        lv = self.lv()
        lv["failed"] = ["nginx.service"]
        lv["units"] = [("nginx", "crit", "failed", ["nginx.service"])]
        found = alerts.gather(host(""), registry.Cache(), lv)
        self.assertEqual(len([a for a in found if "nginx" in a["msg"]]), 1)

    def test_peer_crit_alerts_imported_once(self):
        put("fleet", {"hosts": {"db-1": {"snap": {"label": "db-1", "ts": time.time(), "alerts": [
            {"sev": "crit", "msg": "disk / 95% full on db-1"}, {"sev": "warn", "msg": "meh"}]}}}})
        found = alerts.gather(host('[fleet]\npeers = ["db-1"]'), registry.Cache(), self.lv())
        self.assertIn("disk / 95% full on db-1", [a["msg"] for a in found])
        self.assertNotIn("meh", [a["msg"] for a in found])

    def test_new_since_last_visit_folds_digits(self):
        h = host("")
        render.visit(h, True, [{"sev": "warn", "msg": "swap 89% on x"}])
        found = [{"sev": "warn", "msg": "swap 90% on x"}, {"sev": "crit", "msg": "brand new"}]
        render.visit(h, False, found)
        self.assertFalse(found[0]["new"])
        self.assertTrue(found[1]["new"])


class Rendering(unittest.TestCase):
    """The demo fleet through the real renderer at every width class."""

    @classmethod
    def setUpClass(cls):
        from porchlight import demo
        cls.saved = core.CACHE_DIR
        cls.h, cls.cache, cls.tmp = demo.build()

    @classmethod
    def tearDownClass(cls):
        import shutil
        from porchlight import demo
        demo.restore()  # the demo swaps the live probes for fixtures; later tests need the real ones
        shutil.rmtree(cls.tmp, ignore_errors=True)
        core.use_cache_dir(cls.saved)

    def test_every_width_fits(self):
        for w in (40, 60, 72, 99, 100, 120, 150, 180, 220):
            out = render.render(self.h, self.cache, width=w)
            for line in out.splitlines():
                self.assertLessEqual(T.width(line), w, "width %d: %r" % (w, T.strip(line)))

    def test_compact_below_100(self):
        out = T.strip(render.render(self.h, self.cache, width=90))
        self.assertIn("compact view", out)
        self.assertNotIn("FILES", out)
        full = T.strip(render.render(self.h, self.cache, width=200))
        self.assertIn("FILES", full)
        self.assertIn("ATTENTION", full)

    def test_only_panels(self):
        out = T.strip(render.render(self.h, self.cache, width=130, only=["system"]))
        self.assertIn("SYSTEM", out)
        self.assertNotIn("ATTENTION", out)

    def test_no_color_has_no_escapes(self):
        T.set_mode("none")
        try:
            out = render.render(self.h, self.cache, width=150)
            self.assertNotIn("\x1b[", out)
        finally:
            T.set_mode("truecolor")

    def test_ascii_mode_is_pure_ascii(self):
        T.set_ascii(True)
        T.set_mode("none")
        try:
            for w in (60, 150):
                out = render.render(self.h, self.cache, width=w)
                self.assertFalse(sorted({ch for ch in out if ord(ch) > 127}))
                self.assertTrue(all(len(ln) <= w for ln in out.splitlines()))
        finally:
            T.set_ascii(False)
            T.set_mode("truecolor")

    def test_broken_panel_does_not_blank_dashboard(self):
        from porchlight.ui import BUILDERS
        saved = BUILDERS["system"]
        BUILDERS["system"] = lambda x: 1 / 0
        try:
            out = T.strip(render.render(self.h, self.cache, width=150))
            self.assertIn("panel error: ZeroDivisionError", out)
            self.assertIn("AGENTS", out)
        finally:
            BUILDERS["system"] = saved


class FleetBundle(unittest.TestCase):
    def setUp(self):
        support.clear_cache()

    def test_bundle_carries_only_global_sources(self):
        put("weather", {"temp": 1})
        put("procs", {"total": 1})
        b = fleet.build_bundle(host('[fleet]\npeers = ["a"]'), {})
        self.assertIn("weather", b["sources"])
        self.assertNotIn("procs", b["sources"])

    def test_remote_command_quoting(self):
        self.assertIn('"${XDG_CACHE_HOME:-$HOME/.cache}/porchlight"', fleet.remote_command())
        self.assertIn('"$HOME"/', fleet.remote_command("~/.cache/pl"))


if __name__ == "__main__":
    unittest.main()

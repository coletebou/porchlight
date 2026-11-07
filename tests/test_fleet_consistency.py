"""One fleet view (docs/fleet-consistency.md): bundle v2, the shared-panel merge in
render.panel_keys, staleness in the header fact / FLEET panel note / one warn alert,
and what `porchlight doctor` says about all of it. Offline: a scratch cache, fake
bundles written straight to core.BUNDLE_PATH, and a couple of throwaway test sources
cleaned up per test (never left registered for other modules' auto layout)."""
import contextlib
import io
import time
import unittest
from unittest import mock

import support  # noqa: F401 — must be first: isolates cache/config
from support import host, put

from porchlight import __version__ as PL_VERSION
from porchlight import alerts, core, doctor, hosts, registry, render, theme as T, view
from porchlight.sources import fleet


def put_bundle_v2(frm="atlas", age=30.0, interval=240, shared=None, porchlight_version=None, now=None):
    """Write a v2 bundle as the hub would, `age` seconds old."""
    now = now if now is not None else time.time()
    bundle = {"v": 2, "porchlight": porchlight_version or PL_VERSION, "from": frm, "ts": now - age,
              "interval": interval, "sources": {},
              "fleet": {"hosts": {frm: {"ok": True, "seen": now - age, "snap": {"label": frm, "ts": now - age}}}}}
    if shared is not None:
        bundle["layout"] = {"shared_panels": list(shared)}
    core.write_json_atomic(core.BUNDLE_PATH, bundle)
    return bundle


def _calm_lv():
    return dict(support.CALM)


class BundleV2(unittest.TestCase):
    """build_bundle: v2 shape, bundle_local records, scope and state filtering."""

    def setUp(self):
        support.clear_cache()
        registry.source("t_local_on", scope="local")(lambda ctx: {})
        registry.source("t_local_off", scope="local", hosts=("#never-matches-xyz",))(lambda ctx: {})

    def tearDown(self):
        registry.SOURCES.pop("t_local_on", None)
        registry.SOURCES.pop("t_local_off", None)

    def test_bundle_is_v2_with_porchlight_version_and_interval(self):
        b = fleet.build_bundle(host('[fleet]\npeers = ["a"]'), {})
        self.assertEqual(b["v"], 2)
        self.assertEqual(b["porchlight"], PL_VERSION)
        self.assertEqual(b["interval"], registry.SOURCES["fleet"].ttl)
        self.assertIn("from", b)
        self.assertIn("ts", b)

    def test_global_records_unchanged(self):
        put("weather", {"temp": 1})
        put("procs", {"total": 1})
        b = fleet.build_bundle(host('[fleet]\npeers = ["a"]'), {})
        self.assertIn("weather", b["sources"])
        self.assertNotIn("procs", b["sources"])

    def test_shared_panels_ship_in_layout_when_set(self):
        b = fleet.build_bundle(host('[fleet]\npeers = ["a"]\nshared_panels = ["drover", "todos"]'), {})
        self.assertEqual(b["layout"]["shared_panels"], ["drover", "todos"])

    def test_no_shared_panels_omits_layout(self):
        b = fleet.build_bundle(host('[fleet]\npeers = ["a"]'), {})
        self.assertNotIn("layout", b)

    def test_bundle_local_record_included_when_on(self):
        put("t_local_on", {"x": 1})
        b = fleet.build_bundle(host('[fleet]\npeers = ["a"]\nbundle_local = ["t_local_on"]'), {})
        self.assertIn("t_local_on", b["sources"])

    def test_bundle_local_excludes_a_disabled_local_source(self):
        put("t_local_off", {"x": 1})
        b = fleet.build_bundle(host('[fleet]\npeers = ["a"]\nbundle_local = ["t_local_off"]'), {})
        self.assertNotIn("t_local_off", b["sources"])

    def test_bundle_local_skips_a_source_with_no_record(self):
        b = fleet.build_bundle(host('[fleet]\npeers = ["a"]\nbundle_local = ["t_local_on"]'), {})
        self.assertNotIn("t_local_on", b["sources"])

    def test_default_bundle_local_is_drover(self):
        # The default (fleet.bundle_local = ["drover"]) is covered by test_core's
        # test_bundle_carries_only_global_sources; here we only confirm our own
        # explicit list is honored and does not also pull in the default.
        put("t_local_on", {"x": 1})
        b = fleet.build_bundle(host('[fleet]\npeers = ["a"]\nbundle_local = ["t_local_on"]'), {})
        self.assertNotIn("drover", b["sources"])


class BundleVersions(unittest.TestCase):
    """A peer accepts v1 and v2; any other value is ignored as if no bundle arrived."""

    def setUp(self):
        support.clear_cache()

    def test_v2_bundle_is_ok(self):
        put_bundle_v2()
        self.assertTrue(hosts.bundle_ok(registry.Cache().bundle))

    def test_v1_bundle_is_ok_but_has_no_layout(self):
        core.write_json_atomic(core.BUNDLE_PATH, {"v": 1, "from": "atlas", "ts": time.time(), "sources": {},
                                                    "fleet": {"hosts": {}}})
        b = registry.Cache().bundle
        self.assertTrue(hosts.bundle_ok(b))
        self.assertEqual(hosts.shared_panels(host(""), b), [])  # no layout key at all

    def test_unknown_version_is_ignored_like_no_bundle(self):
        core.write_json_atomic(core.BUNDLE_PATH, {"v": 99, "from": "atlas", "ts": time.time(),
                                                    "layout": {"shared_panels": ["todos"]}})
        b = registry.Cache().bundle
        self.assertFalse(hosts.bundle_ok(b))
        self.assertEqual(hosts.shared_panels(host(""), b), [])
        self.assertFalse(hosts.bundle_stale(host(""), b))  # not stale: there is effectively no bundle

    def test_an_unknown_version_feeds_no_records_to_the_peer(self):
        core.write_json_atomic(core.BUNDLE_PATH, {"v": 99, "from": "atlas", "ts": time.time(),
                                                    "sources": {"hub_only_probe": {"ts": time.time(), "ok": True,
                                                                            "data": {"temp": 2}}},
                                                    "fleet": {"hosts": {"atlas": {"snap": {}}}}})
        c = registry.Cache()
        self.assertEqual(c.bundle.get("v"), 99)       # still readable, so doctor can say "ignored"
        self.assertIsNone(c.rec("hub_only_probe"))    # but none of its records are used
        self.assertEqual(c.fleet_hosts(False), {})
        self.assertEqual(view.fleet_info(host(""), c)["role"], "solo")

    def test_a_bundle_without_a_version_reads_as_v1(self):
        core.write_json_atomic(core.BUNDLE_PATH, {"from": "atlas", "ts": time.time(), "sources": {}})
        self.assertEqual(hosts.bundle_version(registry.Cache().bundle), 1)

    def test_no_bundle_at_all_is_not_stale(self):
        self.assertFalse(hosts.bundle_ok(registry.Cache().bundle))
        self.assertFalse(hosts.bundle_stale(host(""), registry.Cache().bundle))


class StaleThreshold(unittest.TestCase):
    """fleet.stale_after, or 7 x the bundle's interval when that is 0."""

    def test_default_is_7x_bundle_interval(self):
        self.assertEqual(hosts.stale_after(host(""), {"v": 2, "interval": 100}), 700)

    def test_default_interval_is_240_when_the_bundle_omits_it(self):
        self.assertEqual(hosts.stale_after(host(""), {"v": 1}), 7 * 240)

    def test_configured_stale_after_overrides_the_interval_math(self):
        h = host("[fleet]\nstale_after = 60")
        self.assertEqual(hosts.stale_after(h, {"v": 2, "interval": 100}), 60)

    def test_zero_stale_after_falls_back_to_7x_interval(self):
        h = host("[fleet]\nstale_after = 0")
        self.assertEqual(hosts.stale_after(h, {"v": 2, "interval": 50}), 350)

    def test_age_under_and_over_threshold(self):
        h = host("")
        fresh = {"v": 2, "interval": 100, "ts": time.time() - 100}
        stale = {"v": 2, "interval": 100, "ts": time.time() - 800}
        self.assertFalse(hosts.bundle_stale(h, fresh))
        self.assertTrue(hosts.bundle_stale(h, stale))


class PanelMerge(unittest.TestCase):
    """render.panel_keys: own panels first, then every shared panel not already
    listed, in the shared order; `only` and compact are untouched."""

    def setUp(self):
        support.clear_cache()

    def test_shared_panels_appended_after_own_in_order(self):
        h = host('[layout]\npanels = ["system", "services"]\n[fleet]\nshared_panels = ["todos", "drover", "fleet"]')
        keys = render.panel_keys(h, cache=registry.Cache())
        self.assertEqual(keys[:2], ["system", "services"])
        self.assertEqual(keys[2:5], ["todos", "drover", "fleet"])

    def test_own_listed_panel_keeps_its_own_position_no_duplicate(self):
        h = host('[layout]\npanels = ["system", "todos", "services"]\n[fleet]\nshared_panels = ["todos", "drover"]')
        keys = render.panel_keys(h, cache=registry.Cache())
        self.assertEqual(keys[:3], ["system", "todos", "services"])
        self.assertIn("drover", keys[3:])
        self.assertEqual(keys.count("todos"), 1)

    def test_bundle_layout_used_when_host_sets_no_override(self):
        put_bundle_v2(shared=["todos", "drover"])
        h = host('[layout]\npanels = ["system"]')
        keys = render.panel_keys(h, cache=registry.Cache())
        self.assertEqual(keys[1:3], ["todos", "drover"])

    def test_host_override_wins_over_bundle_layout(self):
        put_bundle_v2(shared=["drover"])
        h = host('[layout]\npanels = ["system"]\n[fleet]\nshared_panels = ["todos"]')
        keys = render.panel_keys(h, cache=registry.Cache())
        self.assertIn("todos", keys)
        self.assertNotIn("drover", keys)

    def test_unknown_shared_panel_key_is_kept_in_the_list_but_builds_nothing(self):
        h = host('[layout]\npanels = ["system"]\n[fleet]\nshared_panels = ["not_a_real_panel"]')
        keys = render.panel_keys(h, cache=registry.Cache())
        self.assertIn("not_a_real_panel", keys)
        with support.calm_probes():
            out = T.strip(render.render(h, registry.Cache(), width=150))
        self.assertNotIn("NOT_A_REAL_PANEL", out)

    def test_only_is_unaffected_by_shared_panels(self):
        h = host('[fleet]\nshared_panels = ["todos"]')
        self.assertEqual(render.panel_keys(h, only=["system"], cache=registry.Cache()), ["system"])

    def test_compact_is_unaffected_by_shared_panels(self):
        h = host('[layout]\ncompact = ["system"]\n[fleet]\nshared_panels = ["todos"]')
        self.assertEqual(render.panel_keys(h, compact=True, cache=registry.Cache()), ["system"])

    def test_no_cache_argument_is_safe(self):
        h = host('[layout]\npanels = ["system"]')
        self.assertEqual(render.panel_keys(h), ["system"])  # cache=None: no shared merge, no crash


class HeaderFact(unittest.TestCase):
    """The peer-only header fact: dim when fresh, amber when stale, absent on the
    hub and on a solo machine with no bundle at all."""

    NOW = 1_800_000_000.0

    def setUp(self):
        support.clear_cache()

    def _facts(self, h):
        return render.side_facts(h, registry.Cache(), {"vitals": support.CALM["vitals"]}, [], None)

    def test_fresh_fact_is_dim(self):
        with mock.patch("porchlight.core.now", return_value=self.NOW):
            put_bundle_v2(frm="atlas", age=30, interval=240, now=self.NOW)
            facts = self._facts(host(""))
            expected = T.dim("fleet via atlas · %s" % core.human_age(30))
        self.assertIn(expected, facts)

    def test_stale_fact_is_amber(self):
        with mock.patch("porchlight.core.now", return_value=self.NOW):
            put_bundle_v2(frm="atlas", age=10_000, interval=240, now=self.NOW)
            facts = self._facts(host(""))
            expected = T.c("fleet via atlas · %s" % core.human_age(10_000), "amber")
        self.assertIn(expected, facts)

    def test_no_fact_on_the_hub(self):
        with mock.patch("porchlight.core.now", return_value=self.NOW):
            put_bundle_v2(frm="atlas", age=30, interval=240, now=self.NOW)
            facts = self._facts(host('[fleet]\npeers = ["atlas"]'))
        self.assertFalse(any("fleet via" in T.strip(f) for f in facts))

    def test_no_fact_when_solo(self):
        facts = self._facts(host(""))  # no bundle written at all
        self.assertFalse(any("fleet via" in T.strip(f) for f in facts))

    def test_no_fact_when_bundle_version_unknown(self):
        core.write_json_atomic(core.BUNDLE_PATH, {"v": 77, "from": "atlas", "ts": time.time()})
        facts = self._facts(host(""))
        self.assertFalse(any("fleet via" in T.strip(f) for f in facts))


class FleetStaleAlert(unittest.TestCase):
    """alerts.gather: one warn alert, src "fleet", only for a stale peer bundle."""

    NOW = 1_800_000_000.0

    def setUp(self):
        support.clear_cache()

    def test_warns_on_a_stale_peer_bundle(self):
        with mock.patch("porchlight.core.now", return_value=self.NOW):
            put_bundle_v2(frm="atlas", age=10_000, interval=240, now=self.NOW)
            found = alerts.gather(host(""), registry.Cache(), _calm_lv())
        hits = [a for a in found if a.get("src") == "fleet"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["sev"], "warn")
        self.assertEqual(hits[0]["msg"], "fleet data from atlas is %s old" % core.human_age(10_000))
        self.assertEqual(hits[0]["fix"], "ssh atlas porchlight collect --only fleet")

    def test_no_alert_when_fresh(self):
        with mock.patch("porchlight.core.now", return_value=self.NOW):
            put_bundle_v2(frm="atlas", age=30, interval=240, now=self.NOW)
            found = alerts.gather(host(""), registry.Cache(), _calm_lv())
        self.assertFalse([a for a in found if a.get("src") == "fleet"])

    def test_no_alert_on_the_hub_even_if_an_old_bundle_file_lingers(self):
        with mock.patch("porchlight.core.now", return_value=self.NOW):
            put_bundle_v2(frm="atlas", age=10_000, interval=240, now=self.NOW)
            found = alerts.gather(host('[fleet]\npeers = ["atlas"]'), registry.Cache(), _calm_lv())
        self.assertFalse([a for a in found if a.get("src") == "fleet"])

    def test_no_alert_when_solo(self):
        found = alerts.gather(host(""), registry.Cache(), _calm_lv())
        self.assertFalse([a for a in found if a.get("src") == "fleet"])

    def test_no_alert_for_an_unknown_bundle_version(self):
        core.write_json_atomic(core.BUNDLE_PATH, {"v": 5, "from": "atlas", "ts": time.time() - 100_000})
        found = alerts.gather(host(""), registry.Cache(), _calm_lv())
        self.assertFalse([a for a in found if a.get("src") == "fleet"])


class FleetPanelNote(unittest.TestCase):
    """panels/core.p_fleet: the "via <from> · <age>" note, amber when stale."""

    NOW = 1_800_000_000.0

    def setUp(self):
        support.clear_cache()

    def _note(self, h):
        from porchlight import panels
        from porchlight.ui import RCtx
        lv = _calm_lv()
        ctx = RCtx(h, registry.Cache(), lv, [], 90)
        p = panels.BUILDERS["fleet"](ctx)
        return p.note if p else ""

    def test_fresh_note_has_no_amber_escape(self):
        with mock.patch("porchlight.core.now", return_value=self.NOW):
            put_bundle_v2(frm="atlas", age=30, interval=240, now=self.NOW)
            note = self._note(host(""))
        self.assertIn("via atlas", note)
        self.assertEqual(T.strip(note), note)  # plain: p_fleet leaves it to Panel.render's dim

    def test_stale_note_carries_amber(self):
        with mock.patch("porchlight.core.now", return_value=self.NOW):
            put_bundle_v2(frm="atlas", age=10_000, interval=240, now=self.NOW)
            note = self._note(host(""))
        self.assertIn("via atlas", T.strip(note))
        self.assertNotEqual(T.strip(note), note)  # colored

    def test_unknown_version_note_says_ignored(self):
        core.write_json_atomic(core.BUNDLE_PATH, {"v": 42, "from": "atlas", "ts": time.time(),
                                                    "fleet": {"hosts": {"atlas": {"ok": True, "seen": time.time(),
                                                                                   "snap": {"label": "atlas"}}}}})
        note = self._note(host(""))
        self.assertIn("ignored", note)


class DoctorOutput(unittest.TestCase):
    """porchlight doctor: bundle version, porchlight skew, shared-list source, staleness."""

    def setUp(self):
        support.clear_cache()

    def _run(self, h):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.run(h, registry.Cache())
        return T.strip(out.getvalue())

    def test_shared_panels_from_own_override(self):
        out = self._run(host('[fleet]\nshared_panels = ["todos"]'))
        self.assertIn("fleet shared panels from this host's fleet.shared_panels", out)

    def test_shared_panels_from_hub_bundle(self):
        put_bundle_v2(shared=["todos", "drover"])
        out = self._run(host(""))
        self.assertIn("fleet shared panels from the hub's layout.shared_panels", out)

    def test_shared_panels_none(self):
        out = self._run(host(""))
        self.assertIn("fleet shared panels from none", out)

    def test_bundle_version_and_stale_state_shown(self):
        with mock.patch("porchlight.core.now", return_value=1_800_000_000.0):
            put_bundle_v2(frm="atlas", age=10_000, interval=240, now=1_800_000_000.0)
            out = self._run(host(""))
        self.assertIn("fleet bundle: v2 from atlas", out)
        self.assertIn("STALE", out)

    def test_fresh_bundle_not_marked_stale(self):
        with mock.patch("porchlight.core.now", return_value=1_800_000_000.0):
            put_bundle_v2(frm="atlas", age=30, interval=240, now=1_800_000_000.0)
            out = self._run(host(""))
        self.assertIn("fleet bundle: v2 from atlas", out)
        self.assertNotIn("STALE", out)

    def test_unknown_version_reported_as_ignored(self):
        core.write_json_atomic(core.BUNDLE_PATH, {"v": 7, "from": "atlas", "ts": time.time()})
        out = self._run(host(""))
        self.assertIn("ignored (unsupported version", out)

    def test_no_bundle_yet(self):
        out = self._run(host(""))
        self.assertIn("fleet bundle: none yet", out)

    def test_version_skew_noted(self):
        put_bundle_v2(frm="atlas", age=5, interval=240, porchlight_version="9.9.9")
        out = self._run(host(""))
        self.assertIn("hub porchlight 9.9.9 vs ours %s" % PL_VERSION, out)

    def test_no_skew_line_when_versions_match(self):
        put_bundle_v2(frm="atlas", age=5, interval=240, porchlight_version=PL_VERSION)
        out = self._run(host(""))
        self.assertNotIn("vs ours", out)

    def test_nothing_fleet_specific_on_the_hub(self):
        out = self._run(host('[fleet]\npeers = ["atlas"]'))
        self.assertNotIn("fleet bundle:", out)


if __name__ == "__main__":
    unittest.main()

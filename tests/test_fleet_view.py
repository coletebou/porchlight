"""One fleet view (docs/fleet-consistency.md): the hub's shared_panels ride in its bundle, a peer of
the same identity shows them after its own local panels, and a stale bundle is said out loud."""
import io
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import support  # noqa: F401 — must be first: isolates cache/config
from support import calm_probes, host, put

from porchlight import alerts, core, doctor, registry, render, theme as T
from porchlight.sources import fleet

HUB = '[fleet]\npeers = ["web-1", "db-1"]\nshared_panels = ["fleet", "todos", "ai"]\n'
PEER = '[layout]\npanels = ["system", "services", "todos", "repos"]\n'


def bundle(age=60.0, shared=("fleet", "todos", "ai"), identity=None, **extra):
    identity = identity or host(PEER).identity
    b = {"v": 1, "from": "hub-box", "label": "hub", "identity": identity, "ts": time.time() - age, "sources": {},
         "fleet": {"hosts": {}}}
    if shared is not None:
        b["layout"] = {"shared": list(shared)}
    b.update(extra)
    core.write_json_atomic(core.BUNDLE_PATH, b)
    return b


class Base(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.addCleanup(support.clear_cache)


class Bundle(Base):
    def test_the_hub_ships_its_layout_label_and_identity(self):
        b = fleet.build_bundle(host(HUB), {})
        self.assertEqual(b["layout"], {"shared": ["fleet", "todos", "ai"]})
        self.assertEqual(b["identity"], host(HUB).identity)
        self.assertTrue(b["label"])

    def test_no_shared_list_no_layout(self):
        self.assertNotIn("layout", fleet.build_bundle(host('[fleet]\npeers = ["a"]\n'), {}))


class Adoption(Base):
    def test_a_peer_adopts_the_hubs_list_after_its_own_local_panels(self):
        bundle()
        h = host(PEER)
        self.assertFalse(h.hub)
        self.assertEqual(render.shared_panels(h, registry.Cache()), ["fleet", "todos", "ai"])
        # local first (todos moves to the shared part, in the fleet's order), then the fleet part
        self.assertEqual(render.panel_keys(h, registry.Cache()), ["system", "services", "repos", "fleet", "todos", "ai"])

    def test_the_hub_uses_its_own_setting(self):
        bundle(shared=("weather",))  # a stray bundle on the hub is ignored
        self.assertEqual(render.shared_panels(host(HUB), registry.Cache()), ["fleet", "todos", "ai"])

    def test_a_peer_may_opt_out(self):
        bundle()
        h = host(PEER + '[fleet]\nadopt_layout = false\nshared_panels = ["todos"]\n')
        self.assertEqual(render.shared_panels(h, registry.Cache()), ["todos"])

    def test_another_identitys_layout_is_never_adopted(self):
        bundle(identity=host(PEER).identity + "-other")
        h = host(PEER)
        self.assertEqual(render.shared_panels(h, registry.Cache()), [])
        self.assertEqual(render.panel_keys(h, registry.Cache()), ["system", "services", "todos", "repos"])

    def test_no_bundle_falls_back_to_the_local_setting(self):
        h = host(PEER + '[fleet]\nshared_panels = ["todos"]\n')
        self.assertEqual(render.panel_keys(h, registry.Cache()), ["system", "services", "repos", "todos"])

    def test_a_malformed_layout_is_ignored(self):
        for lay in ({"shared": "fleet"}, "x", {"shared": [1, None, "todos"]}):
            support.clear_cache()
            bundle(shared=None, layout=lay)
            got = render.shared_panels(host(PEER), registry.Cache())
            self.assertEqual(got, ["todos"] if isinstance(lay, dict) and isinstance(lay.get("shared"), list) else [], lay)


class Staleness(Base):
    def fleet_alerts(self, h):
        return [a for a in alerts.gather(h, registry.Cache(), dict(support.CALM)) if a.get("src") == "fleet"]

    def test_fresh_bundle_no_alert(self):
        bundle(age=120)
        self.assertEqual(self.fleet_alerts(host(PEER)), [])

    def test_a_stale_bundle_warns_with_the_fix(self):
        bundle(age=3600)
        got = self.fleet_alerts(host(PEER))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["sev"], "warn")
        self.assertIn("fleet view from hub is 1h old", got[0]["msg"])
        self.assertIn("porchlight doctor", got[0]["fix"])

    def test_stale_after_is_configurable_and_zero_turns_it_off(self):
        bundle(age=600)
        self.assertEqual(len(self.fleet_alerts(host(PEER + '[fleet]\nstale_after = 300\n'))), 1)
        bundle(age=99999)
        self.assertEqual(self.fleet_alerts(host(PEER + '[fleet]\nstale_after = 0\n')), [])

    def test_a_named_hub_without_a_bundle_is_an_info(self):
        got = self.fleet_alerts(host(PEER + '[fleet]\nhub = "hz-prod"\n'))
        self.assertEqual([(a["sev"], a["msg"]) for a in got], [("info", "no fleet view from hz-prod yet")])

    def test_the_hub_never_warns_about_itself(self):
        bundle(age=99999)
        self.assertEqual(self.fleet_alerts(host(HUB)), [])


class Footer(Base):
    def test_the_footer_names_the_view_and_its_age(self):
        bundle(age=180)
        self.assertEqual(T.strip(render.fleet_view(host(PEER), registry.Cache())), "fleet view hub 3m")

    def test_amber_once_stale(self):
        T.set_mode("truecolor")
        bundle(age=7200)
        self.addCleanup(T.set_mode, "truecolor")
        self.assertEqual(render.fleet_view(host(PEER), registry.Cache()), T.c("fleet view hub 2h", "amber"))
        bundle(age=60)
        self.assertEqual(render.fleet_view(host(PEER), registry.Cache()), T.dim("fleet view hub 1m"))

    def test_none_on_the_hub_or_without_a_bundle(self):
        self.assertEqual(render.fleet_view(host(PEER), registry.Cache()), "")
        bundle()
        self.assertEqual(render.fleet_view(host(HUB), registry.Cache()), "")

    def test_a_full_render_on_a_peer_shows_it(self):
        bundle(age=180)
        put("todos", {"items": [], "providers": {}, "total": 0, "overdue": 0})
        with calm_probes():
            out = T.strip(render.render(host(PEER), registry.Cache(), width=140))
        self.assertIn("fleet view hub 3m", out)

    def test_it_can_be_turned_off(self):
        bundle(age=180)
        with calm_probes():
            out = T.strip(render.render(host(PEER + '[footer]\nfleet_view = false\n'), registry.Cache(), width=140))
        self.assertNotIn("fleet view", out)


class Doctor(Base):
    def run_doctor(self, h):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(doctor, "timer_state", lambda: ""):
            doctor.run(h, registry.Cache())
        return T.strip(buf.getvalue())

    def test_doctor_says_where_the_fleet_view_comes_from(self):
        bundle(age=60)
        out = self.run_doctor(host(PEER))
        self.assertIn("hub bundle from hub-box, 1m old", out)
        self.assertIn("fleet view: fleet todos ai  (from the hub's bundle)", out)

    def test_doctor_marks_a_stale_bundle(self):
        bundle(age=7200)
        self.assertIn("2h old (stale)", self.run_doctor(host(PEER)))


if __name__ == "__main__":
    unittest.main()

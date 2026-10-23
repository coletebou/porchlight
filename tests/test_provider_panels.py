"""New provider states survive the cache-to-dashboard path at terminal widths."""
import unittest

import support  # noqa: F401 — scratch cache must be set before imports

from porchlight import registry, theme as T
from porchlight import sources  # noqa: F401 — register sources before testing identity filters
from porchlight.panels import dev as panels
from porchlight.ui import RCtx


class ProviderPanels(unittest.TestCase):
    def setUp(self):
        support.clear_cache()

    def tearDown(self):
        support.clear_cache()

    def build(self, name="access", width=90, toml="[sources.gcp_auth]\n[sources.azure_auth]\n"):
        return getattr(panels, "p_" + name)(RCtx(support.host(toml), registry.Cache(), {"vitals": {}}, [], width))

    def test_cloud_states_fit_and_only_rejected_warns(self):
        for name in ("gcp_auth", "azure_auth"):
            for state in ("ok", "none", "configured", "rejected", "unreachable", "error"):
                for width in (60, 90, 130):
                    with self.subTest(name=name, state=state, width=width):
                        support.clear_cache()
                        support.put(name, {"cli": True, "state": state, "account": "developer@example.com",
                                           "project": "example-development-project", "subscription": "Example development",
                                           "error": "a long explanation " * 12, "login": "login --profile example"})
                        p = self.build(width=width)
                        self.assertIsNotNone(p)
                        self.assertEqual(p.level, "warn" if state == "rejected" else "ok")
                        for line in p.lines:
                            self.assertLessEqual(T.width(line), width - 1)
                        text = T.strip("\n".join(p.lines))
                        if state == "ok":
                            self.assertNotIn("not verified", text)
                        if state == "configured" and width >= 90:
                            self.assertIn("not verified", text)

    def test_cloud_cache_does_not_enable_a_source_or_cross_identities(self):
        support.put("gcp_auth", {"cli": True, "state": "ok", "account": "private@example.com", "project": "private"})
        for toml in ("", "[sources.gcp_auth]\nenabled = false\n",
                     '[general]\nidentity = "work"\n[sources.gcp_auth]\nidentities = ["personal"]\n',
                     '[general]\nidentity = "work"\n[identities.work]\nallow = ["procs"]\n[sources.gcp_auth]\n'):
            with self.subTest(toml=toml):
                self.assertIsNone(self.build(toml=toml))

    def test_missing_cli_and_stale_record_are_visible(self):
        support.put("azure_auth", {"cli": False, "state": "none"}, ok=False, age=7200)
        p = self.build()
        self.assertIn("az CLI not installed", T.strip("\n".join(p.lines)))
        self.assertIn("azure", p.note)
        self.assertIn("2h", p.note)

    def test_new_forge_errors_and_partial_lists_are_named(self):
        for forge, label in (("gt", "gitea"), ("fj", "forgejo"), ("bb", "bitbucket")):
            support.put("prs", {"mine": [], "review": [], "forges": [forge], "open": 0,
                                "errors": {forge: "unreachable"}, "truncated": [forge]})
            for width in (60, 90, 130):
                p = self.build("prs", width)
                text = T.strip("\n".join(p.lines))
                self.assertIn(label + ": unreachable", text)
                self.assertIn("showing a limited list", text)
                for line in p.lines:
                    self.assertLessEqual(T.width(line), width - 1)

    def test_authentication_hint_belongs_to_the_rejected_forge(self):
        for forge in ("gh", "gl", "gt", "fj", "bb"):
            with self.subTest(forge=forge):
                support.put("prs", {"error": forge + ": unauthorized", "status": "unknown"})
                text = T.strip("\n".join(self.build("prs", 130).lines))
                self.assertIn(forge + ": unauthorized", text)
                self.assertEqual("gh auth login" in text, forge == "gh")
        support.put("prs", {"error": "bb: unauthorized; gh: unauthorized", "status": "unknown"})
        self.assertIn("gh auth login", T.strip("\n".join(self.build("prs", 130).lines)))


if __name__ == "__main__":
    unittest.main()

"""GitHub Actions billing (sources/gh_cost.py) and its MONEY panel row.

Offline: every GitHub API is a fake routed by (method, URL), reusing test_saas's
Router and Base.run_src so this source is exercised through its real entry point,
exactly like the other saas-style sources.
"""
import json
import os
import time
import unittest
from unittest import mock

import support  # noqa: F401 — must be first: isolates cache/config
from support import host, put
from test_saas import Base, CANARY, Router

from porchlight import registry, theme as T
from porchlight.panels import cloud  # noqa: F401 — registers the money panel
from porchlight.sources import gh_cost as G
from porchlight.sources import saas as saas_core
from porchlight.ui import BUILDERS, RCtx

GH = "https://api.github.com"
NOW = float(time.mktime(time.strptime("2026-11-10", "%Y-%m-%d")))


def item(product="Actions", sku="Actions Linux", qty=100, unit="minutes", price=0.008, gross=None, discount=0.0,
         repo="acme/web", date="2026-11-05"):
    g = gross if gross is not None else round(qty * price, 2)
    return {"date": date, "product": product, "sku": sku, "quantity": qty, "unitType": unit, "pricePerUnit": price,
            "grossAmount": g, "discountAmount": discount, "netAmount": round(g - discount, 2), "repositoryName": repo}


class Classify(unittest.TestCase):
    def test_is_actions(self):
        self.assertTrue(G.is_actions(item(product="Actions")))
        self.assertTrue(G.is_actions({"product": "Shared Storage", "sku": "Actions Storage"}))
        self.assertFalse(G.is_actions({"product": "Shared Storage", "sku": "Packages Storage"}))
        self.assertFalse(G.is_actions({"product": "Codespaces", "sku": "Codespaces Linux"}))


class Aggregate(unittest.TestCase):
    def test_totals_minutes_storage_and_top_repos(self):
        items = [item(qty=100, price=0.008, repo="acme/web"),               # gross 0.80, net 0.80
                 item(sku="Actions Windows", qty=50, price=0.016, discount=0.4, repo="acme/web"),  # gross 0.80 net 0.40
                 item(product="Shared Storage", sku="Actions Storage", qty=10, unit="GB", price=0.25, repo="acme/web"),
                 item(repo="acme/api", qty=200, price=0.008),               # gross 1.60
                 {"product": "Codespaces", "sku": "Codespaces Linux", "quantity": 10, "unitType": "hours",
                  "pricePerUnit": 0.18, "grossAmount": 1.8, "discountAmount": 0, "netAmount": 1.8, "repositoryName": "x"}]
        agg = G.aggregate(items)
        self.assertAlmostEqual(agg["gross_usd"], 0.8 + 0.8 + 2.5 + 1.6, places=2)
        self.assertAlmostEqual(agg["net_usd"], 0.8 + 0.4 + 2.5 + 1.6, places=2)
        self.assertEqual(agg["discount_usd"], 0.4)
        self.assertAlmostEqual(agg["discount_pct"], round(100 * 0.4 / agg["gross_usd"], 1))
        skus = {r["sku"]: r for r in agg["by_sku"]}
        self.assertEqual(skus["Actions Linux"]["quantity"], 300)   # the two acme/web + acme/api Linux rows combine
        self.assertEqual(skus["Actions Windows"]["net"], 0.4)
        self.assertEqual([r["sku"] for r in agg["storage"]], ["Actions Storage"])
        self.assertEqual(agg["storage"][0]["gross"], 2.5)
        repos = {r["repo"]: r for r in agg["top_repos"]}
        self.assertEqual(round(repos["acme/web"]["gross"], 2), 0.8 + 0.8 + 2.5)
        self.assertEqual(round(repos["acme/api"]["gross"], 2), 1.6)
        self.assertNotIn("x", repos)  # Codespaces usage never joins an Actions repo total

    def test_top_repos_is_capped_and_sorted_by_net(self):
        items = [item(repo="r%d" % i, qty=(10 - i) * 10, price=0.008) for i in range(10)]
        agg = G.aggregate(items, top_repos=3)
        self.assertEqual(len(agg["top_repos"]), 3)
        self.assertEqual([r["repo"] for r in agg["top_repos"]], ["r0", "r1", "r2"])

    def test_no_actions_items_is_all_zero(self):
        agg = G.aggregate([{"product": "Packages", "sku": "Packages Storage", "quantity": 1, "unitType": "GB",
                            "pricePerUnit": 1, "grossAmount": 1, "discountAmount": 0, "netAmount": 1, "repositoryName": "x"}])
        self.assertEqual((agg["gross_usd"], agg["net_usd"], agg["by_sku"], agg["storage"], agg["top_repos"]),
                         (0.0, 0.0, [], [], []))
        self.assertIsNone(agg["discount_pct"])


class UsageFetch(unittest.TestCase):
    """gh_cost.usage() against a fake ctx.http, independent of the full source."""
    def ctx(self, routes):
        c = support.ctx(host())
        c.http = Router(routes)
        return c, c.http

    def test_enhanced_success(self):
        rows = [item()]
        ctx, rt = self.ctx({("GET", GH + "/users/octocat/settings/billing/usage"): (200, {"usageItems": rows})})
        out = G.usage(ctx, {"Authorization": "Bearer x"}, "user", "octocat", NOW)
        self.assertEqual(out, {"mode": "enhanced", "items": rows})
        self.assertEqual(rt.calls[0]["params"]["year"], "2026")
        self.assertEqual(rt.calls[0]["params"]["month"], "11")

    def test_404_falls_back_to_legacy(self):
        ctx, rt = self.ctx({
            ("GET", GH + "/organizations/acme/settings/billing/usage"): (404, {"message": "not found"}),
            ("GET", GH + "/orgs/acme/settings/billing/actions"): (200, {"total_minutes_used": 305, "included_minutes": 3000,
                                                                         "minutes_used_breakdown": {"UBUNTU": 305, "MACOS": 0}})})
        out = G.usage(ctx, {}, "org", "acme", NOW)
        self.assertEqual(out, {"mode": "legacy", "total_minutes_used": 305, "included_minutes": 3000, "breakdown": {"UBUNTU": 305}})

    def test_403_on_enhanced_is_needs_scope_without_trying_legacy(self):
        ctx, rt = self.ctx({("GET", GH + "/users/octocat/settings/billing/usage"): (403, {"message": "nope"})})
        with self.assertRaises(G.ProbeError) as e:
            G.usage(ctx, {}, "user", "octocat", NOW)
        self.assertEqual(str(e.exception), "needs-scope")
        self.assertEqual(len(rt.calls), 1)  # the legacy endpoint was never asked

    def test_403_on_legacy_after_a_404_is_also_needs_scope(self):
        ctx, _ = self.ctx({("GET", GH + "/users/octocat/settings/billing/usage"): (404, {}),
                           ("GET", GH + "/users/octocat/settings/billing/actions"): (403, {"message": "nope"})})
        with self.assertRaises(G.ProbeError) as e:
            G.usage(ctx, {}, "user", "octocat", NOW)
        self.assertEqual(str(e.exception), "needs-scope")

    def test_404_on_both_for_a_classic_token_without_the_scope_is_needs_scope(self):
        # GitHub hides the billing endpoints from a classic token that lacks the scope with a 404, not a 403;
        # the token's granted scopes come back in X-OAuth-Scopes.
        scopes = {"X-OAuth-Scopes": "gist, read:org, repo, workflow"}
        ctx, _ = self.ctx({("GET", GH + "/users/octocat/settings/billing/usage"): (404, {}, scopes),
                           ("GET", GH + "/users/octocat/settings/billing/actions"): (404, {"message": "Not Found"}, scopes)})
        with self.assertRaises(G.ProbeError) as e:
            G.usage(ctx, {}, "user", "octocat", NOW)
        self.assertEqual(str(e.exception), "needs-scope")
        # an org target is satisfied by repo (the legacy endpoint's documented scope), so its 404 is a real failure
        ctx, _ = self.ctx({("GET", GH + "/organizations/acme/settings/billing/usage"): (404, {}, scopes),
                           ("GET", GH + "/orgs/acme/settings/billing/actions"): (404, {"message": "Not Found"}, scopes)})
        with self.assertRaises(G.ProbeError) as e:
            G.usage(ctx, {}, "org", "acme", NOW)
        self.assertNotEqual(str(e.exception), "needs-scope")

    def test_404_on_both_with_the_scope_or_no_scope_header_stays_a_plain_failure(self):
        for headers in ({"x-oauth-scopes": "repo, user"}, {}):  # the scope granted; a fine-grained token (no header)
            ctx, _ = self.ctx({("GET", GH + "/users/octocat/settings/billing/usage"): (404, {}, headers),
                               ("GET", GH + "/users/octocat/settings/billing/actions"): (404, {"message": "Not Found"}, headers)})
            with self.assertRaises(G.ProbeError) as e:
                G.usage(ctx, {}, "user", "octocat", NOW)
            self.assertNotEqual(str(e.exception), "needs-scope", headers)

    def test_garbage_shapes_raise_probeerror_not_a_crash(self):
        ctx, _ = self.ctx({("GET", GH + "/users/octocat/settings/billing/usage"): (200, {"usageItems": "not-a-list"})})
        with self.assertRaises(G.ProbeError):
            G.usage(ctx, {}, "user", "octocat", NOW)
        ctx2, _ = self.ctx({("GET", GH + "/users/octocat/settings/billing/usage"): (404, {}),
                            ("GET", GH + "/users/octocat/settings/billing/actions"): (200, {"total_minutes_used": "x"})})
        with self.assertRaises(G.ProbeError):
            G.usage(ctx2, {}, "user", "octocat", NOW)


GH_TOML = '[sources.gh_actions_cost]\nuser = "octocat"\n'


class Source(Base):
    def go(self, routes, toml=GH_TOML, env=None, cli="gho_good"):
        full = {("GET", GH + "/rate_limit"): (200, {}), ("GET", GH + "/user"): (200, {"login": "octocat"})}
        full.update(routes)
        with mock.patch.object(saas_core, "_gh_cli_token", return_value=cli):
            return self.run_src("gh_actions_cost", env if env is not None else {"GITHUB_TOKEN": "gho_good"}, full, toml, now=NOW)

    def usage_route(self, who="octocat", items=None, status=200, kind="users"):
        return {("GET", "%s/%s/%s/settings/billing/usage" % (GH, kind, who)): (status, {"usageItems": items or [item()]})}

    def test_one_user_target_ok_no_net_spend(self):
        res, rt = self.go(self.usage_route(items=[item(qty=100, price=0.008, discount=0.8)]))  # fully covered: net 0
        self.assertEqual((res["status"], res["details"]["net_usd"]), ("ok", 0.0))
        self.assertNotIn("alerts", res)
        self.assertEqual(res["details"]["targets"], ["octocat"])

    def test_net_spend_warns_and_budget_exceeded_crits(self):
        routes = self.usage_route(items=[item(qty=1000, price=0.008)])  # gross == net == 8.00
        res, _ = self.go(routes)
        self.assertEqual((res["status"], res["alerts"][0]["sev"]), ("warn", "warn"))
        self.assertIn("$8.00", res["summary"])
        res2, _ = self.go(routes, toml=GH_TOML + "budget_usd = 5\n")
        self.assertEqual((res2["status"], res2["alerts"][0]["sev"]), ("crit", "crit"))
        self.assertIn("exceeds the $5.00 budget", res2["alerts"][0]["msg"])

    def test_org_and_user_targets_mixed_enhanced_and_legacy(self):
        routes = dict(self.usage_route(items=[item(qty=100, price=0.008)]))
        routes[("GET", GH + "/organizations/acme/settings/billing/usage")] = (404, {})
        routes[("GET", GH + "/orgs/acme/settings/billing/actions")] = (200, {"total_minutes_used": 500, "included_minutes": 2000,
                                                                              "minutes_used_breakdown": {"UBUNTU": 500}})
        res, _ = self.go(routes, toml=GH_TOML + 'orgs = ["acme"]\n')
        self.assertEqual(sorted(res["details"]["targets"]), ["acme", "octocat"])
        self.assertEqual(res["details"]["modes"], {"acme": "legacy", "octocat": "enhanced"})
        self.assertEqual(res["details"]["legacy_minutes_used"], 500)
        self.assertEqual(res["details"]["legacy_included_minutes"], 2000)
        self.assertEqual(res["details"]["legacy_included_pct"], 25.0)

    def test_needs_scope_on_every_target_is_unknown_never_an_alert(self):
        res, _ = self.go({("GET", GH + "/users/octocat/settings/billing/usage"): (403, {"message": "nope"})})
        self.assertEqual(res["status"], "unknown")
        self.assertNotIn("alerts", res)
        self.assertIn("needs billing scope", res["details"]["errors"]["octocat"])

    def test_one_target_needing_scope_is_partial_not_fatal(self):
        routes = dict(self.usage_route(items=[item(qty=100, price=0.008, discount=0.8)]))
        routes[("GET", GH + "/organizations/acme/settings/billing/usage")] = (403, {"message": "nope"})
        res, _ = self.go(routes, toml=GH_TOML + 'orgs = ["acme"]\n')
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["details"]["targets"], ["octocat"])
        self.assertIn("acme", res["details"]["errors"])

    def test_no_token_is_a_soft_failure(self):
        res, rt = self.go({}, env={}, cli=None)
        self.assertEqual((res["status"], res["summary"]), ("unknown", "no valid GitHub token"))
        self.assertEqual(rt.calls, [])

    def test_no_user_or_orgs_configured_is_a_soft_failure(self):
        # An app token: no /user identity, and no [sources.gh_actions_cost] user or orgs
        # configured either, so there is nothing to bill and the billing endpoint is never asked.
        routes = {("GET", GH + "/rate_limit"): (200, {}), ("GET", GH + "/user"): (403, {"message": "no user identity"})}
        with mock.patch.object(saas_core, "_gh_cli_token", return_value="gho_good"):
            res, rt = self.run_src("gh_actions_cost", {"GITHUB_TOKEN": "gho_good"}, routes,
                                   '[sources.gh_actions_cost]\nuser = ""\n', now=NOW)
        self.assertEqual(res["status"], "unknown")
        self.assertFalse(any("billing" in c["url"] for c in rt.calls))

    def test_token_never_leaks(self):
        res, rt = self.go(self.usage_route(items=[item(qty=100, price=0.008)]), env={"GITHUB_TOKEN": CANARY}, cli=None)
        for c in rt.calls:
            self.assertEqual(c["headers"].get("Authorization"), "Bearer " + CANARY)
        self.assertNotIn(CANARY, json.dumps(res))

    def test_the_reference_page_documents_every_setting(self):
        with open(os.path.join(support.ROOT, "docs", "reference", "saas.md"), encoding="utf-8") as fh:
            doc = fh.read()
        src = registry.SOURCES["gh_actions_cost"]
        self.assertEqual(src.scope, "global")
        self.assertIn("## `gh_actions_cost`", doc)
        for key in src.defaults:
            self.assertIn(key, doc, "gh_actions_cost.%s is undocumented" % key)


DEMO = os.path.join(support.ROOT, "porchlight", "data", "demo", "gh_cost.json")


class Panel(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.addCleanup(support.clear_cache)
        self.addCleanup(T.set_mode, "truecolor")
        self.addCleanup(T.set_ascii, False)

    def load(self):
        with open(DEMO, encoding="utf-8") as fh:
            d = json.load(fh)
        for name, data in d.items():
            if not name.startswith("_"):
                put(name, data)

    def test_the_row_renders_at_every_width(self):
        self.load()
        for cw in (60, 90, 130):
            p = BUILDERS["money"](RCtx(host(), registry.Cache(), {"vitals": {}}, [], cw))
            self.assertIsNotNone(p)
            lines = [T.strip(ln) for ln in p.render(cw, T.ACCENT)]
            for ln in lines:
                self.assertLessEqual(T.width(ln), cw)
            self.assertTrue(any("gh actions" in ln for ln in lines), lines)

    def test_no_record_means_no_row(self):
        p = BUILDERS["money"](RCtx(host(), registry.Cache(), {"vitals": {}}, [], 90))
        self.assertIsNone(p)


if __name__ == "__main__":
    unittest.main()

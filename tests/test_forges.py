"""Additional forge adapters: read-only requests, bounded lists, safe stale data."""
import support  # noqa: F401 — scratch config/cache before importing porchlight

import base64
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from porchlight import collect, core, registry
from porchlight.sources import dev, forges  # noqa: F401 — dev registers the prs source
from support import FakeHTTP, resp

BASE = "https://git.example.com/code"
REPO = "acme/widgets"
BB = "https://api.bitbucket.org/2.0"


def gitea_pull(n, author="me", reviewers=(), **extra):
    p = {"number": n, "title": "A change", "state": "open", "user": {"login": author},
         "requested_reviewers": [{"login": u} for u in reviewers], "mergeable": True,
         "updated_at": "2026-10-21T09:00:00Z"}
    p.update(extra)
    return p


def bb_pull(n, author="{me}", reviewers=(), **extra):
    p = {"id": n, "title": "A change", "state": "OPEN", "author": {"uuid": author},
         "reviewers": [{"uuid": u} for u in reviewers], "updated_on": "2026-10-21T09:00:00Z"}
    p.update(extra)
    return p


def gitea_http(pulls=(), reviews=()):
    return FakeHTTP({"/api/v1/user": resp(200, {"login": "me"}),
                     "/settings/api": resp(200, {"max_response_items": forges.PAGE_SIZE}),
                     "/pulls?": resp(200, list(pulls)),
                     "/reviews?": resp(200, list(reviews), headers={"X-Total-Count": str(len(reviews))})})


def bb_http(pulls=()):
    return FakeHTTP({"/2.0/user": resp(200, {"uuid": "{me}"}),
                     "/pullrequests?": resp(200, {"values": list(pulls)})})


def run_src(settings="", env=None, prev=None, http=None):
    host = support.host("[sources.prs]\ngithub = false\ngitlab = false\n" + settings)
    src = registry.SOURCES["prs"]
    return src.fn(support.ctx(host, src.config(host), env, prev, http))


class TargetTest(unittest.TestCase):
    def test_explicit_targets_and_deduplication(self):
        self.assertEqual(forges.target(BASE + "/", [REPO, REPO]), {"url": BASE, "repos": [REPO]})
        self.assertEqual(forges.target("https://[::1]:9443/git", [REPO])["url"], "https://[::1]:9443/git")

    def test_bad_urls_and_repo_names_fail_before_auth(self):
        # Assemble dummy URL credentials at runtime so scanners do not mistake the rejection fixture for a login.
        userinfo_url = "https://" + ":".join(("name", "pass")) + "@git.example.com"
        urls = ("", "git.example.com", "http://git.example.com", "file:///tmp/x", userinfo_url,
                "https://git.example.com?token=private", "https://git.example.com/#x", "https://git.example.com:bad",
                "https://git.example.com/a/../b", "https://git.example.com/a%2fb", "https://git.example.com\\@other")
        for base in urls:
            fake = gitea_http()
            with self.subTest(base=base), self.assertRaises(forges.ForgeError):
                forges.gitea_prs(fake, "test-token", base, [REPO], 20)
            self.assertEqual(fake.calls, [])
        for repos in ([], REPO, ["../widgets"], ["acme/.."], ["acme/a?token=x"], ["acme/a/b"], [1],
                      ["acme/r%d" % i for i in range(forges.MAX_REPOS + 1)]):
            with self.subTest(repos=repos), self.assertRaises(forges.ForgeError):
                forges.target(BASE, repos)


class GiteaTest(unittest.TestCase):
    def test_selection_and_normalization(self):
        fake = gitea_http([
            gitea_pull(1, mergeable=False),
            gitea_pull(2, author="other", reviewers=("me",)),
            gitea_pull(3, author="other", assignees=[{"login": "me"}]),
            gitea_pull(4, state="closed"),
            gitea_pull(5, draft=True, mergeable=None, title="Draft"),
        ], [{"user": {"login": "a"}, "state": "REQUEST_CHANGES", "submitted_at": "2026-10-20T00:00:00Z"},
            {"user": {"login": "a"}, "state": "APPROVED", "submitted_at": "2026-10-21T00:00:00Z"},
            {"user": {"login": "b"}, "state": "REQUEST_CHANGES", "submitted_at": "2026-10-21T00:00:00Z"},
            {"user": {"login": "b"}, "state": "COMMENT", "submitted_at": "2026-10-22T00:00:00Z"},
            {"user": {"login": "c"}, "state": "APPROVED", "dismissed": True},
            {"user": {"login": "d"}, "state": "REQUEST_CHANGES", "stale": True},
            {"user": {"login": "me"}, "state": "REQUEST_REVIEW"}])
        out = forges.gitea_prs(fake, "gitea-test-token", BASE, [REPO], 20)
        self.assertEqual([r["n"] for r in out["mine"]], [1, 5])
        self.assertEqual([r["n"] for r in out["review"]], [2])
        self.assertEqual(out["mine"][0], {"forge": "gt", "ref": "acme/widgets#1", "n": 1, "title": "A change",
                         "merge": "unknown", "approvals": 1, "changes": 2, "ci": None, "draft": False,
                         "updated": core.iso_epoch("2026-10-21T09:00:00Z"), "url": BASE + "/acme/widgets/pulls/1"})
        self.assertEqual((out["mine_total"], out["review_total"], out["truncated"]), (2, 1, False))
        self.assertTrue(out["mine"][1]["draft"])
        self.assertEqual(out["mine"][1]["merge"], "unknown")
        self.assertTrue(all(c[0] == "GET" and c[1].startswith(BASE + "/api/v1/") for c in fake.calls))
        self.assertTrue(all(c[2] == {"Authorization": "token gitea-test-token"} for c in fake.calls))
        self.assertTrue(all(c[3]["redirects"] is False for c in fake.calls))
        self.assertNotIn("gitea-test-token", json.dumps(out))

    def test_false_mergeable_never_claims_conflicts(self):
        for value in (False, None):
            for forge in ("gt", "fj"):
                with self.subTest(value=value, forge=forge):
                    row = forges.gitea_row(gitea_pull(1, mergeable=value), REPO, BASE, forge)
                    self.assertEqual(row["merge"], "unknown")
                    self.assertEqual(dev.pr_alerts([row]), [])
        self.assertEqual(forges.gitea_row(gitea_pull(1), REPO, BASE)["merge"], "mergeable")

    def test_stale_change_request_clears_only_with_dismissal_or_new_verdict(self):
        request = {"user": {"login": "a"}, "state": "REQUEST_CHANGES", "stale": True,
                   "submitted_at": "2026-10-20T00:00:00Z"}
        comment = {"user": {"login": "a"}, "state": "COMMENT", "submitted_at": "2026-10-21T00:00:00Z"}
        approved = dict(comment, state="APPROVED")
        for reviews, changes, approvals in (([request], 1, 0), ([request, comment], 1, 0),
                                            ([dict(request, dismissed=True)], 0, 0),
                                            ([request, approved], 0, 1),
                                            ([request, dict(approved, stale=True)], 0, 0)):
            with self.subTest(reviews=reviews):
                row = forges.gitea_row(gitea_pull(1), REPO, BASE, reviews=reviews)
                self.assertEqual((row["changes"], row["approvals"]), (changes, approvals))

    def test_wip_prefix_fallback_and_explicit_draft(self):
        for title, extra, draft in (("WIP: Build", {}, True), ("wip: Build", {}, True),
                                    ("[WIP] Build", {}, True), ("[wIp] Build", {}, True),
                                    ("Add WIP: support", {}, False), ("Build", {}, False),
                                    ("WIP: Build", {"draft": False}, False),
                                    ("Build", {"draft": True}, True)):
            with self.subTest(title=title, extra=extra):
                row = forges.gitea_row(gitea_pull(1, title=title, **extra), REPO, BASE)
                self.assertIs(row["draft"], draft)

    def test_server_page_limit_and_headers(self):
        fake = gitea_http()
        def pages(url, *args):
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["page"][0])
            return resp(200, [gitea_pull(page)], headers={"x-total-count": "2"})
        fake.routes["/pulls?"] = pages
        out = forges.gitea_prs(fake, "t", BASE, [REPO], 20, detail=0)
        self.assertEqual([r["n"] for r in out["mine"]], [1, 2])
        self.assertEqual(len(fake.calls), 3)

    def test_rerequest_clears_a_prior_verdict_including_same_second(self):
        for state in ("APPROVED", "REQUEST_CHANGES"):
            with self.subTest(state=state):
                prior = {"id": 1, "user": {"login": "a"}, "state": state, "official": False,
                         "submitted_at": "2026-10-21T00:00:00Z"}
                requested = dict(prior, id=2, state="REQUEST_REVIEW", official=True)
                comment = dict(prior, id=3, state="COMMENT")
                fake = gitea_http([gitea_pull(1)], [comment, requested, prior])
                out = forges.gitea_prs(fake, "t", BASE, [REPO], 20)
                row = out["mine"][0]
                self.assertEqual((row["approvals"], row["changes"]), (0, 0))
                self.assertEqual(dev.pr_alerts([row]), [])
                renewed = dict(prior, id=4, state="REQUEST_CHANGES", official=True)
                fake = gitea_http([gitea_pull(1)], [renewed, comment, requested, prior])
                row = forges.gitea_prs(fake, "t", BASE, [REPO], 20)["mine"][0]
                self.assertEqual(row["changes"], 1)
                self.assertIn("changes requested", dev.pr_alerts([row])[0]["msg"])

    def test_link_pagination_never_follows_foreign_link(self):
        fake = gitea_http()
        fake.routes["/pulls?"] = lambda url, *args: (
            resp(200, [gitea_pull(1)], headers={"Link": '<https://other.invalid/?token=bad>; rel="next"'})
            if "page=1" in url else resp(200, [gitea_pull(2)]))
        out = forges.gitea_prs(fake, "t", BASE, [REPO], 20, detail=0)
        self.assertEqual(out["mine_total"], 2)
        self.assertTrue(all(c[1].startswith(BASE) for c in fake.calls))

    def test_page_cap_dedup_and_detail_limit(self):
        fake = gitea_http([gitea_pull(1)] * forges.PAGE_SIZE)
        out = forges.gitea_prs(fake, "t", BASE, [REPO], 20, detail=0)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["mine_total"], 1)
        self.assertEqual(len(fake.calls), 1 + forges.MAX_PAGES)
        fake = gitea_http([gitea_pull(i) for i in range(1, 21)])
        forges.gitea_prs(fake, "t", BASE, [REPO], 20, detail=9999)
        self.assertEqual(sum("/reviews?" in c[1] for c in fake.calls), 10)

    def test_optional_review_failure_keeps_list(self):
        fake = gitea_http([gitea_pull(1)])
        fake.routes["/reviews?"] = resp(403, {"message": "token echo should not escape"})
        out = forges.gitea_prs(fake, "t", BASE, [REPO], 20, forge="fj")
        self.assertEqual(out["mine"][0]["forge"], "fj")
        self.assertEqual((out["mine"][0]["changes"], out["mine"][0]["approvals"]), (0, 0))

    def test_truncated_review_history_has_no_false_verdict(self):
        fake = gitea_http([gitea_pull(1)], [{"user": {"login": "a"}, "state": "REQUEST_CHANGES"}] * forges.PAGE_SIZE)
        fake.routes["/reviews?"].headers["X-Total-Count"] = "1000"
        out = forges.gitea_prs(fake, "t", BASE, [REPO], 20)
        self.assertEqual(out["mine"][0]["changes"], 0)

    def test_completed_reviewers_require_a_new_request_to_reenter_queue(self):
        for state in ("APPROVED", "REQUEST_CHANGES"):
            with self.subTest(state=state):
                prior = {"id": 1, "user": {"login": "me"}, "state": state,
                         "submitted_at": "2026-10-21T00:00:00Z"}
                pull = gitea_pull(1, author="other", reviewers=("me",))
                fake = gitea_http([pull], [prior])
                out = forges.gitea_prs(fake, "t", BASE, [REPO], 20, detail=0)
                self.assertEqual((out["review"], out["review_total"], out["truncated"]), ([], 0, False))
                requested = dict(prior, id=2, state="REQUEST_REVIEW")
                comment = dict(prior, id=3, state="COMMENT")
                fake = gitea_http([pull], [comment, requested, prior])
                out = forges.gitea_prs(fake, "t", BASE, [REPO], 20, detail=0)
                self.assertEqual([r["n"] for r in out["review"]], [1])
                self.assertEqual((out["review_total"], out["truncated"]), (1, False))

    def test_unconfirmed_review_candidates_are_omitted_and_marked_limited(self):
        requested = {"user": {"login": "me"}, "state": "REQUEST_REVIEW"}
        replies = (resp(500, {"message": "unavailable"}), resp(200, [], headers={"X-Total-Count": "0"}),
                   resp(200, [dict(requested, user={"login": "another"})], headers={"X-Total-Count": "1"}),
                   resp(200, [requested], headers={"X-Total-Count": "1000"}),
                   resp(200, [requested]))  # without page metadata, hidden entries make completeness unknown
        for reply in replies:
            with self.subTest(status=reply.status, body=reply.body, headers=reply.headers):
                fake = gitea_http([gitea_pull(1, author="other", reviewers=("me",))])
                fake.routes["/reviews?"] = reply
                out = forges.gitea_prs(fake, "t", BASE, [REPO], 20, detail=0)
                self.assertEqual((out["review"], out["review_total"], out["truncated"]), ([], 0, True))
                self.assertLessEqual(sum("/reviews?" in c[1] for c in fake.calls), forges.MAX_PAGES)

    def test_review_confirmation_has_its_own_newest_ten_budget(self):
        pulls = [gitea_pull(i) for i in range(1, 3)] + [
            gitea_pull(i, author="other", reviewers=("me",), updated_at=1000 + i) for i in range(3, 15)]
        fake = gitea_http(pulls, [{"user": {"login": "me"}, "state": "REQUEST_REVIEW"}])
        out = forges.gitea_prs(fake, "t", BASE, [REPO], 50, detail=1)
        self.assertEqual([r["n"] for r in out["review"]], list(range(14, 4, -1)))
        self.assertEqual((out["mine_total"], out["review_total"], out["truncated"]), (2, 10, True))
        urls = [c[1] for c in fake.calls]
        self.assertEqual(sum("/settings/api" in u for u in urls), 1)
        self.assertEqual(sum("/reviews?" in u for u in urls), 11)
        self.assertFalse(any("/pulls/2/reviews?" in u or "/pulls/3/reviews?" in u or "/pulls/4/reviews?" in u for u in urls))

    def test_hidden_pending_review_does_not_discard_visible_verdict(self):
        request = {"user": {"login": "a"}, "state": "REQUEST_CHANGES"}
        fake = gitea_http([gitea_pull(1)], [request])
        fake.routes["/reviews?"].headers["X-Total-Count"] = "2"  # second raw entry is a hidden pending review
        out = forges.gitea_prs(fake, "t", BASE, [REPO], 20)
        self.assertEqual(out["mine"][0]["changes"], 1)
        self.assertEqual(sum("/reviews?" in c[1] for c in fake.calls), 1)
        self.assertIn("changes requested", dev.pr_alerts(out["mine"])[0]["msg"])

    def test_server_review_page_cap_and_hidden_empty_pages(self):
        request = {"id": 1, "user": {"login": "a"}, "state": "REQUEST_CHANGES",
                   "submitted_at": "2026-10-21T00:00:00Z"}
        requested = dict(request, id=3, state="REQUEST_REVIEW")
        for pages, changes in (([[request], []], 1), ([[request], [], [requested]], 0)):
            with self.subTest(pages=pages):
                fake = gitea_http([gitea_pull(1)])
                fake.routes["/settings/api"] = resp(200, {"max_response_items": 1})
                def reviews(url, *args):
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                    self.assertEqual(query["limit"], ["1"])
                    page = int(query["page"][0])
                    return resp(200, pages[page - 1], headers={"X-Total-Count": str(len(pages))})
                fake.routes["/reviews?"] = reviews
                out = forges.gitea_prs(fake, "t", BASE, [REPO], 20)
                self.assertEqual(out["mine"][0]["changes"], changes)
                self.assertEqual(sum("/reviews?" in c[1] for c in fake.calls), len(pages))

    def test_unknown_server_page_limit_keeps_lists_without_verdicts(self):
        request = {"user": {"login": "a"}, "state": "REQUEST_CHANGES"}
        for reply in (resp(404, {}), resp(200, {}), resp(200, {"max_response_items": 0}),
                      resp(200, {"max_response_items": True}), resp(200, {"max_response_items": "50"})):
            with self.subTest(status=reply.status, body=reply.body):
                fake = gitea_http([gitea_pull(1), gitea_pull(2, author="other", reviewers=("me",))], [request])
                fake.routes["/settings/api"] = reply
                out = forges.gitea_prs(fake, "t", BASE, [REPO], 20)
                self.assertEqual(([r["n"] for r in out["mine"]], out["review"], out["truncated"]), ([1], [], True))
                self.assertEqual(out["mine"][0]["changes"], 0)
                self.assertFalse(any("/reviews?" in c[1] for c in fake.calls))

    def test_bad_shapes_and_missing_identity_fail(self):
        for response in (resp(200, []), resp(200, {}), resp(200, {"login": None}), resp(401, {"secret": "do not cache"})):
            fake = gitea_http()
            fake.routes["/api/v1/user"] = response
            with self.subTest(response=response.body), self.assertRaises(forges.ForgeError):
                forges.gitea_prs(fake, "t", BASE, [REPO], 20)
        with self.assertRaises(forges.ForgeError):
            forges.gitea_prs(gitea_http([{"number": "unsafe/path"}]), "t", BASE, [REPO], 20)


class BitbucketTest(unittest.TestCase):
    def test_current_token_auth(self):
        env = {"BITBUCKET_EMAIL": "me@example.com", "BITBUCKET_API_TOKEN": "api-token-for-test"}
        fake = bb_http([bb_pull(1)])
        out = forges.bitbucket_prs(fake, env, [REPO], 20)
        auth = "Basic " + base64.b64encode(b"me@example.com:api-token-for-test").decode()
        self.assertTrue(all(c[2] == {"Authorization": auth} for c in fake.calls))
        self.assertNotIn("api-token-for-test", json.dumps(out))
        self.assertNotIn(auth, json.dumps(out))
        self.assertEqual(forges.bitbucket_headers({"BITBUCKET_TOKEN": "user-token"}), {"Authorization": "Bearer user-token"})
        for env in ({}, {"BITBUCKET_APP_PASSWORD": "retired", "BITBUCKET_USER": "me"}, {"BITBUCKET_API_TOKEN": "t"}):
            with self.subTest(env=env), self.assertRaises(forges.ForgeError):
                forges.bitbucket_headers(env)

    def test_selection_expansions_and_normalization(self):
        fake = bb_http([bb_pull(1, participants=[{"approved": True}, {"state": "changes_requested"}]),
                        bb_pull(2, author="{someone}", reviewers=("{me}",)),
                        bb_pull(3, author="{someone}", participants=[{"user": {"uuid": "{me}"}}]),
                        bb_pull(4, state="MERGED"), bb_pull(5, draft=True)])
        out = forges.bitbucket_prs(fake, {"BITBUCKET_TOKEN": "test-token"}, [REPO], 20)
        self.assertEqual([r["n"] for r in out["mine"]], [1, 5])
        self.assertEqual([r["n"] for r in out["review"]], [2])
        self.assertEqual(out["mine"][0], {"forge": "bb", "ref": "acme/widgets#1", "n": 1, "title": "A change",
                         "merge": "unknown", "approvals": 1, "changes": 1, "ci": None, "draft": False,
                         "updated": core.iso_epoch("2026-10-21T09:00:00Z"),
                         "url": "https://bitbucket.org/acme/widgets/pull-requests/1"})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[1][1]).query)
        self.assertEqual(query["fields"], ["+values.reviewers,+values.participants"])
        self.assertEqual((query["state"], query["sort"]), (["OPEN"], ["-updated_on"]))
        self.assertTrue(all(c[0] == "GET" and c[3]["redirects"] is False for c in fake.calls))

    def test_completed_review_is_removed_and_revoked_review_returns(self):
        for verdict in ({"approved": True}, {"state": "approved"}, {"state": "changes_requested"}):
            with self.subTest(verdict=verdict):
                part = dict(verdict, user={"uuid": "{me}"})
                pull = bb_pull(1, author="{other}", reviewers=("{me}",), participants=[part])
                fake = bb_http([pull])
                done = forges.bitbucket_prs(fake, {"BITBUCKET_TOKEN": "t"}, [REPO], 20)
                self.assertEqual((done["review"], done["review_total"]), ([], 0))
                # The assignment remains; the participant's active verdict is revoked.
                part.update(approved=False, state=None)
                pending = forges.bitbucket_prs(fake, {"BITBUCKET_TOKEN": "t"}, [REPO], 20)
                self.assertEqual([r["n"] for r in pending["review"]], [1])
                self.assertEqual(pending["review_total"], 1)

    def test_another_reviewers_verdict_does_not_hide_my_pending_review(self):
        pull = bb_pull(1, author="{other}", reviewers=("{me}",),
                       participants=[{"user": {"uuid": "{someone}"}, "approved": True},
                                     {"user": {"uuid": "{me}"}, "approved": False, "state": None}])
        out = forges.bitbucket_prs(bb_http([pull]), {"BITBUCKET_TOKEN": "t"}, [REPO], 20)
        self.assertEqual([r["n"] for r in out["review"]], [1])

    def test_opaque_next_page_is_followed_with_same_repo(self):
        fake = bb_http()
        fake.routes["/pullrequests?"] = resp(200, {"values": [bb_pull(1)],
            "next": BB + "/repositories/acme/widgets/pullrequests?cursor=opaque%2Bvalue"})
        fake.routes["cursor=opaque%2Bvalue"] = resp(200, {"values": [bb_pull(1), bb_pull(2)]})
        out = forges.bitbucket_prs(fake, {"BITBUCKET_TOKEN": "t"}, [REPO], 20)
        self.assertEqual(out["mine_total"], 2)
        self.assertFalse(out["truncated"])
        self.assertEqual(len(fake.calls), 3)

    def test_next_urls_cannot_change_auth_destination(self):
        nexts = ("https://evil.invalid/2.0/repositories/acme/widgets/pullrequests?page=2",
                 "https://[invalid",
                 "https://api.bitbucket.org@evil.invalid/2.0/repositories/acme/widgets/pullrequests?page=2",
                 "http://api.bitbucket.org/2.0/repositories/acme/widgets/pullrequests?page=2",
                 "https://api.bitbucket.org:444/2.0/repositories/acme/widgets/pullrequests?page=2",
                 BB + "/repositories/other/repo/pullrequests?page=2",
                 BB + "/repositories/acme/widgets/pullrequests#secret")
        for nxt in nexts:
            fake = bb_http()
            fake.routes["/pullrequests?"] = resp(200, {"values": [bb_pull(1)], "next": nxt})
            with self.subTest(nxt=nxt), self.assertRaises(forges.ForgeError):
                forges.bitbucket_prs(fake, {"BITBUCKET_TOKEN": "test-token"}, [REPO], 20)
            self.assertEqual(len(fake.calls), 2)

    def test_repeated_page_and_page_cap(self):
        fake = bb_http()
        fake.routes["/pullrequests?"] = lambda url, *args: resp(200, {"values": [bb_pull(1)], "next": url})
        with self.assertRaises(forges.ForgeError):
            forges.bitbucket_prs(fake, {"BITBUCKET_TOKEN": "t"}, [REPO], 20)
        self.assertEqual(len(fake.calls), 2)
        def page(url, *args):
            n = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("page", ["1"])[0])
            return resp(200, {"values": [bb_pull(n)],
                             "next": BB + "/repositories/acme/widgets/pullrequests?page=%d" % (n + 1)})
        fake = bb_http()
        fake.routes["/pullrequests?"] = page
        out = forges.bitbucket_prs(fake, {"BITBUCKET_TOKEN": "t"}, [REPO], 20)
        self.assertTrue(out["truncated"])
        self.assertEqual((len(fake.calls), out["mine_total"]), (1 + forges.MAX_PAGES, forges.MAX_PAGES))


class AggregateTest(unittest.TestCase):
    SETTINGS = 'gitea = true\ngitea_url = "%s"\ngitea_repos = ["%s"]\ndetail = 0\n' % (BASE, REPO)

    def test_tokens_alone_never_enable_providers(self):
        fake = FakeHTTP({})
        out = run_src(env={"GITEA_TOKEN": "t", "FORGEJO_TOKEN": "f", "BITBUCKET_TOKEN": "b"}, http=fake)
        self.assertEqual((out["forges"], fake.calls), ([], []))

    def test_parallel_providers_use_their_own_credentials(self):
        settings = self.SETTINGS + ('forgejo = true\nforgejo_url = "https://forge.example.com"\n'
                                    'forgejo_repos = ["acme/tools"]\nbitbucket = true\nbitbucket_repos = ["acme/service"]\n')
        fake = gitea_http([gitea_pull(1)])
        fake.routes.update(bb_http([bb_pull(3)]).routes)
        out = run_src(settings, {"GITEA_TOKEN": "gt-token", "FORGEJO_TOKEN": "fj-token", "BITBUCKET_TOKEN": "bb-token"}, http=fake)
        self.assertEqual(out["forges"], ["bb", "fj", "gt"])
        self.assertEqual(out["open"], 3)
        for _, url, hdr, _ in fake.calls:
            want = "Bearer bb-token" if "api.bitbucket.org" in url else "token fj-token" if "forge.example.com" in url else "token gt-token"
            self.assertEqual(hdr["Authorization"], want)

    def test_one_failure_retains_matching_scope_rows_and_counts(self):
        prev = run_src(self.SETTINGS, {"GITEA_TOKEN": "t"}, http=gitea_http([gitea_pull(1)]))
        prev["truncated"] = ["gt"]
        settings = self.SETTINGS + 'bitbucket = true\nbitbucket_repos = ["acme/tools"]\n'
        fake = bb_http([bb_pull(2)])
        fake.routes["/api/v1/user"] = resp(503, {"message": "no service"})
        out = run_src(settings, {"GITEA_TOKEN": "t", "BITBUCKET_TOKEN": "b"}, prev, fake)
        self.assertEqual({r["forge"] for r in out["mine"]}, {"bb", "gt"})
        self.assertEqual(out["counts"]["gt"], {"mine": 1, "review": 0})
        self.assertEqual(out["errors"], {"gt": "HTTP 503"})
        self.assertEqual(out["truncated"], ["gt"])

    def test_same_scope_total_failure_is_soft(self):
        prev = run_src(self.SETTINGS, {"GITEA_TOKEN": "t"}, http=gitea_http([gitea_pull(1)]))
        out = run_src(self.SETTINGS, {"GITEA_TOKEN": "t"}, prev, FakeHTTP({}))
        self.assertTrue(collect._soft_failure(out))

    def test_changed_or_disabled_target_cannot_keep_old_rows_on_failure(self):
        prev = run_src(self.SETTINGS, {"GITEA_TOKEN": "t"}, http=gitea_http([gitea_pull(1)]))
        for settings in (self.SETTINGS.replace(BASE, "https://elsewhere.example.com"),
                         self.SETTINGS.replace(REPO, "acme/elsewhere"),
                         self.SETTINGS.replace(BASE, "http://invalid.example.com"),
                         'bitbucket = true\nbitbucket_repos = ["acme/tools"]\n'):
            out = run_src(settings, {"GITEA_TOKEN": "t"}, prev, FakeHTTP({}))
            self.assertFalse(collect._soft_failure(out))  # collector cannot retain the whole obsolete record
            self.assertEqual((out["mine"], out["counts"], out["open"]), ([], {}, 0))

    def test_secrets_do_not_escape_error_bodies_or_exceptions(self):
        token = "a-test-secret-that-must-not-be-cached"
        for reply in (resp(500, {"message": token}), RuntimeError("Authorization: Bearer " + token),
                      urllib.error.URLError(token)):
            fake = gitea_http()
            fake.routes["/api/v1/user"] = reply
            out = run_src(self.SETTINGS, {"GITEA_TOKEN": token}, http=fake)
            self.assertNotIn(token, json.dumps(out))
        fake = bb_http()
        credentials = base64.b64encode(b"me@example.com:short-token").decode()
        fake.routes["/2.0/user"] = resp(401, {"message": "Basic " + credentials})
        out = run_src('bitbucket = true\nbitbucket_repos = ["acme/tools"]\n',
                      {"BITBUCKET_EMAIL": "me@example.com", "BITBUCKET_API_TOKEN": "short-token"}, http=fake)
        self.assertNotIn(credentials, json.dumps(out))

    def test_global_repo_filter_and_alerts(self):
        settings = self.SETTINGS.replace("detail = 0", "detail = 10")
        reviews = [{"user": {"login": "reviewer"}, "state": "REQUEST_CHANGES", "stale": True}]
        pulls = [gitea_pull(1, mergeable=False), gitea_pull(2, mergeable=False, draft=True),
                 gitea_pull(3, title="wIp: Build"), gitea_pull(4, title="[wIp] Build")]
        out = run_src(settings + 'repos = ["acme/*"]\n', {"GITEA_TOKEN": "t"}, http=gitea_http(pulls, reviews))
        self.assertEqual(len(out["alerts"]), 1)
        self.assertEqual(out["alerts"][0]["msg"], "changes requested on acme/widgets#1")
        no_verdict = run_src(settings, {"GITEA_TOKEN": "t"}, http=gitea_http([gitea_pull(1, mergeable=False)]))
        self.assertNotIn("alerts", no_verdict)
        out = run_src(self.SETTINGS + 'repos = ["other/*"]\n', {"GITEA_TOKEN": "t"}, http=gitea_http([gitea_pull(1)]))
        self.assertEqual((out["mine"], out["open"]), ([], 0))

    def test_deadline_prevents_more_requests(self):
        fake = gitea_http()
        with mock.patch.object(forges.time, "monotonic", side_effect=[0, 26]), self.assertRaises(forges.ForgeError):
            forges.gitea_prs(fake, "t", BASE, [REPO], 20)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()

"""Opt-in, repository-scoped Gitea/Forgejo and Bitbucket Cloud PR adapters.

Only explicit targets receive credentials. Pagination stays on those targets,
and each adapter has both a request bound and a wall-clock budget.
"""
from __future__ import annotations

import re
import time

from .. import core

PAGE_SIZE = 50
MAX_PAGES = 5
MAX_REPOS = 10
BITBUCKET_API = "https://api.bitbucket.org/2.0"
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class ForgeError(Exception):
    """A forge could not provide a trustworthy list."""


def target(base, repos) -> dict:
    """Validate before sending any credentials, and return cache provenance."""
    import urllib.parse
    base = str(base or "").strip().rstrip("/")
    try:
        u = urllib.parse.urlsplit(base)
        valid = (u.scheme == "https" and u.hostname and "@" not in u.netloc
                 and not u.query and not u.fragment and not re.search(r"[\s\\]", base)
                 and re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*", u.path)
                 and all(p not in (".", "..") for p in u.path.split("/")))
        _ = u.port  # Reject malformed port numbers before core.http sees them.
    except ValueError:
        valid = False
    if not valid:
        raise ForgeError("configure an https URL without credentials, query or fragment")
    if not isinstance(repos, list) or not repos or any(
            not isinstance(r, str) or not _REPO.fullmatch(r)
            or any(p in (".", "..") for p in r.split("/")) for r in repos):
        raise ForgeError("configure exact owner/repo names in the provider's repos list")
    repos = sorted(set(repos))
    if len(repos) > MAX_REPOS:
        raise ForgeError("configure at most %d repositories per provider" % MAX_REPOS)
    return {"url": base, "repos": repos}


class _Client:
    def __init__(self, http, headers):
        self.http, self.headers = http, headers
        self.deadline = time.monotonic() + 25

    def get(self, url, kind):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ForgeError("timeout")
        try:
            r = self.http(url, headers=self.headers, timeout=min(8, left), redirects=False)
        except OSError as e:
            raise ForgeError(core.net_reason(e)) from None
        except Exception:  # noqa: BLE001 — exceptions may include a credentialed request
            raise ForgeError("request failed") from None
        if not r.ok:
            # Bodies and exception messages may echo Authorization (including
            # base64 Basic credentials), so only fixed status text leaves here.
            raise ForgeError("unauthorized" if r.status == 401 else "HTTP %d" % r.status)
        if not isinstance(r.body, kind):
            raise ForgeError("invalid API response")
        return r


def _obj(value) -> dict:
    return value if isinstance(value, dict) else {}


def _objects(value) -> list:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _header(headers, key):
    return next((str(v) for k, v in headers.items() if k.lower() == key.lower()), "")


def _gitea_pages(client, url, review_history=False, page_size=PAGE_SIZE):
    """Build our own page URLs; never follow a server-supplied Link target."""
    rows = []
    for page in range(1, MAX_PAGES + 1):
        r = client.get(url + ("&" if "?" in url else "?") + "limit=%d&page=%d" % (page_size, page), list)
        rows.extend(r.body)
        total = _header(r.headers, "X-Total-Count")
        link = _header(r.headers, "Link")
        if link:
            more = bool(re.search(r'rel=["\']?next\b', link))
        elif total.isdigit():
            # Review totals count hidden draft entries before the API filters
            # the page. Visible rows cannot measure progress through that list.
            consumed = page * page_size if review_history else len(rows)
            more = consumed < int(total)
        elif review_history:
            return rows, True  # filtering can make even a full raw page appear empty
        else:
            more = len(r.body) >= page_size
        if not more:
            return rows, False
        if not r.body and not review_history:
            raise ForgeError("incomplete API response")
    return rows, True


def _gitea_reviews(reviews) -> dict:
    """Latest effective event per reviewer; a comment does not revoke a verdict."""
    latest = {}
    for review in sorted(_objects(reviews), key=lambda r: (core.iso_epoch(r.get("submitted_at")) or 0,
                                                           r.get("id") if type(r.get("id")) is int else 0)):
        who = _obj(review.get("user")).get("login")
        state = review.get("state")
        if isinstance(who, str) and who and state in ("APPROVED", "REQUEST_CHANGES", "REQUEST_REVIEW"):
            latest[who] = review  # a re-request clears the previous verdict
    return latest


def gitea_row(p, repo, base, forge="gt", reviews=()) -> dict:
    latest = [r for r in _gitea_reviews(reviews).values() if not r.get("dismissed")]
    merge = p.get("mergeable")
    title = str(p.get("title") or "")
    draft = p.get("draft")
    if not isinstance(draft, bool):
        draft = title.upper().startswith(("WIP:", "[WIP]"))
    return {"forge": forge, "ref": "%s#%d" % (repo, p["number"]), "n": p["number"],
            "title": title[:120],
            # False also means checking, an internal error, or WIP on these
            # forges; it does not prove a git conflict.
            "merge": "mergeable" if merge is True else "unknown",
            # Outdated change requests remain actionable; stale approvals are
            # conservatively ignored without knowing branch protection policy.
            "approvals": sum(r["state"] == "APPROVED" and not r.get("stale") for r in latest),
            "changes": sum(r["state"] == "REQUEST_CHANGES" for r in latest), "ci": None,
            "draft": draft, "updated": core.iso_epoch(p.get("updated_at")) or 0,
            "url": "%s/%s/pulls/%d" % (base, repo, p["number"])}


def gitea_prs(http, token, base, repos, n, detail=10, forge="gt") -> dict:
    """Gitea and Forgejo share these read-only API v1 endpoints."""
    cfg = target(base, repos)
    if not token:
        raise ForgeError("no %s_TOKEN" % ("FORGEJO" if forge == "fj" else "GITEA"))
    client = _Client(http, {"Authorization": "token " + token})
    api = cfg["url"] + "/api/v1"
    me = client.get(api + "/user", dict).body.get("login")
    if not isinstance(me, str) or not me:
        raise ForgeError("no username from /user")
    mine, candidates, truncated = [], [], False
    for repo in cfg["repos"]:
        pulls, limited = _gitea_pages(client, api + "/repos/" + repo + "/pulls?state=open&sort=recentupdate")
        truncated = truncated or limited
        seen = set()
        for p in pulls:
            if not isinstance(p, dict) or type(p.get("number")) is not int or p["number"] <= 0:
                raise ForgeError("invalid pull request response")
            if p.get("state", "open") != "open" or p.get("merged"):
                continue
            if p["number"] in seen:
                continue
            seen.add(p["number"])
            row = gitea_row(p, repo, cfg["url"], forge)
            if _obj(p.get("user")).get("login") == me:
                mine.append((row, p, repo))
            elif any(u.get("login") == me for u in _objects(p.get("requested_reviewers"))):
                candidates.append((row, p, repo))  # this field also includes completed reviewers
    mine.sort(key=lambda item: -item[0]["updated"])
    candidates.sort(key=lambda item: -item[0]["updated"])
    own_detail = mine[:max(0, min(10, detail, n))]
    page_size = None
    if own_detail or candidates:
        try:
            maximum = client.get(api + "/settings/api", dict).body.get("max_response_items")
            if type(maximum) is not int or maximum <= 0:
                raise ForgeError("invalid API page limit")
            page_size = min(PAGE_SIZE, maximum)
        except ForgeError:
            truncated = True  # filtered review pages cannot be counted safely without the server limit
    for i, (_, p, repo) in enumerate(own_detail if page_size else []):
        try:
            reviews, limited = _gitea_pages(client, api + "/repos/%s/pulls/%d/reviews" % (repo, p["number"]),
                                             review_history=True, page_size=page_size)
            if not limited:  # an incomplete history cannot establish the latest verdict
                mine[i] = (gitea_row(p, repo, cfg["url"], forge, reviews), p, repo)
        except ForgeError:
            pass  # optional details must not discard a complete PR list
    review = []
    truncated = truncated or len(candidates) > 10
    for row, p, repo in candidates[:10] if page_size else []:
        try:
            reviews, limited = _gitea_pages(client, api + "/repos/%s/pulls/%d/reviews" % (repo, p["number"]),
                                             review_history=True, page_size=page_size)
            current = _gitea_reviews(reviews).get(me)
            if limited or not current or current.get("dismissed"):
                truncated = True  # show only confirmed pending reviews, with a limited-list note
            elif current["state"] == "REQUEST_REVIEW":
                review.append(row)
        except ForgeError:
            truncated = True
    return {"mine": [item[0] for item in mine[:n]], "review": review[:n],
            "mine_total": len(mine), "review_total": len(review), "truncated": truncated}


def bitbucket_headers(env) -> dict:
    """API token + Atlassian email, or a user OAuth/API bearer token."""
    if env.get("BITBUCKET_API_TOKEN"):
        import base64
        email = env.get("BITBUCKET_EMAIL")
        if not email or ":" in email:
            raise ForgeError("set BITBUCKET_EMAIL with BITBUCKET_API_TOKEN")
        pair = (email + ":" + env["BITBUCKET_API_TOKEN"]).encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
    if env.get("BITBUCKET_TOKEN"):
        return {"Authorization": "Bearer " + env["BITBUCKET_TOKEN"]}
    raise ForgeError("set BITBUCKET_EMAIL and BITBUCKET_API_TOKEN, or BITBUCKET_TOKEN")


def _bitbucket_pages(client, url):
    import urllib.parse
    rows, seen, endpoint = [], set(), urllib.parse.urlsplit(url)
    for _ in range(MAX_PAGES):
        # Keep opaque pagination queries, but bind every URL to this exact
        # repository endpoint (no redirects, userinfo, ports, or other paths).
        u = urllib.parse.urlsplit(url)
        if (u.scheme != endpoint.scheme or u.netloc != endpoint.netloc or u.path != endpoint.path
                or u.fragment or re.search(r"[\s\\]", url) or url in seen):
            raise ForgeError("unsafe or repeated pagination URL")
        seen.add(url)
        body = client.get(url, dict).body
        if not isinstance(body.get("values"), list):
            raise ForgeError("invalid pull request response")
        rows.extend(body["values"])
        nxt = body.get("next")
        if not nxt:
            return rows, False
        if not isinstance(nxt, str) or not body["values"]:
            raise ForgeError("incomplete API response")
        try:
            url = urllib.parse.urljoin(url, nxt)
        except ValueError:
            raise ForgeError("unsafe pagination URL") from None
    return rows, True


def bitbucket_row(p, repo) -> dict:
    parts = _objects(p.get("participants"))
    return {"forge": "bb", "ref": "%s#%d" % (repo, p["id"]), "n": p["id"],
            "title": str(p.get("title") or "")[:120], "merge": "unknown",
            "approvals": sum(bool(x.get("approved")) for x in parts),
            "changes": sum(x.get("state") == "changes_requested" for x in parts), "ci": None,
            "draft": bool(p.get("draft")), "updated": core.iso_epoch(p.get("updated_on")) or 0,
            "url": "https://bitbucket.org/%s/pull-requests/%d" % (repo, p["id"])}


def bitbucket_prs(http, env, repos, n) -> dict:
    import urllib.parse
    cfg = target(BITBUCKET_API, repos)
    client = _Client(http, bitbucket_headers(env))
    me = client.get(BITBUCKET_API + "/user", dict).body.get("uuid")
    if not isinstance(me, str) or not me:
        raise ForgeError("no user UUID from /user; use a user token with account read access")
    mine, review, truncated = [], [], False
    for repo in cfg["repos"]:
        # Expand reviewers and participants; compact list responses omit them.
        query = urllib.parse.urlencode({"state": "OPEN", "sort": "-updated_on", "pagelen": PAGE_SIZE,
                                       "fields": "+values.reviewers,+values.participants"})
        pulls, limited = _bitbucket_pages(client, BITBUCKET_API + "/repositories/" + repo + "/pullrequests?" + query)
        truncated = truncated or limited
        seen = set()
        for p in pulls:
            if not isinstance(p, dict) or type(p.get("id")) is not int or p["id"] <= 0:
                raise ForgeError("invalid pull request response")
            if p.get("state", "OPEN") != "OPEN":
                continue
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            row = bitbucket_row(p, repo)
            if _obj(p.get("author")).get("uuid") == me:
                mine.append(row)
            elif (any(u.get("uuid") == me for u in _objects(p.get("reviewers")))
                  and not any(_obj(part.get("user")).get("uuid") == me
                              and (part.get("approved") is True or part.get("state") in ("approved", "changes_requested"))
                              for part in _objects(p.get("participants")))):
                # Reviewer assignments survive a verdict; only pending reviews
                # belong in this queue. Revoking a verdict makes it pending again.
                review.append(row)
    mine.sort(key=lambda row: -row["updated"])
    review.sort(key=lambda row: -row["updated"])
    return {"mine": mine[:n], "review": review[:n], "mine_total": len(mine), "review_total": len(review),
            "truncated": truncated}

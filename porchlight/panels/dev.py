"""Developer panels: container stacks, pull requests, access (cloud credentials,
ssh routes, endpoints) and EC2 instances. Tailscale, ssh tunnels and dev ports
live in the network panel, so none of that repeats here."""
from __future__ import annotations

from .. import core, registry, theme as T
from ..ui import SEP, Panel, cells, kv, panel

W = 10  # label column
OFF_NET = ("timeout", "no route", "no dns")  # failures that mean "not on this network"
FORGE_NAMES = {"gh": "github", "gl": "gitlab", "gt": "gitea", "fj": "forgejo", "bb": "bitbucket"}


def _age(t) -> str:
    """At most three or four cells so rows line up: 7m, 5h, 12d."""
    if not t:
        return "?"
    s = max(0, core.now() - t)
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def _some(names, n=5) -> str:
    names = list(names)
    return ", ".join(names[:n]) + (" +%d" % (len(names) - n) if len(names) > n else "")


def _sep() -> str:
    """The dim-able " · " (" | " in ASCII mode), resolved at render time."""
    return " %s " % T.g("sep")


def _note(*parts) -> str:
    return _sep().join(p for p in parts if p)


# ── Containers ──────────────────────────────────────────────────────────────
def _exited(f, recent_s) -> str:
    """'api exited 1 12m ago': red while the crash is fresh, dim once it is history."""
    ago = f.get("ago")
    code = "?" if f.get("code") is None else f["code"]
    color = "red" if ago is not None and ago < recent_s else "dim"
    return "%s %s" % (f.get("name", "?"), T.c("exited %s" % code, color)) + \
        (T.dim(" " + core.human_age(ago, " ago")) if ago is not None else "")


@panel("docker", title="DOCKER")
def p_docker(x):
    d = x.data("docker")
    if not d:
        return None
    rows, lvl, cw = [], "ok", x.cw - 1
    eng, name = d.get("engine"), d.get("binary") or "docker"
    ex = d.get("expected") or {}
    if eng == "up":
        recent = float(d.get("recent_s") or 21600)
        projs = d.get("projects") or []
        up = [p for p in projs if p.get("running") or p.get("restarting")]  # crash-looping is not stopped
        for i, p in enumerate(up[:5]):
            failed = p.get("failed") or []
            fresh = [f for f in failed if f.get("ago") is not None and f["ago"] < recent]
            l2 = "crit" if p.get("restarting") else ("warn" if p.get("unhealthy") or fresh else "ok")
            lvl = T.worst(lvl, l2)
            want = p.get("total", 0) - p.get("done", 0)
            bits = [T.sev("%d/%d up" % (p.get("running", 0), want), "ok" if p.get("running", 0) >= want else "warn")]
            if p.get("starting"):
                bits.append(T.c("%d starting" % p["starting"], "sky"))
            if p.get("done"):
                bits.append(T.dim(("%d done" if x.cw < 80 else "%d one-shot done") % p["done"]))
            if p.get("stopped"):
                bits.append(T.dim("%d stopped" % p["stopped"]))
            rows.append(T.fit(kv("stacks" if i == 0 else "", T.dot(l2) + " " + T.c(p["name"], "fg", bold=True) +
                                 "  " + SEP.join(bits), W), cw))
            issues = [n + " " + T.c("unhealthy", "amber") for n in p.get("unhealthy") or []]
            issues += [n + " " + T.c("restarting", "red") for n in p.get("restarting") or []]
            issues += [_exited(f, recent) for f in failed[:3]]
            if issues:  # the marker stays dim when the only news is an old crash
                mark = T.c(T.g("warn") + " ", "amber" if l2 != "ok" else "dim")
                rows.append(T.fit(kv("", "  " + mark + SEP.join(issues[:4]), W), cw))
        if len(up) > 5:
            rows.append(kv("", T.dim("+%d more running stacks" % (len(up) - 5)), W))
        down = [p["name"] for p in projs if not (p.get("running") or p.get("restarting"))]
        if down:  # a stopped stack is a choice: listed, never flagged
            rows.append(T.fit(kv("" if up else "stacks", T.dot("unknown") + T.dim(" stopped: " + _some(down)), W), cw))
        o = d.get("other") or {}
        if o.get("total"):
            bits = ["%d running" % o.get("running", 0) + T.dim(" of %d standalone" % o["total"])]
            bits += [n + " " + T.c("restarting", "red") for n in o.get("restarting") or []]
            bits += [n + " " + T.c("unhealthy", "amber") for n in o.get("unhealthy") or []]
            lvl = T.worst(lvl, "crit" if o.get("restarting") else ("warn" if o.get("unhealthy") else "ok"))
            rows.append(T.fit(kv("other", SEP.join(bits), W), cw))
        if not projs and not o.get("total"):
            rows.append(kv("stacks", T.dot("unknown") + T.dim(" no containers"), W))
    elif eng == "down":
        rows.append(kv(name, T.dot("unknown") + T.dim(" engine not running"), W))
    elif eng == "denied":
        hint = _sep() + "join the docker group" if name == "docker" else ""
        rows.append(T.fit(kv(name, T.dot("unknown") + T.dim(" no access to the engine's socket" + hint), W), cw))
    elif ex or x.cfg.source_configured("docker"):
        rows.append(kv(name, T.dot("unknown") + T.dim(" not installed"), W))
    else:
        return None  # no container engine here and none asked for: nothing worth a panel
    if ex.get("missing"):
        lvl = "crit"
        rows.append(T.fit(kv("expected", T.c(T.g("crit") + " down: " + _some(ex["missing"]), "red"), W), cw))
    elif ex.get("want"):
        rows.append(kv("expected", T.dot("ok") + " %d/%d running" % (ex.get("up", 0), ex["want"]), W))
    badge = ("%s up" % d["badge"]) if d.get("badge") else ""
    return Panel("docker", "DOCKER", rows, lvl, _note(badge, x.stale_note("docker", 5, 900)))


# ── Pull requests ───────────────────────────────────────────────────────────
def _ref(m, short: bool) -> str:
    """'acme/widgets#12'; short drops the owner and groups: 'widgets#12', 'api!34'."""
    ref = str(m.get("ref") or "?")
    if short:
        mark = "#" if "#" in ref else "!"
        repo, _, num = ref.rpartition(mark)
        if repo:
            ref = repo.rsplit("/", 1)[-1] + mark + num
    return ref


def _pr_status(m) -> tuple:
    """(level, cell): what one of your PRs needs from you first."""
    if m.get("draft"):
        return "unknown", T.dim("draft")
    if m.get("changes"):
        return "crit", T.c(T.g("crit") + " changes", "red")
    if m.get("merge") == "conflicts":
        return "warn", T.c("conflicts", "amber")
    if m.get("ci") == "failure":
        return "crit", T.c(T.g("crit") + " ci", "red")
    if m.get("approvals"):
        return "ok", T.c(T.g("ok") + "%d" % m["approvals"], "green")
    if m.get("ci") == "pending":
        return "ok", T.c("ci " + T.ellipsis(), "sky")
    return "ok", T.dim("review")


@panel("prs", title="PULL REQUESTS")
def p_prs(x):
    d = x.data("prs")
    if not d:
        return None
    cw = x.cw - 1
    mine = d.get("mine")
    if mine is None:  # a soft failure before any list arrived: say why
        err = str(d.get("error") or "no data")
        low = err.lower()
        github_rejected = any(e.strip().startswith("gh: unauthorized") for e in low.split(";"))
        hint = "off VPN?" if any(s in low for s in OFF_NET) else ("gh auth login" if github_rejected else "")
        row = T.dot("unknown") + " " + T.dim(err) + ((" " + T.c(hint, "sky")) if hint else "")
        return Panel("prs", "PULL REQUESTS", [T.fit(row, cw)], "unknown", x.stale_note("prs", 4, 3600))
    review = d.get("review") or []
    if not d.get("forges") and not mine and not review:
        return None  # no forge credentials on this machine
    rows, lvl = [], "ok"
    ready = [m for m in mine if not m.get("draft")]
    conf = sum(1 for m in ready if m.get("merge") == "conflicts")
    chg = sum(1 for m in ready if m.get("changes"))
    ci = sum(1 for m in ready if m.get("ci") == "failure")
    appr = sum(1 for m in ready if m.get("approvals") and not m.get("changes"))
    total = max(len(mine), int(d.get("open") or 0))
    if mine:
        bits = [T.c("%d open" % total, "fg", bold=True)]
        if appr:
            bits.append(T.c("%d approved" % appr, "green"))
        if conf:
            bits.append(T.c("%d conflicted" % conf, "amber"))
        if chg:
            bits.append(T.c("%d to fix" % chg, "red"))
        if ci:
            bits.append(T.c("%d failing ci" % ci, "red"))
        if len(mine) > len(ready):
            bits.append(T.dim("%d draft" % (len(mine) - len(ready))))
        rows.append(T.fit(kv("mine", SEP.join(bits), W), cw))
        if conf or chg or ci:
            lvl = "warn"
    else:
        rows.append(kv("mine", T.dim("no open pull requests"), W))
    multi = len({m.get("forge") for m in mine + review}) > 1
    short = x.cw < 80
    shown = mine[:5]
    refs = [_ref(m, short) for m in shown]
    rw = min(max([T.width(r) for r in refs] or [6]), 16 if short else 24)
    tw = max(6, cw - (2 + (3 if multi else 0) + rw + 1 + 1 + 10 + 1 + 4))
    for m, ref in zip(shown, refs):
        l2, status = _pr_status(m)
        mark = T.dim(str(m.get("forge") or "?")[:2] + " ") if multi else ""
        rows.append(T.dot(l2) + " " + mark + T.pad(T.c(ref, "violet"), rw) + " " + T.pad(m.get("title") or "", tw) +
                    " " + T.pad(status, 10, ">") + " " + T.dim(T.pad(_age(m.get("updated")), 4, ">")))
    if total > len(shown):
        rows.append(T.dim("  %s %d more" % (T.ellipsis(), total - len(shown))))
    if review:
        waiting = max(len(review), int(d.get("review_open") or 0))
        rows.append(T.fit(kv("review", T.c("%d waiting on you" % waiting, "sky") +
                             T.dim(_sep() + "  ".join(_ref(m, short) for m in review[:4])), W), cw))
    for forge, err in sorted((d.get("errors") or {}).items()):
        kept = any(m.get("forge") == forge for m in mine + review)
        rows.append(T.fit(kv("", T.dot("unknown") + T.dim(" %s: %s%s" % (
            FORGE_NAMES.get(forge, forge), err, _sep() + "last list kept" if kept else "")), W), cw))
    for forge in d.get("truncated") or []:
        rows.append(T.fit(kv("", T.dim("%s: showing a limited list" % FORGE_NAMES.get(forge, forge)), W), cw))
    note = _note("+".join(d.get("forges") or []), "%d open" % total if total else "", x.stale_note("prs", 4, 3600))
    return Panel("prs", "PULL REQUESTS", rows, lvl, note)


# ── Access: cloud credentials, ssh routes, endpoints ───────────────────────
def _sts_row(a) -> str:
    s = a.get("sts") or {}
    state = s.get("state")
    prof = a.get("profile") or "default"
    if a.get("env"):
        how = "env keys"
    elif prof in (a.get("static") or []):
        how = "keys"
    elif prof in ((a.get("sso") or {}).get("profiles") or []):
        how = "sso"
    else:
        how = ""
    tail = _note(how, prof if prof != "default" else "", a.get("region") or "", _age(s["at"]) if s.get("at") else "")
    tail = T.dim(_sep() + tail) if tail else ""
    if state == "ok":
        return T.dot("ok") + " " + T.c(s.get("principal") or "?", "fg") + T.dim(" @ ") + str(s.get("account") or "?") + tail
    if state == "rejected":
        return T.dot("crit") + " " + T.c("rejected: %s" % s.get("error"), "red") + T.dim(_sep()) + \
            T.c(a.get("login") or "aws sso login", "sky")
    if state == "none":
        others = ((a.get("sso") or {}).get("profiles") or []) + (a.get("static") or [])
        if others:  # the CLI's active profile is empty, but this machine has others
            return T.dot("unknown") + T.dim(" profile %s has no credentials%sprofiles: %s" % (prof, _sep(), _some(others, 3)))
        return T.dot("unknown") + T.dim(" no credentials configured")
    if state == "unreachable":
        return T.dot("unknown") + T.dim(" sts unreachable (%s)" % (s.get("error") or "network"))
    if a.get("cli") is False:
        return T.dot("unknown") + T.dim(" aws CLI not installed")
    return T.dot("unknown") + T.dim(" %s" % (s.get("error") or "not checked yet"))


def _sso_row(a) -> str:
    """The SSO token's own clock. With a refresh token the CLI renews an expired
    access token itself, so the STS row above stays the verdict, but only while
    the SSO session behind it lives (hours, typically): a day past expiry, a fresh
    login is all but certain to be needed, so the hint says so."""
    sso = a.get("sso") or {}
    exp, login = sso.get("expires"), a.get("login") or "aws sso login"
    now = core.now()
    if not exp:
        txt = T.dot("unknown") + T.dim(" not logged in" + _sep()) + T.c(login, "sky")
    elif exp > now:
        txt = T.dot("warn" if exp - now < 3600 and not sso.get("refreshable") else "ok") + \
            " token %s left" % core.human_age(exp - now) + (T.dim(_sep() + "renews") if sso.get("refreshable") else "")
    else:
        renews = sso.get("refreshable") and now - exp < 86400
        txt = T.dot("unknown") + T.dim(" token expired %s" % core.human_age(now - exp, " ago")) + \
            (T.dim(_sep() + "renews on next use") if renews else T.dim(_sep()) + T.c(login, "sky"))
    return txt + (T.dim(_sep() + str(sso["account"])) if sso.get("account") else "")


def _ssh_cell(v) -> str:
    alias = str(v.get("alias") or "?")
    if v.get("ok"):
        return T.dot("ok") + " " + alias + T.dim(" %dms" % (v.get("ms") or 0))
    if v.get("ok") is None:  # behind a ProxyCommand: not probeable from here
        return T.dot("unknown") + " " + T.dim(alias + " " + str(v.get("error") or "?"))
    lv = "unknown" if v.get("error") in OFF_NET else "warn"
    return T.dot(lv) + " " + alias + T.dim(" " + str(v.get("error") or "down"))


def _endpoint_cell(e) -> tuple:
    status = e.get("status") or 0
    if e.get("ok"):
        return "ok", T.dot("ok") + " " + e["label"] + " " + T.dim(str(status)) + \
            (T.c(" " + T.g("via"), "sky") if e.get("via") == "socks" else "")
    if status >= 500:
        return "warn", T.dot("warn") + " " + e["label"] + " " + T.c(str(status), "amber")
    why = str(e.get("error") or ("HTTP %d" % status if status else "down"))
    return "unknown", T.dot("unknown") + " " + T.dim(e.get("label", "?") + " " + why[:10])


def _cloud_row(data, provider) -> str:
    state = data.get("state")
    login = data.get("login") or ("gcloud auth login" if provider == "gcp" else "az login")
    if data.get("cli") is False:
        return T.dot("unknown") + T.dim(" %s CLI not installed" % ("gcloud" if provider == "gcp" else "az"))
    if state in ("ok", "configured"):
        target = data.get("project") if provider == "gcp" else data.get("subscription") or data.get("subscription_id")
        txt = T.dot("ok" if state == "ok" else "unknown") + " " + T.c(data.get("account") or "?", "fg")
        if target:
            txt += T.dim(" @ ") + str(target)
        if state == "configured":
            txt += T.dim(_sep() + "not verified: " + (data.get("error") or "select a project or subscription"))
        return txt
    if state == "rejected":
        return T.dot("warn") + " " + T.c("credentials rejected", "amber") + T.dim(_sep()) + T.c(login, "sky")
    if state == "none":
        return T.dot("unknown") + T.dim(" not signed in" + _sep()) + T.c(login, "sky")
    if state == "unreachable":
        return T.dot("unknown") + T.dim(" unreachable (%s)" % (data.get("error") or "network"))
    return T.dot("unknown") + T.dim(" " + (data.get("error") or "not checked yet"))


@panel("access", title="ACCESS")
def p_access(x):
    rows, lvl, cw = [], "ok", x.cw - 1
    a = x.data("aws_auth")
    if a:
        st = (a.get("sts") or {}).get("state")
        sso = a.get("sso") or {}
        configured = a.get("static") or a.get("env") or sso.get("profiles")
        if configured or st not in (None, "none"):  # an AWS CLI that was never set up is not news
            rows.append(T.fit(kv("aws", _sts_row(a), W), cw))
            if st == "rejected":
                lvl = "crit"
            if sso.get("profiles"):
                rows.append(T.fit(kv("sso", _sso_row(a), W), cw))
    notes = []
    for provider in ("gcp", "azure"):
        name = provider + "_auth"
        data = x.data(name)
        if not data or not x.cfg.source_configured(name) or x.cfg.source(name).get("enabled") is False:
            continue
        src = registry.SOURCES.get(name)
        if src is None or not registry.serves(src, x.host):
            continue
        rows.append(T.fit(kv(provider, _cloud_row(data, provider), W), cw))
        if data.get("state") == "rejected":
            lvl = T.worst(lvl, "warn")
        note = x.stale_note(name, 4, 3600)
        if note:
            notes.append(provider + " " + note)
    r = x.data("reach")
    if r:
        ssh = r.get("ssh") or []
        if ssh:
            if all(v.get("ok") is False and v.get("error") in OFF_NET for v in ssh):
                lines = [T.dot("unknown") + T.dim(" no route to %s%soff VPN?" % (_some([v.get("alias") for v in ssh], 3), _sep()))]
            else:
                lines = cells([_ssh_cell(v) for v in ssh], cw - W, 16)
            rows += [T.fit(kv("ssh" if i == 0 else "", ln, W), cw) for i, ln in enumerate(lines)]
        eps = r.get("endpoints") or []
        items = []
        for e in eps:
            l2, cell = _endpoint_cell(e)
            if l2 == "warn":
                lvl = T.worst(lvl, "warn")
            items.append(cell)
        rows += [T.fit(kv("endpoints" if i == 0 else "", ln, W), cw) for i, ln in enumerate(cells(items, cw - W, 17))]
        if not r.get("socks") and any(not e.get("status") for e in eps):
            # An `ssh -D` proxy shows up in the procs census and the next run routes through it.
            # Flags stay apart: the census reads one flag per word, so a cluster (-fND) is missed.
            hop = str(next((v for v in ssh if v.get("ok")), ssh[0]).get("alias")) if ssh else "<jump host>"
            lead = "off VPN? try " if x.cw < 80 else "off VPN? a SOCKS proxy reaches them: "
            rows.append(T.fit(kv("", T.dim(lead) + T.c("ssh -f -N -D 1080 " + hop, "sky"), W), cw))
    if not rows:
        return None
    if r or a:
        note = x.stale_note("reach", 4, 3600) if r else x.stale_note("aws_auth", 4, 3600)
        if note:
            notes.insert(0, note)
    return Panel("access", "ACCESS", rows, lvl, _sep().join(notes))


# ── EC2 instances ───────────────────────────────────────────────────────────
def _short_type(t: str) -> str:
    return (t or "?").replace("xlarge", "xl").replace("large", "l").replace("medium", "m")


@panel("instances", title="INSTANCES")
def p_instances(x):
    d = x.data("instances")
    if not d:
        return None
    cw = x.cw - 1
    stale = x.stale_note("instances", 4, 3600)
    if d.get("rows") is None:  # a soft failure before any list arrived
        row = T.dot("unknown") + T.dim(" " + str(d.get("error") or "no data"))
        return Panel("instances", "INSTANCES", [T.fit(row, cw)], "unknown", stale)
    allrows = d.get("rows") or []
    if not allrows and not x.cfg.source_configured("instances"):
        return None  # no AWS credentials here, or an empty account, and nothing asked for
    run = [r for r in allrows if r.get("state") == "running"]
    nw = max(6, min(22, max([T.width(str(r.get("name") or "")) for r in run] or [6]), cw - 37))
    rows = []
    for r in run[:8]:
        rows.append(T.dot("ok") + " " + T.pad(T.c(str(r.get("name") or "?"), "fg"), nw) + " " +
                    T.pad(T.dim(_short_type(r.get("type"))), 9) + " " + T.pad(T.dim(r.get("ip") or "-"), 15) + " " +
                    T.dim("up " + (core.human_age(core.now() - r["launched"]) if r.get("launched") else "?")))
    if len(run) > 8:
        rows.append(T.dim("  %s %d more running" % (T.ellipsis(), len(run) - 8)))
    rest = [r for r in allrows if r.get("state") != "running"]
    if rest:  # a stopped instance is a choice (and saves money): summarized, never flagged
        names = [str(r.get("name")) + ("" if r.get("state") == "stopped" else " (%s)" % r.get("state")) for r in rest]
        rows.append(T.fit(T.dot("unknown") + T.dim(" stopped: " + _some(names, 6)), cw))
        hint = next((r.get("start") for r in rest if r.get("start")), "")
        if hint:
            rows.append(T.fit("  " + T.dim("start: ") + T.c(hint, "slate"), cw))
    if not allrows:
        rows.append(T.dim("no instances match the filters"))
    if d.get("aws_error"):
        rows.append(T.fit(T.dot("unknown") + T.dim(" aws: %s%sshowing the cache file" % (d["aws_error"], _sep())), cw))
    note = "%d/%d running" % (d.get("running", len(run)), d.get("total", len(allrows)))
    if d.get("src") == "cache":
        note += _sep() + "cache %s old" % _age(d.get("asof"))
    return Panel("instances", "INSTANCES", rows, "ok", _note(note, stale))

"""Developer sources and panels: docker, aws_auth, instances, prs, reach.

Everything is offline: subprocesses (`docker`, `aws`, `gh`, `ssh -G`, `curl`),
sockets and HTTP are faked, and AWS files live in a temporary directory (the
real ~/.aws is never read).
"""
import support  # noqa: F401 — first: scratch cache and config

import contextlib
import json
import os
import socket
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from porchlight import collect, core, registry, theme as T
from porchlight.panels import dev as pdev  # noqa: F401 — registers the panels
from porchlight.sources import dev
from porchlight.ui import BUILDERS, RCtx
from support import FakeHTTP, put, resp

NOW = time.time()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "porchlight", "data", "demo", "dev.json")


def run_src(name, toml="", env=None, prev=None, http=None):
    """A source's function with its merged settings, as the collector calls it."""
    h = support.host(toml)
    src = registry.SOURCES[name]
    return src.fn(support.ctx(h, src.config(h), env, prev, http))


class FakeRun:
    """Stands in for core.run: answers by the first argv words, records every call."""

    def __init__(self, answers):
        self.answers = answers  # [(tuple of leading argv words after the binary, (rc, out, err))]
        self.calls = []

    def __call__(self, argv, timeout=4.0, env=None, input_text=None, cwd=None):
        self.calls.append({"argv": list(argv), "env": env})
        for words, answer in self.answers:
            if list(argv[1:1 + len(words)]) == list(words):
                return answer(argv) if callable(answer) else answer
        return 127, "", "not found: %s" % argv[0]


@contextlib.contextmanager
def faked(answers, which=lambda name, extra=(): "/usr/bin/" + os.path.basename(name)):
    fake = FakeRun(answers)
    with mock.patch.object(core, "run", fake), mock.patch.object(core, "which", which):
        yield fake


@contextlib.contextmanager
def no_aws_env(**extra):
    """os.environ without any AWS_* variable (the developer's own must not leak in)."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    clean.update(extra)
    with mock.patch.dict(os.environ, clean, clear=True):
        yield


PS = "\n".join("\t".join(r) for r in [
    ("widgets-web-1", "running", "Up 3 hours (healthy)", "widgets", "web"),
    ("widgets-db-1", "running", "Up 3 hours (healthy)", "widgets", "db"),
    ("widgets-migrate-1", "exited", "Exited (0) 3 hours ago", "widgets", "migrate"),
    ("widgets-worker-1", "exited", "Exited (1) About an hour ago", "widgets", "worker"),
    ("widgets-api-1", "running", "Up 2 minutes (unhealthy)", "widgets", "api"),
    ("widgets-cache-1", "running", "Up Less than a second (health: starting)", "widgets", "cache"),
    ("widgets-cron-1", "exited", "Exited (137) 3 days ago", "widgets", "cron"),
    ("gadgets-app-1", "exited", "Exited (143) 2 days ago", "gadgets", "app"),
    ("gadgets-db-1", "exited", "Exited (130) 2 days ago", "gadgets", "db"),
    ("gadgets-seed-1", "exited", "Exited (2) 5 minutes ago", "gadgets", "seed"),
    ("blog-loop-1", "restarting", "Restarting (1) 5 seconds ago", "blog", "loop"),
    ("registry", "running", "Up 5 weeks", "", ""),
    ("scratch", "exited", "Exited (2) Less than a second ago", "", ""),
    ("helper", "restarting", "Restarting (1) 2 seconds ago", "<no value>", "<no value>"),
    ("lonely", "running", "Up 2 days (unhealthy)", "", ""),
    ("half a line",),
])
DOWN_ERR = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"


class ExitInfoTest(unittest.TestCase):
    def test_ages(self):
        cases = {
            "Exited (0) Less than a second ago": (0, 0),
            "Exited (1) 1 second ago": (1, 1),
            "Exited (1) 45 seconds ago": (1, 45),
            "Exited (137) About a minute ago": (137, 60),
            "Exited (2) 12 minutes ago": (2, 720),
            "Exited (1) About an hour ago": (1, 3600),
            "Exited (143) 5 hours ago": (143, 18000),
            "Exited (0) 3 days ago": (0, 259200),
            "Exited (255) 2 weeks ago": (255, 1209600),
            "Exited (1) 3 months ago": (1, 3 * 2592000),
            "Exited (1) 2 years ago": (1, 2 * 31536000),
            "Exited (-1) 4 hours ago": (-1, 14400),
        }
        for status, want in cases.items():
            self.assertEqual(dev._exit_info(status), want, status)

    def test_unparseable(self):
        self.assertEqual(dev._exit_info("Exited (3)"), (3, None))
        self.assertEqual(dev._exit_info("Exited (3) a while back"), (3, None))
        self.assertEqual(dev._exit_info("Up 3 hours"), (None, None))
        self.assertEqual(dev._exit_info(""), (None, None))
        self.assertEqual(dev._exit_info(None), (None, None))


class ParsePsTest(unittest.TestCase):
    def setUp(self):
        self.d = dev.parse_ps(PS)
        self.by = {p["name"]: p for p in self.d["projects"]}

    def test_projects_and_exit_codes(self):
        w = self.by["widgets"]
        self.assertEqual((w["running"], w["total"], w["done"], w["starting"], w["stopped"]), (4, 7, 1, 1, 0))
        self.assertEqual(w["unhealthy"], ["api"])
        self.assertEqual([(f["name"], f["code"], f["ago"]) for f in w["failed"]],
                         [("worker", 1, 3600), ("cron", 137, 259200)])
        g = self.by["gadgets"]  # 143 and 130 are deliberate stops; 2 is a crash
        self.assertEqual((g["running"], g["stopped"], g["done"]), (0, 2, 0))
        self.assertEqual([(f["name"], f["code"]) for f in g["failed"]], [("seed", 2)])
        self.assertEqual(self.by["blog"]["restarting"], ["loop"])

    def test_order_active_first(self):
        # blog crash-loops, so it is active even with nothing running; gadgets is stopped
        self.assertEqual([p["name"] for p in self.d["projects"]], ["blog", "widgets", "gadgets"])
        self.assertTrue(dev.active(self.by["blog"]))
        self.assertFalse(dev.active(self.by["gadgets"]))

    def test_standalone_containers(self):
        o = self.d["other"]
        self.assertEqual((o["running"], o["total"]), (2, 4))  # podman's "<no value>" label = no project
        self.assertEqual(o["restarting"], ["helper"])
        self.assertEqual(o["unhealthy"], ["lonely"])

    def test_ignore_projects(self):
        d = dev.parse_ps(PS, ignore=["wid*", "blog"])
        self.assertEqual([p["name"] for p in d["projects"]], ["gadgets"])

    def test_created_and_paused_are_stopped_not_crashed(self):
        d = dev.parse_ps("a\tcreated\tCreated\tst\tx\nb\tpaused\tUp 2 hours (Paused)\tst\ty")
        p = d["projects"][0]
        self.assertEqual((p["running"], p["stopped"], p["failed"]), (0, 2, []))

    def test_podman_health_starting(self):
        p = dev.parse_ps("a\trunning\tUp 10 seconds (starting)\tst\tx")["projects"][0]
        self.assertEqual(p["starting"], 1)


class StackAlertsTest(unittest.TestCase):
    def test_semantics(self):
        al = dev.stack_alerts(dev.parse_ps(PS)["projects"])
        msgs = [a["msg"] for a in al]
        # widgets: unhealthy api and the hour-old worker crash; the 3-day-old cron crash is history
        self.assertIn("widgets stack: 1 unhealthy (api); worker exited 1", msgs)
        # gadgets is stopped: a choice, even though seed crashed 5 minutes ago
        self.assertFalse(any(m.startswith("gadgets") for m in msgs))
        # blog only restart-loops: reported separately, as critical, by the source
        self.assertFalse(any(m.startswith("blog") for m in msgs))
        self.assertTrue(all(a["sev"] == "warn" for a in al))

    def test_recent_window(self):
        projs = dev.parse_ps(PS)["projects"]
        self.assertFalse(any("worker" in a["msg"] for a in dev.stack_alerts(projs, recent_s=1800)))
        self.assertTrue(any("cron" in a["msg"] for a in dev.stack_alerts(projs, recent_s=4 * 86400)))


class ExpectedTest(unittest.TestCase):
    def setUp(self):
        self.rows = dev.ps_rows(PS)

    def test_names_globs_and_services(self):
        ex = dev.expected_check(self.rows, ["registry", "widgets/*", "db-*", "scratch", "*/db"])
        # widgets/* skips the finished one-shot (migrate, exit 0) and reports worker and cron;
        # db-* matches nothing; scratch exists but is not running; */db matches a running and a stopped db
        self.assertEqual(ex["missing"], ["widgets-worker-1", "widgets-cron-1", "db-*", "scratch", "gadgets-db-1"])
        self.assertEqual((ex["up"], ex["want"]), (1 + 4 + 0 + 0 + 1, 1 + 6 + 1 + 1 + 2))

    def test_literal_needs_one_running(self):
        self.assertEqual(dev.expected_check(self.rows, ["widgets/web"])["missing"], [])
        self.assertEqual(dev.expected_check(self.rows, ["widgets-migrate-1"])["missing"], ["widgets-migrate-1"])

    def test_expected_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "inv.json")
            with open(p, "w") as f:
                json.dump(["registry", "web-*", 7, ""], f)
            pats, problem = dev.expected_entries({"expected": ["registry", "x"], "expected_file": p})
            self.assertEqual((pats, problem), (["registry", "x", "web-*"], None))
            with open(p, "w") as f:
                json.dump({"running": ["a", "b"]}, f)
            self.assertEqual(dev.expected_entries({"expected_file": p})[0], ["a", "b"])
            with open(p, "w") as f:
                f.write("{not json")
            pats, problem = dev.expected_entries({"expected": ["a"], "expected_file": p})
            self.assertEqual(pats, ["a"])
            self.assertIn("not a JSON list", problem)


class DockerSourceTest(unittest.TestCase):
    def run_docker(self, answer, toml="", which=None):
        which = which or (lambda name, extra=(): "/usr/bin/docker" if name == "docker" else None)
        with faked([(("ps",), answer)], which) as fake:
            return run_src("docker", toml), fake

    def test_up(self):
        d, fake = self.run_docker((0, PS, ""))
        self.assertEqual(d["engine"], "up")
        self.assertEqual(fake.calls[0]["argv"][:3], ["/usr/bin/docker", "ps", "-a"])
        msgs = {a["msg"]: a for a in d["alerts"]}
        loop = msgs["restart loop: blog-loop-1, helper"]
        self.assertEqual(loop["sev"], "crit")
        self.assertEqual(loop["fix"], "docker logs --tail 50 blog-loop-1")
        self.assertIn("widgets stack: 1 unhealthy (api); worker exited 1", msgs)
        self.assertEqual(msgs["unhealthy container: lonely"]["sev"], "warn")
        # running / what the running stacks should have up (+ standalone running)
        self.assertEqual(d["badge"], "%d/%d" % (0 + 4 + 2, 1 + 6 + 2))
        self.assertEqual(d["recent_s"], 6 * 3600)

    def test_ignored_project_never_alerts(self):
        d, _ = self.run_docker((0, PS, ""), '[sources.docker]\nignore_projects = ["blog", "widgets"]\n')
        self.assertEqual([a["msg"] for a in d["alerts"] if a["sev"] == "crit"], ["restart loop: helper"])

    def test_expected_containers(self):
        toml = '[sources.docker]\nexpected = ["registry", "db-*", "widgets/web"]\n'
        d, _ = self.run_docker((0, PS, ""), toml)
        down = [a for a in d["alerts"] if a["msg"].startswith("container down")]
        self.assertEqual(down, [{"sev": "crit", "msg": "container down: db-*"}])
        self.assertEqual(d["expected"], {"want": 3, "up": 2, "missing": ["db-*"]})
        self.assertEqual(d["badge"], "2/3")

    def test_engine_down_is_state(self):
        d, _ = self.run_docker((1, "", DOWN_ERR))
        self.assertEqual(d["engine"], "down")
        self.assertNotIn("alerts", d)
        d, _ = self.run_docker((124, "", "timeout after 8.0s"))
        self.assertEqual(d["engine"], "down")

    def test_engine_down_with_expected_containers(self):
        d, _ = self.run_docker((1, "", DOWN_ERR), '[sources.docker]\nexpected = ["web"]\n')
        self.assertEqual(d["alerts"], [{"sev": "crit", "msg": "container down: web (docker not running)"}])
        self.assertEqual(d["badge"], "0/1")

    def test_missing_engine(self):
        d, fake = self.run_docker((0, PS, ""), which=lambda name, extra=(): None)
        self.assertEqual((d["engine"], d["binary"]), ("missing", "docker"))
        self.assertNotIn("alerts", d)
        self.assertEqual(fake.calls, [])
        with faked([], lambda name, extra=(): None):
            d = run_src("docker", '[sources.docker]\nexpected = ["web"]\n')
        self.assertEqual(d["alerts"][0]["msg"], "container down: web (docker not installed)")

    def test_podman_fallback_and_binary_setting(self):
        which = lambda name, extra=(): "/opt/podman/bin/podman" if name == "podman" else None  # noqa: E731
        d, fake = self.run_docker((0, "", ""), which=which)
        self.assertEqual((d["engine"], d["binary"]), ("up", "podman"))
        self.assertIn("/opt/podman/bin", dev.ENGINE_DIRS)
        seen = []
        with faked([(("ps",), (0, "", ""))], lambda name, extra=(): seen.append(name) or name):
            run_src("docker", '[sources.docker]\nbinary = "/usr/local/bin/nerdctl"\n')
        self.assertEqual(seen, ["/usr/local/bin/nerdctl"])

    def test_permission_denied(self):
        err = "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock"
        d, _ = self.run_docker((1, "", err))
        self.assertEqual(d["engine"], "denied")
        self.assertNotIn("alerts", d)
        d, _ = self.run_docker((1, "", err), '[sources.docker]\nexpected = ["web"]\n')
        self.assertEqual([a["sev"] for a in d["alerts"]], ["warn"])
        self.assertNotIn("badge", d)

    def test_unknown_failure_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_docker((1, "", "unknown flag: --format"))

    def test_nothing_running_has_no_badge(self):
        d, _ = self.run_docker((0, "a\texited\tExited (0) 1 day ago\tst\tx", ""))
        self.assertNotIn("badge", d)
        self.assertNotIn("alerts", d)


# ── AWS ─────────────────────────────────────────────────────────────────────
STS_OK = json.dumps({"UserId": "AROAEXAMPLE:dev", "Account": "123456789012",
                     "Arn": "arn:aws:sts::123456789012:assumed-role/DeveloperAccess/dev-session"})


class ClassifyStsTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(dev.classify_sts(0, STS_OK, ""),
                         {"state": "ok", "account": "123456789012", "principal": "role/DeveloperAccess"})
        user = json.dumps({"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/ci-bot"})
        self.assertEqual(dev.classify_sts(0, user, "")["principal"], "user/ci-bot")
        self.assertEqual(dev.classify_sts(0, "not json", ""), {"state": "error", "error": "unparseable sts reply"})

    def test_failures(self):
        cases = [
            (254, "An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: "
                  "The security token included in the request is expired", "rejected", "ExpiredToken"),
            (254, "An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation: "
                  "The security token included in the request is invalid.", "rejected", "InvalidClientTokenId"),
            (255, "Error when retrieving token from sso: Token has expired and refresh failed", "rejected", None),
            (255, "Error loading SSO Token: Token for dev does not exist", "rejected", None),
            (253, 'Unable to locate credentials. You can configure credentials by running "aws configure".',
             "none", None),
            (255, "The config profile (scratch) could not be found", "none", "scratch"),
            (255, 'Could not connect to the endpoint URL: "https://sts.us-west-2.amazonaws.com/"', "unreachable", None),
            (255, "Connect timeout on endpoint URL", "unreachable", None),
            (124, "", "unreachable", "rc 124"),
            (255, "botocore.exceptions.SomethingNew: odd", "error", None),
            (1, "", "error", "rc 1"),
        ]
        for rc, err, state, code in cases:
            got = dev.classify_sts(rc, "", err)
            self.assertEqual(got["state"], state, err)
            if code:
                self.assertEqual(got["error"], code, err)
            self.assertTrue(got["error"])


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


CONFIG = """[default]
region = us-west-2

[profile dev]
sso_session = corp
sso_account_id = 123456789012
sso_role_name = DeveloperAccess
region = eu-west-1

[profile legacy-sso]
sso_start_url = https://example.awsapps.com/start
sso_account_id = 210987654321

[profile inline-keys]
aws_access_key_id = AKIAEXAMPLEINLINE0000

[sso-session corp]
sso_start_url = https://example.awsapps.com/start
sso_region = us-west-2

[services local]
s3 =
  endpoint_url = http://localhost:9000
"""
CREDS = """[default]
aws_access_key_id = AKIAEXAMPLEDEFAULT000
aws_secret_access_key = example/secret/not/real

[ci]
aws_access_key_id = AKIAEXAMPLECI00000000
aws_secret_access_key = example/secret/not/real

[empty]
region = us-east-1
"""


def fake_aws_dir(d, newest="2026-10-21T18:00:00Z"):
    write(os.path.join(d, "config"), CONFIG)
    write(os.path.join(d, "credentials"), CREDS)
    cache = os.path.join(d, "sso", "cache")
    tok = {"startUrl": "https://example.awsapps.com/start", "region": "us-west-2", "accessToken": "example-token"}
    write(os.path.join(cache, "a.json"), json.dumps(dict(tok, expiresAt=newest, refreshToken="example-refresh")))
    write(os.path.join(cache, "b.json"), json.dumps(dict(tok, expiresAt="2026-10-20T08:00:00Z")))
    # newer, but not an SSO token (no startUrl), and a client registration: both skipped
    write(os.path.join(cache, "c.json"), json.dumps({"accessToken": "x", "expiresAt": "2030-01-01T00:00:00Z"}))
    write(os.path.join(cache, "botocore-client-id-us-west-2.json"),
          json.dumps(dict(tok, expiresAt="2031-01-01T00:00:00Z")))
    write(os.path.join(cache, "broken.json"), "{")
    write(os.path.join(cache, "notes.txt"), "not json")


class AwsFilesTest(unittest.TestCase):
    def test_parse(self):
        with tempfile.TemporaryDirectory() as d:
            fake_aws_dir(d)
            f = dev.aws_files(d, env={})
        self.assertEqual(f["static"], ["ci", "default", "inline-keys"])
        self.assertEqual(f["sso"]["profiles"], ["dev", "legacy-sso"])
        self.assertEqual(f["sso"]["expires"], core.iso_epoch("2026-10-21T18:00:00Z"))
        self.assertTrue(f["sso"]["refreshable"])
        self.assertEqual((f["profile"], f["region"], f["env"]), ("default", "us-west-2", False))
        self.assertEqual(f["sso"]["account"], "123456789012")
        self.assertNotIn("example/secret", json.dumps(f))  # no key or token is ever read out
        self.assertNotIn("example-token", json.dumps(f))

    def test_profile_env_and_paths(self):
        with tempfile.TemporaryDirectory() as d:
            fake_aws_dir(d, newest="2026-10-22T01:00:00UTC")  # the CLI v1 spelling
            f = dev.aws_files(d, env={"AWS_PROFILE": "legacy-sso", "AWS_ACCESS_KEY_ID": "x"})
            self.assertEqual((f["profile"], f["sso"]["account"], f["env"]), ("legacy-sso", "210987654321", True))
            self.assertEqual(f["sso"]["expires"], core.iso_epoch("2026-10-22T01:00:00Z"))
            self.assertEqual(dev.aws_files(d, env={}, profile="dev")["region"], "eu-west-1")
            other = os.path.join(d, "elsewhere")
            write(other, "[profile solo]\nsso_session = s\n")
            f = dev.aws_files(d, env={"AWS_CONFIG_FILE": other, "AWS_SHARED_CREDENTIALS_FILE": other + ".none"})
            self.assertEqual((f["sso"]["profiles"], f["static"]), (["solo"], []))

    def test_nothing_there(self):
        with tempfile.TemporaryDirectory() as d:
            f = dev.aws_files(d, env={})
        self.assertEqual((f["static"], f["sso"]["profiles"], f["sso"]["expires"]), ([], [], None))

    def test_env_scrub(self):
        with no_aws_env(AWS_BEARER_TOKEN_BEDROCK="bearer-example", AWS_REGION="us-east-1"):
            env = dev.aws_env({"AWS_PROFILE": "dev", "AWS_BEARER_TOKEN_OTHER": "y", "GITHUB_TOKEN": "z"}, "eu-west-1")
        self.assertNotIn("AWS_BEARER_TOKEN_BEDROCK", env)
        self.assertNotIn("AWS_BEARER_TOKEN_OTHER", env)
        self.assertEqual((env["AWS_PROFILE"], env["AWS_REGION"], env["AWS_DEFAULT_REGION"]), ("dev", "eu-west-1", "eu-west-1"))
        self.assertEqual((env["AWS_PAGER"], env["AWS_CLI_AUTO_PROMPT"]), ("", "off"))
        self.assertNotEqual(env.get("GITHUB_TOKEN"), "z")  # only AWS_* settings come from the secrets files


class AwsAuthSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake_aws_dir(self.tmp.name)
        self.patch = mock.patch.object(dev, "AWS_DIR", self.tmp.name)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_auth(self, answer, toml="", prev=None, env=None):
        with no_aws_env(AWS_BEARER_TOKEN_BEDROCK="bearer-example"), faked([(("sts",), answer)]) as fake:
            return run_src("aws_auth", toml, env=env, prev=prev), fake

    def test_ok_then_cached(self):
        d, fake = self.run_auth((0, STS_OK, ""))
        self.assertEqual(d["sts"]["state"], "ok")
        self.assertEqual(d["sts"]["principal"], "role/DeveloperAccess")
        self.assertEqual(len(fake.calls), 1)
        argv, env = fake.calls[0]["argv"], fake.calls[0]["env"]
        self.assertEqual(argv[1:5], ["sts", "get-caller-identity", "--output", "json"])
        self.assertNotIn("--region", argv)  # no region setting: the CLI's own default
        self.assertNotIn("AWS_BEARER_TOKEN_BEDROCK", env)
        self.assertNotIn("alerts", d)
        # same files, checked just now: no second network call
        d2, fake = self.run_auth((0, STS_OK, ""), prev=json.loads(json.dumps(d)))
        self.assertEqual(fake.calls, [])
        self.assertEqual(d2["sts"], d["sts"])

    def test_recheck_when_files_change_or_time_passes(self):
        d, _ = self.run_auth((0, STS_OK, ""))
        os.utime(os.path.join(self.tmp.name, "config"), (NOW - 50, NOW - 50))  # `aws configure`, say
        d, fake = self.run_auth((0, STS_OK, ""), prev=json.loads(json.dumps(d)))
        self.assertEqual(len(fake.calls), 1)
        # a fresh `aws sso login` writes a token with a new expiry: that changes the fingerprint too
        cache = os.path.join(self.tmp.name, "sso", "cache", "a.json")
        tok = core.read_json(cache)
        write(cache, json.dumps(dict(tok, expiresAt="2026-10-22T06:00:00Z")))
        d, fake = self.run_auth((0, STS_OK, ""), prev=json.loads(json.dumps(d)))
        self.assertEqual(len(fake.calls), 1)
        old = json.loads(json.dumps(d))
        old["sts"]["at"] = NOW - 31 * 60
        _, fake = self.run_auth((0, STS_OK, ""), prev=old)
        self.assertEqual(len(fake.calls), 1)  # 31 min > recheck_minutes = 30
        _, fake = self.run_auth((0, STS_OK, ""), prev=old, toml="[sources.aws_auth]\nrecheck_minutes = 60\n")
        self.assertEqual(len(fake.calls), 0)

    def test_unreachable_is_always_retried(self):
        d, _ = self.run_auth((255, "", 'Could not connect to the endpoint URL: "https://sts.amazonaws.com/"'))
        self.assertEqual(d["sts"]["state"], "unreachable")
        self.assertNotIn("alerts", d)  # off the network is state
        _, fake = self.run_auth((0, STS_OK, ""), prev=json.loads(json.dumps(d)))
        self.assertEqual(len(fake.calls), 1)

    def test_profile_change_rechecks(self):
        d, _ = self.run_auth((0, STS_OK, ""))
        _, fake = self.run_auth((0, STS_OK, ""), prev=json.loads(json.dumps(d)), toml='[sources.aws_auth]\nprofile = "dev"\n')
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["argv"][-2:], ["--profile", "dev"])

    def test_rejected_alert_and_fix(self):
        err = "An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: expired"
        # the active profile (default) is not an SSO one, so the hint names one that is
        d, _ = self.run_auth((254, "", err))
        self.assertEqual(d["alerts"], [{"sev": "warn", "msg": "AWS credentials rejected (ExpiredToken)",
                                        "fix": "aws sso login --profile dev"}])
        d, _ = self.run_auth((254, "", err), env={"AWS_PROFILE": "legacy-sso"})  # already an SSO profile
        self.assertEqual(d["alerts"][0]["fix"], "aws sso login")
        d, fake = self.run_auth((254, "", err), toml='[sources.aws_auth]\nprofile = "dev"\nregion = "eu-west-1"\n')
        self.assertEqual(d["alerts"][0]["fix"], "aws sso login --profile dev")
        self.assertEqual(fake.calls[0]["argv"][-4:], ["--region", "eu-west-1", "--profile", "dev"])
        self.assertEqual(fake.calls[0]["env"]["AWS_REGION"], "eu-west-1")
        self.assertEqual(d["region"], "eu-west-1")
        d, _ = self.run_auth((254, "", err), toml='[sources.aws_auth]\nlogin_command = "corp-login --aws"\n')
        self.assertEqual(d["alerts"][0]["fix"], "corp-login --aws")

    def test_static_keys_get_aws_configure(self):
        os.remove(os.path.join(self.tmp.name, "config"))  # no SSO profiles left
        d, _ = self.run_auth((254, "", "An error occurred (InvalidClientTokenId) when calling it"))
        self.assertEqual(d["alerts"][0]["fix"], "aws configure")
        self.assertEqual(d["static"], ["ci", "default"])

    def test_none_is_not_an_alert(self):
        d, _ = self.run_auth((253, "", "Unable to locate credentials. You can configure credentials by running"))
        self.assertEqual(d["sts"]["state"], "none")
        self.assertNotIn("alerts", d)

    def test_secrets_file_aws_settings_reach_the_cli(self):
        _, fake = self.run_auth((0, STS_OK, ""), env={"AWS_PROFILE": "legacy-sso", "OPENAI_API_KEY": "sk-example"})
        env = fake.calls[0]["env"]
        self.assertEqual(env["AWS_PROFILE"], "legacy-sso")
        self.assertNotEqual(env.get("OPENAI_API_KEY"), "sk-example")

    def test_no_cli(self):
        with no_aws_env(), faked([], lambda name, extra=(): None):
            d = run_src("aws_auth")
        self.assertEqual((d["cli"], d.get("sts")), (False, None))
        self.assertEqual(d["sso"]["profiles"], ["dev", "legacy-sso"])


# ── EC2 instances ───────────────────────────────────────────────────────────
EC2 = "\n".join("\t".join(r) for r in [
    ("devbox", "m7i.xlarge", "10.0.1.23", "i-0abc3456def789012", "2026-10-18T08:00:00+00:00", "running"),
    ("None", "t3.micro", "None", "i-0abc9999def000000", "2026-10-21T09:30:00.000Z", "pending"),
    ("gpu-box", "g5.xlarge", "10.0.2.15", "i-0abc4567def890123", "2026-10-12T12:00:00+00:00", "stopped"),
    ("bastion", "t3.small", "10.0.0.10", "i-0abc1234def567890", "2026-09-10T12:00:00+00:00", "running"),
    ("winding-down", "t3.small", "10.0.0.11", "i-0abc1111def222222", "2026-10-01T00:00:00+00:00", "stopping"),
    ("", "x", "y", "not-an-instance", "z", "running"),
    ("too", "short"),
])


class InstancesTest(unittest.TestCase):
    def test_parse(self):
        rows = dev.parse_instances(EC2)
        self.assertEqual([(r["name"], r["state"]) for r in rows], [
            ("bastion", "running"), ("devbox", "running"), ("i-0abc9999def000000", "pending"),
            ("winding-down", "stopping"), ("gpu-box", "stopped")])
        dbx = rows[1]
        self.assertEqual((dbx["type"], dbx["ip"], dbx["id"]), ("m7i.xlarge", "10.0.1.23", "i-0abc3456def789012"))
        self.assertEqual(dbx["launched"], core.iso_epoch("2026-10-18T08:00:00Z"))
        self.assertEqual(rows[2]["ip"], "")  # "None" is the CLI's text for a missing value

    def run_inst(self, answer, toml="", prev=None):
        with no_aws_env(), faked([(("ec2", "describe-instances"), answer)]) as fake:
            return run_src("instances", toml, prev=prev), fake

    def test_from_aws(self):
        toml = '[sources.instances]\nregion = "us-west-2"\nprofile = "dev"\nfilters = ["Name=tag:Owner,Values=me"]\n'
        d, fake = self.run_inst((0, EC2, ""), toml)
        argv = fake.calls[0]["argv"]
        i = argv.index("--filters")
        self.assertEqual(argv[i + 1:i + 3], ["Name=tag:Owner,Values=me", dev._LIVE])
        self.assertEqual(argv[-4:], ["--region", "us-west-2", "--profile", "dev"])
        self.assertIn("--max-items", argv)
        self.assertEqual((d["src"], d["running"], d["total"]), ("aws", 2, 5))
        stopped = next(r for r in d["rows"] if r["name"] == "gpu-box")
        self.assertEqual(stopped["start"], "aws ec2 start-instances --instance-ids i-0abc4567def890123"
                                           " --region us-west-2 --profile dev")
        self.assertNotIn("start", d["rows"][0])
        self.assertFalse(collect._soft_failure(d))

    def test_custom_hint_and_state_filter(self):
        toml = ('[sources.instances]\nstart_hint = "devctl wake {name}"\n'
                'filters = ["Name=instance-state-name,Values=running"]\n')
        d, fake = self.run_inst((0, EC2, ""), toml)
        self.assertNotIn(dev._LIVE, fake.calls[0]["argv"])  # the user's own state filter wins
        self.assertEqual(next(r for r in d["rows"] if r["state"] == "stopped")["start"], "devctl wake gpu-box")

    def test_cache_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "instances.tsv")
            write(path, EC2)
            toml = '[sources.instances]\ncache_file = "%s"\n' % path
            err = "An error occurred (ExpiredToken) when calling the DescribeInstances operation: expired"
            d, _ = self.run_inst((254, "", err), toml)
            self.assertEqual((d["src"], d["aws_error"], d["total"]), ("cache", "ExpiredToken", 5))
            self.assertAlmostEqual(d["asof"], os.path.getmtime(path), places=3)
            self.assertFalse(collect._soft_failure(d))  # a usable list: stored, not discarded
            # our own last AWS list is newer than the cache file: keep it (soft failure)
            d, _ = self.run_inst((254, "", err), toml, prev={"src": "aws", "asof": NOW + 60, "rows": [{}]})
            self.assertTrue(collect._soft_failure(d))

    def test_failure_without_cache_is_soft(self):
        d, _ = self.run_inst((255, "", 'Could not connect to the endpoint URL: "https://ec2.example"'))
        self.assertEqual(d, {"error": "describe-instances: Could not connect to the endpoint URL: "
                                      '"https://ec2.example"', "status": "unknown"})
        self.assertTrue(collect._soft_failure(d))

    def test_no_credentials_is_quiet(self):
        d, _ = self.run_inst((253, "", "Unable to locate credentials. You can configure credentials by running"))
        self.assertEqual((d["state"], d["rows"]), ("none", []))
        self.assertFalse(collect._soft_failure(d))


# ── Pull and merge requests ─────────────────────────────────────────────────
def gh_node(number, repo="acme/widgets", title="A change", mergeable="MERGEABLE", decision=None, reviews=(),
            rollup="SUCCESS", draft=False, updated="2026-10-21T09:00:00Z"):
    return {"number": number, "title": title, "url": "https://github.com/%s/pull/%d" % (repo, number),
            "isDraft": draft, "updatedAt": updated, "mergeable": mergeable, "reviewDecision": decision,
            "repository": {"nameWithOwner": repo},
            "latestOpinionatedReviews": {"nodes": [{"state": s} for s in reviews]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": rollup} if rollup else None}}]}}


GH_BODY = {"data": {
    "mine": {"issueCount": 7, "nodes": [
        gh_node(128, title="Add a retry budget", decision="APPROVED", reviews=("APPROVED", "APPROVED", "COMMENTED")),
        gh_node(131, title="Fix a race", mergeable="CONFLICTING", rollup="FAILURE", updated="2026-10-20T09:00:00Z"),
        gh_node(77, repo="acme/gadgets", decision="CHANGES_REQUESTED", reviews=("CHANGES_REQUESTED",),
                rollup="PENDING", updated="2026-10-19T09:00:00Z"),
        gh_node(140, mergeable="CONFLICTING", rollup="ERROR", draft=True, updated="2026-10-18T09:00:00Z"),
        gh_node(12, repo="acme/docs", mergeable="UNKNOWN", rollup=None, decision="APPROVED",
                updated="2026-10-17T09:00:00Z"),
        None,  # a PR hidden by SAML enforcement comes back as null
    ]},
    "review": {"issueCount": 1, "nodes": [gh_node(133, title="Cache negative DNS answers", rollup="EXPECTED")]},
}}


def gl_mr(iid, ref, title="An MR", has_conflicts=False, dms="mergeable", draft=False,
          updated="2026-10-21T08:00:00.000Z", pid=42):
    return {"id": 9000 + iid, "iid": iid, "project_id": pid, "title": title, "has_conflicts": has_conflicts,
            "detailed_merge_status": dms, "merge_status": "can_be_merged", "draft": draft, "updated_at": updated,
            "references": {"full": ref}, "web_url": "https://gitlab.example.com/x/-/merge_requests/%d" % iid}


def gl_routes(mine_total="3"):
    return {
        "/api/v4/user": resp(200, {"id": 7, "username": "dev"}),
        "scope=created_by_me": resp(200, [
            gl_mr(34, "acme/infra!34", "Pin provider versions", updated="2026-10-21T10:00:00Z"),
            gl_mr(35, "acme/infra!35", "Conflicted", has_conflicts=True, dms="conflict", updated="2026-10-20T10:00:00Z"),
            gl_mr(36, "acme/infra!36", "Waiting on review", dms="requested_changes", draft=False,
                  updated="2026-10-16T10:00:00Z"),
        ], headers={"X-Total": mine_total}),
        "reviewer_username=dev": resp(200, [gl_mr(212, "acme/platform/api!212", "Rate limits", dms="ci_still_running",
                                                  pid=43)], headers={"x-total": "1"}),
        "/merge_requests/34/approvals": resp(200, {"approved": True, "approved_by": [{"user": {"username": "a"}},
                                                                                     {"user": {"username": "b"}}]}),
        "/merge_requests/34/pipelines": resp(200, [{"id": 1, "status": "success"}]),
        "/merge_requests/35/approvals": resp(200, {"approved_by": []}),
        "/merge_requests/35/pipelines": resp(200, [{"id": 2, "status": "failed"}]),
        "/merge_requests/36/approvals": resp(500, {"message": "boom"}),
        "/merge_requests/36/pipelines": resp(200, []),
    }


class GithubTest(unittest.TestCase):
    def test_normalize(self):
        fake = FakeHTTP({"api.github.com/graphql": resp(200, GH_BODY)})
        out = dev.github_prs(fake, "gho_exampletoken", 20)
        method, url, headers, kw = fake.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.github.com/graphql"))
        self.assertEqual(headers["Authorization"], "bearer gho_exampletoken")
        self.assertIs(kw["redirects"], False)
        v = kw["json_body"]["variables"]
        self.assertIn("author:@me", v["mine"])
        self.assertIn("review-requested:@me", v["review"])
        self.assertTrue(v["mine"].startswith("is:pr is:open"))
        self.assertEqual(v["n"], 20)
        self.assertTrue(kw["json_body"]["query"].lstrip().startswith("query("))  # a query, never a mutation
        self.assertNotIn("mutation", kw["json_body"]["query"])
        rows = {r["n"]: r for r in out["mine"]}
        self.assertEqual(sorted(rows), [12, 77, 128, 131, 140])
        self.assertEqual(rows[128], {"forge": "gh", "ref": "acme/widgets#128", "n": 128, "title": "Add a retry budget",
                                     "merge": "mergeable", "approvals": 2, "changes": 0, "ci": "success", "draft": False,
                                     "updated": core.iso_epoch("2026-10-21T09:00:00Z"),
                                     "url": "https://github.com/acme/widgets/pull/128"})
        self.assertEqual((rows[131]["merge"], rows[131]["ci"]), ("conflicts", "failure"))
        self.assertEqual((rows[77]["changes"], rows[77]["ci"], rows[77]["ref"]), (1, "pending", "acme/gadgets#77"))
        self.assertEqual((rows[140]["draft"], rows[140]["ci"]), (True, "failure"))
        self.assertEqual((rows[12]["merge"], rows[12]["ci"], rows[12]["approvals"]), ("unknown", None, 1))
        self.assertEqual((out["mine_total"], out["review_total"]), (7, 1))
        self.assertEqual(out["review"][0]["ci"], "pending")

    def test_errors(self):
        for r, want in ((resp(401, {"message": "Bad credentials"}), "unauthorized"),
                        (resp(403, {"message": "API rate limit exceeded for user"}), "API rate limit exceeded for user"),
                        (resp(200, {"errors": [{"message": "Something went wrong"}]}), "Something went wrong"),
                        (resp(502, "<html>bad gateway</html>"), "HTTP 502")):
            with self.assertRaises(dev.ForgeError) as cm:
                dev.github_prs(FakeHTTP({"graphql": r}), "t", 5)
            self.assertEqual(str(cm.exception), want)

    def test_enterprise_url(self):
        fake = FakeHTTP({"ghe.example.com/api/graphql": resp(200, GH_BODY)})
        dev.github_prs(fake, "t", 5, "https://ghe.example.com/api/")
        self.assertEqual(fake.calls[0][1], "https://ghe.example.com/api/graphql")

    def test_token_sources(self):
        self.assertEqual(dev.github_token({"GH_TOKEN": "a", "GITHUB_TOKEN": "b"}), "a")
        self.assertEqual(dev.github_token({"GITHUB_TOKEN": "b"}), "b")
        with faked([(("auth", "token"), (0, "gho_fromgh\n", ""))]) as fake:
            self.assertEqual(dev.github_token({}), "gho_fromgh")
            # named even for github.com: bare `gh auth token` answers for GH_HOST, maybe another instance
            self.assertEqual(fake.calls[-1]["argv"][1:], ["auth", "token", "--hostname", "github.com"])
            self.assertEqual(dev.github_token({"GH_ENTERPRISE_TOKEN": "e"}, "ghe.example.com"), "e")
            dev.github_token({}, "ghe.example.com")
        self.assertEqual(fake.calls[-1]["argv"][1:], ["auth", "token", "--hostname", "ghe.example.com"])
        with faked([(("auth", "token"), (1, "", "no oauth token found"))]):
            self.assertIsNone(dev.github_token({}))
        with faked([], lambda name, extra=(): None):
            self.assertIsNone(dev.github_token({}))


class GitlabTest(unittest.TestCase):
    def test_normalize(self):
        fake = FakeHTTP(gl_routes())
        out = dev.gitlab_prs(fake, "glpat-example", "https://gitlab.example.com/", 20, detail=10)
        urls = [c[1] for c in fake.calls]
        self.assertEqual(urls[0], "https://gitlab.example.com/api/v4/user")
        self.assertTrue(all(c[0] == "GET" for c in fake.calls))
        self.assertTrue(all(c[2] == {"Authorization": "Bearer glpat-example"} for c in fake.calls))
        self.assertTrue(all(c[3]["redirects"] is False for c in fake.calls))
        self.assertTrue(any("scope=created_by_me&state=opened" in u for u in urls))
        self.assertTrue(any("scope=all&reviewer_username=dev&state=opened" in u for u in urls))
        mine = {r["n"]: r for r in out["mine"]}
        self.assertEqual(mine[34], {"forge": "gl", "ref": "acme/infra!34", "n": 34, "title": "Pin provider versions",
                                    "merge": "mergeable", "approvals": 2, "changes": 0, "ci": "success", "draft": False,
                                    "updated": core.iso_epoch("2026-10-21T10:00:00Z"),
                                    "url": "https://gitlab.example.com/x/-/merge_requests/34"})
        self.assertEqual((mine[35]["merge"], mine[35]["ci"], mine[35]["approvals"]), ("conflicts", "failure", 0))
        self.assertEqual((mine[36]["changes"], mine[36]["approvals"], mine[36]["ci"]), (1, 0, None))  # failed GET: unknown
        rev = out["review"][0]
        self.assertEqual((rev["ref"], rev["ci"], rev["approvals"]), ("acme/platform/api!212", "pending", 0))
        self.assertEqual((out["mine_total"], out["review_total"]), (3, 1))
        # approvals and pipelines only for your own MRs, never for review rows
        self.assertFalse(any("/merge_requests/212/" in u for u in urls))

    def test_detail_is_bounded(self):
        fake = FakeHTTP(gl_routes())
        dev.gitlab_prs(fake, "t", "https://gitlab.example.com", 20, detail=1)
        extra = [c[1] for c in fake.calls if "/projects/" in c[1]]
        self.assertEqual(sorted(u.rsplit("/", 1)[-1].split("?")[0] for u in extra), ["approvals", "pipelines"])
        self.assertTrue(all("/merge_requests/34/" in u for u in extra))

    def test_merge_status_variants(self):
        old = dict(gl_mr(1, "g/p!1"), detailed_merge_status=None)
        self.assertEqual(dev.gl_row(dict(old, merge_status="can_be_merged"))["merge"], "mergeable")
        self.assertEqual(dev.gl_row(dict(old, merge_status="cannot_be_merged"))["merge"], "conflicts")
        self.assertEqual(dev.gl_row(dict(old, merge_status="unchecked"))["merge"], "unknown")
        self.assertEqual(dev.gl_row(gl_mr(1, "g/p!1", dms="checking"))["merge"], "unknown")
        self.assertEqual(dev.gl_row(gl_mr(1, "g/p!1", dms="not_approved"))["merge"], "mergeable")
        self.assertEqual(dev.gl_row(gl_mr(1, "g/p!1", dms="ci_must_pass"))["ci"], "failure")
        wip = dict(gl_mr(1, "g/p!1"), draft=None, work_in_progress=True, references=None)
        self.assertEqual((dev.gl_row(wip)["draft"], dev.gl_row(wip)["ref"]), (True, "!1"))

    def test_unauthorized(self):
        with self.assertRaises(dev.ForgeError):
            dev.gitlab_prs(FakeHTTP({"/api/v4/user": resp(401, {"message": "401 Unauthorized"})}), "t",
                           "https://gitlab.example.com", 5)

    def test_token_goes_to_its_own_instance(self):
        # A token for a self-managed instance (glab's GITLAB_HOST) must never be sent to gitlab.com.
        self.assertEqual(dev.gitlab_base({}, {}), "https://gitlab.com")
        self.assertEqual(dev.gitlab_base({}, {"GITLAB_HOST": "git.example.com"}), "https://git.example.com")
        self.assertEqual(dev.gitlab_base({}, {"GL_HOST": "https://gl.example.com/"}), "https://gl.example.com")
        self.assertEqual(dev.gitlab_base({}, {"GITLAB_URI": "http://10.0.0.5:8080"}), "http://10.0.0.5:8080")
        self.assertEqual(dev.gitlab_base({"gitlab_url": "https://mine.example.com"}, {"GITLAB_HOST": "other"}),
                         "https://mine.example.com")
        fake = FakeHTTP(gl_routes())
        with faked([], lambda name, extra=(): None):
            run_src("prs", env={"GITLAB_TOKEN": "glpat-x", "GITLAB_HOST": "git.example.com"}, http=fake)
        self.assertTrue(fake.calls)
        self.assertTrue(all(c[1].startswith("https://git.example.com/api/v4/") for c in fake.calls))


class PrAlertsTest(unittest.TestCase):
    def test_alerts(self):
        mine = [{"ref": "acme/w#1", "merge": "conflicts"}, {"ref": "acme/w#2", "merge": "conflicts", "ci": "failure"},
                {"ref": "acme/w#3", "changes": 1}, {"ref": "g/p!4", "ci": "failure"},
                {"ref": "acme/w#5", "merge": "conflicts", "changes": 2, "ci": "failure", "draft": True},
                {"ref": "acme/w#6", "ci": "pending"}]
        self.assertEqual(dev.pr_alerts(mine), [
            {"sev": "warn", "msg": "2 of your PRs have merge conflicts (acme/w#1, acme/w#2)"},
            {"sev": "warn", "msg": "changes requested on acme/w#3"},
            {"sev": "warn", "msg": "CI failing on acme/w#2, g/p!4"}])
        self.assertEqual(dev.pr_alerts([{"ref": "a/b#1", "merge": "conflicts"}])[0]["msg"],
                         "1 of your PRs has merge conflicts (a/b#1)")
        self.assertEqual(dev.pr_alerts([]), [])


class PrsSourceTest(unittest.TestCase):
    def routes(self):
        r = gl_routes()
        r["api.github.com/graphql"] = resp(200, GH_BODY)
        return r

    def test_both_forges(self):
        fake = FakeHTTP(self.routes())
        with faked([]):
            d = run_src("prs", '[sources.prs]\ngitlab_url = "https://gitlab.example.com"\n',
                        env={"GH_TOKEN": "gho_x", "GITLAB_TOKEN": "glpat-x"}, http=fake)
        self.assertEqual(d["forges"], ["gh", "gl"])
        self.assertEqual((d["open"], d["review_open"]), (7 + 3, 1 + 1))
        ups = [m["updated"] for m in d["mine"]]
        self.assertEqual(ups, sorted(ups, reverse=True))  # newest first, across forges
        self.assertEqual({m["forge"] for m in d["mine"]}, {"gh", "gl"})
        msgs = [a["msg"] for a in d["alerts"]]
        self.assertIn("2 of your PRs have merge conflicts (acme/infra!35, acme/widgets#131)", msgs)  # not draft #140
        self.assertIn("changes requested on acme/gadgets#77, acme/infra!36", msgs)
        self.assertIn("CI failing on acme/infra!35, acme/widgets#131", msgs)
        self.assertNotIn("errors", d)
        self.assertFalse(collect._soft_failure(d))
        self.assertNotIn("gho_x", json.dumps(d))

    def test_gitlab_auto_and_explicit(self):
        fake = FakeHTTP(self.routes())
        with faked([], lambda name, extra=(): None):  # no gh CLI
            d = run_src("prs", env={"GH_TOKEN": "x"}, http=fake)
            self.assertEqual(d["forges"], ["gh"])  # gitlab = auto and no GITLAB_TOKEN: off, quietly
            self.assertFalse(any("gitlab" in c[1] for c in fake.calls))
            d = run_src("prs", "[sources.prs]\ngitlab = true\n", env={"GH_TOKEN": "x"}, http=fake)
            self.assertEqual(d["errors"], {"gl": "no GITLAB_TOKEN"})
            d = run_src("prs", "[sources.prs]\ngitlab = true\ngithub = false\n", http=fake)
            self.assertTrue(collect._soft_failure(d))

    def test_no_credentials_anywhere_is_quiet(self):
        with faked([], lambda name, extra=(): None):
            d = run_src("prs", http=FakeHTTP({}))
        self.assertEqual((d["forges"], d["mine"], d["open"]), ([], [], 0))
        self.assertFalse(collect._soft_failure(d))

    def test_one_forge_down_keeps_its_last_rows(self):
        prev = {"mine": [{"forge": "gl", "ref": "acme/infra!9", "n": 9, "title": "old", "updated": 5},
                         {"forge": "gh", "ref": "acme/widgets#1", "n": 1, "title": "stale gh", "updated": 4}],
                "review": [], "counts": {"gl": {"mine": 1, "review": 0}}}
        r = self.routes()
        r["/api/v4/user"] = urllib.error.URLError(OSError(65, "No route to host"))
        with faked([]):
            d = run_src("prs", '[sources.prs]\ngitlab_url = "https://gitlab.example.com"\n',
                        env={"GH_TOKEN": "x", "GITLAB_TOKEN": "y"}, prev=prev, http=FakeHTTP(r))
        self.assertEqual(d["errors"], {"gl": "no route"})
        refs = [m["ref"] for m in d["mine"]]
        self.assertIn("acme/infra!9", refs)  # GitLab's last list survives
        self.assertNotIn("acme/widgets#1", refs)  # GitHub answered: its stale row is replaced
        self.assertEqual(d["counts"]["gl"], {"mine": 1, "review": 0})
        self.assertFalse(collect._soft_failure(d))

    def test_everything_down_is_a_soft_failure(self):
        r = {"graphql": urllib.error.URLError(socket.timeout("timed out"))}
        with faked([]):
            d = run_src("prs", env={"GH_TOKEN": "x"}, http=FakeHTTP(r))
        self.assertEqual(d, {"error": "gh: timeout", "status": "unknown"})
        self.assertTrue(collect._soft_failure(d))
        r = {"graphql": resp(401, {"message": "Bad credentials"})}
        with faked([]):
            d = run_src("prs", env={"GH_TOKEN": "x"}, http=FakeHTTP(r))
        self.assertEqual(d["error"], "gh: unauthorized")

    def test_repos_allowlist_and_limit(self):
        fake = FakeHTTP(self.routes())
        toml = '[sources.prs]\nrepos = ["acme/widgets", "acme/infra*"]\nlimit = 3\n'
        with faked([]):
            d = run_src("prs", toml, env={"GH_TOKEN": "x", "GITLAB_TOKEN": "y"}, http=fake)
        self.assertEqual(fake.calls[0][3]["json_body"]["variables"]["n"], 3)
        self.assertTrue(all(m["ref"].startswith(("acme/widgets#", "acme/infra!")) for m in d["mine"]))
        self.assertEqual(d["review"], [m for m in d["review"] if m["ref"].startswith("acme/widgets#")])
        self.assertLessEqual(len(d["mine"]), 3)


# ── Reachability ────────────────────────────────────────────────────────────
SSH_G = {
    "devbox": "user dev\nhostname 10.0.1.23\nport 2222\nproxyjump bastion\nserveraliveinterval 30\n",
    "bastion": "user ops\nhostname bastion.example.com\nport 22\n",
    "viassm": 'hostname i-0abc123\nport 22\nproxycommand sh -c "aws ssm start-session --target %h"\n',
    "plain": "hostname 10.0.3.12\nport 22\nproxyjump none\nproxycommand none\n",
    "chain": "hostname 10.0.9.9\nport 22\nproxyjump ops@jump.example.com:2200,second.example.com\n",
}


def ssh_answer(argv):
    alias = argv[-1]
    return (0, SSH_G[alias], "") if alias in SSH_G else (0, "hostname %s\nport 22\n" % alias, "")


class FakeSock:
    def close(self):
        pass


class SshTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(dev.parse_ssh_g(SSH_G["devbox"]), {"hostname": "10.0.1.23", "port": "2222", "proxyjump": "bastion"})
        self.assertEqual(dev.parse_ssh_g(SSH_G["plain"]), {"hostname": "10.0.3.12", "port": "22"})
        self.assertEqual(dev.parse_ssh_g("HostName X\nhostname y\n"), {"hostname": "X"})  # first value wins
        self.assertEqual(dev.parse_ssh_g(""), {})

    def test_hop(self):
        self.assertEqual(dev._hop("bastion"), ("bastion", ""))
        self.assertEqual(dev._hop("ops@jump.example.com:2200,second.example.com"), ("jump.example.com", "2200"))
        self.assertEqual(dev._hop("ssh://ops@[2001:db8::1]:2022"), ("2001:db8::1", "2022"))
        self.assertEqual(dev._hop("2001:db8::7"), ("2001:db8::7", ""))

    def probe(self, alias, connect):
        seen = []

        def conn(addr, timeout=None):
            seen.append(addr)
            return connect(addr)
        with faked([(("-G",), ssh_answer)]) as fake, mock.patch.object(socket, "create_connection", conn):
            row = dev.ssh_route(alias)
        return row, seen, fake

    def test_direct_route(self):
        row, seen, fake = self.probe("plain", lambda a: FakeSock())
        self.assertEqual(seen, [("10.0.3.12", 22)])
        self.assertEqual((row["ok"], row["host"], row["port"]), (True, "10.0.3.12", 22))
        self.assertIn("ms", row)
        self.assertEqual(fake.calls[0]["argv"][1:], ["-G", "plain"])  # resolution only: never a login

    def test_proxyjump_probes_the_first_hop(self):
        row, seen, _ = self.probe("devbox", lambda a: FakeSock())
        self.assertEqual(seen, [("bastion.example.com", 22)])
        self.assertEqual((row["ok"], row["via"]), (True, "bastion"))
        row, seen, _ = self.probe("chain", lambda a: FakeSock())
        self.assertEqual(seen, [("jump.example.com", 2200)])

    def test_proxycommand_is_unknown(self):
        row, seen, _ = self.probe("viassm", lambda a: FakeSock())
        self.assertEqual((row["ok"], row["error"], seen), (None, "proxied", []))

    def test_failures(self):
        def boom(exc):
            def f(a):
                raise exc
            return f
        for exc, why in ((socket.timeout("timed out"), "timeout"), (ConnectionRefusedError(61, "refused"), "refused"),
                         (socket.gaierror(8, "nodename nor servname provided"), "no dns"),
                         (OSError(65, "No route to host"), "no route")):
            row, _, _ = self.probe("plain", boom(exc))
            self.assertEqual((row["ok"], row["error"]), (False, why))
        with faked([(("-G",), (255, "", "bad config"))]):
            self.assertEqual(dev.ssh_route("broken"), {"alias": "broken", "ok": False, "error": "no ssh config"})


class EndpointTest(unittest.TestCase):
    def test_direct_redirect_is_up(self):
        fake = FakeHTTP({"git.example.com": resp(302, b"", ms=40)})
        with mock.patch.object(dev, "_resolves", lambda h: True):
            row = dev.endpoint({"label": "git", "url": "https://git.example.com/", "verify": False}, None, fake)
        self.assertEqual((row["ok"], row["status"], row["via"], row["ms"]), (True, 302, "direct", 40))
        kw = fake.calls[0][3]
        self.assertEqual((kw["redirects"], kw["verify"]), (False, False))

    def test_socks_fallback(self):
        fake = FakeHTTP({"wiki.internal.example.com": urllib.error.URLError(OSError(65, "No route to host"))})
        curl = [(("-sS",), (0, "200 0.123", ""))]
        with mock.patch.object(dev, "_resolves", lambda h: True), faked(curl) as run:
            row = dev.endpoint({"label": "wiki", "url": "https://wiki.internal.example.com/"}, "1080", fake)
        self.assertEqual((row["ok"], row["status"], row["via"], row["ms"]), (True, 200, "socks", 123))
        argv = run.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--socks5-hostname") + 1], "127.0.0.1:1080")
        self.assertNotIn("-k", argv)
        self.assertNotIn("-L", argv)  # a redirect is an answer: never followed

    def test_no_dns_goes_straight_to_socks(self):
        fake = FakeHTTP({})
        with mock.patch.object(dev, "_resolves", lambda h: False), faked([(("-sS",), (7, "000 0.0", ""))]):
            row = dev.endpoint({"url": "https://vpc-only.example.com/"}, "127.0.0.2:9050", fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual((row["ok"], row["dns"], row["error"], row["via"]), (False, False, "no dns", None))
        with mock.patch.object(dev, "_resolves", lambda h: False):
            row = dev.endpoint({"url": "https://vpc-only.example.com/"}, None, fake)
        self.assertEqual((row["label"], row["error"]), ("https://vpc-only.example.com/", "no dns"))

    def test_socks_from_procs(self):
        put("procs", {"tunnels": [{"kind": "L", "ports": ["5432"], "host": "db", "pid": os.getpid()},
                                  {"kind": "D", "ports": ["1080"], "host": "bastion", "pid": os.getpid()}]})
        self.assertEqual(dev.socks_from_procs(), "1080")
        put("procs", {"tunnels": [{"kind": "D", "ports": ["1080"], "host": "bastion", "pid": 2 ** 22 + 7}]})
        self.assertIsNone(dev.socks_from_procs())  # a dead proxy does not count
        put("procs", {"tunnels": []})


class ReachSourceTest(unittest.TestCase):
    TOML = ('[sources.reach]\nssh = ["plain", "viassm"]\nsocks_port = 1080\n'
            'endpoints = [{label = "ci", url = "https://ci.example.com/health"},\n'
            '             {label = "git", url = "https://git.example.com/", verify = false},\n'
            '             "ftp://ignored.example.com/"]\n')

    def run_reach(self, ci_status, prev=None, toml=None):
        fake = FakeHTTP({"ci.example.com": resp(ci_status, b""), "git.example.com": resp(200, b"")})
        with faked([(("-G",), ssh_answer)]), mock.patch.object(socket, "create_connection", lambda a, timeout=None: FakeSock()), \
                mock.patch.object(dev, "_resolves", lambda h: True):
            return run_src("reach", toml or self.TOML, prev=prev, http=fake)

    def test_shape(self):
        d = self.run_reach(200)
        self.assertEqual([v["alias"] for v in d["ssh"]], ["plain", "viassm"])
        self.assertEqual([(v["ok"]) for v in d["ssh"]], [True, None])
        self.assertEqual([e["label"] for e in d["endpoints"]], ["ci", "git"])  # ftp:// is not probed
        self.assertEqual(d["socks"], "1080")
        self.assertNotIn("alerts", d)

    def test_two_strikes(self):
        first = self.run_reach(503)
        self.assertNotIn("alerts", first)  # one 5xx can be a deploy restarting
        second = self.run_reach(503, prev=first)
        self.assertEqual(second["alerts"], [{"sev": "warn", "msg": "ci answers HTTP 503"}])
        self.assertNotIn("alerts", self.run_reach(200, prev=second))
        self.assertNotIn("alerts", self.run_reach(502, prev={"endpoints": [{"label": "ci", "status": 0}]}))

    def test_off_vpn_is_state(self):
        def no_route(addr, timeout=None):
            raise OSError(65, "No route to host")
        fake = FakeHTTP({"example.com": urllib.error.URLError(OSError(65, "No route to host"))})
        toml = self.TOML.replace("socks_port = 1080\n", "")
        with faked([(("-G",), ssh_answer)]), mock.patch.object(socket, "create_connection", no_route), \
                mock.patch.object(dev, "_resolves", lambda h: True), mock.patch.object(dev, "socks_from_procs", lambda: None):
            d = run_src("reach", toml, http=fake)
        self.assertNotIn("alerts", d)
        self.assertEqual([v.get("error") for v in d["ssh"]], ["no route", "proxied"])
        self.assertEqual([e["error"] for e in d["endpoints"]], ["no route", "no route"])
        self.assertIsNone(d["socks"])

    def test_optin(self):
        self.assertEqual(registry.state(registry.SOURCES["reach"], support.host("")), "opt-in")
        self.assertEqual(registry.state(registry.SOURCES["reach"], support.host(self.TOML)), "on")


# ── Panels ──────────────────────────────────────────────────────────────────
def demo_data():
    """The demo records as `porchlight demo` loads them: "_" keys dropped, and the
    "+N" / "-N" strings (seconds from the moment of loading) turned into times."""
    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str) and len(v) > 1 and v[0] in "+-" and v[1:].isdigit():
            return NOW + int(v)
        return v
    with open(DEMO, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: walk(v) for k, v in raw.items() if not k.startswith("_")}


# Edge states per panel: each must render (or return None) without overflowing.
LONG = "x" * 140
VARIANTS = {
    "docker": [
        {"engine": "down", "binary": "docker"},
        {"engine": "denied", "binary": "docker"},
        {"engine": "denied", "binary": "podman"},
        {"engine": "missing", "binary": "docker", "expected": {"want": 2, "up": 0, "missing": ["web", "db-*"]},
         "badge": "0/2"},
        {"engine": "up", "binary": "docker", "projects": [], "other": {"running": 0, "total": 0}},
        {"engine": "up", "binary": "docker", "recent_s": 21600, "badge": "1/9",
         "projects": [{"name": "p%d-%s" % (i, LONG), "running": 1, "total": 9, "done": 2, "starting": 3, "stopped": 1,
                       "unhealthy": ["svc-%s" % LONG], "restarting": ["r1", "r2"],
                       "failed": [{"name": "f", "code": None, "ago": None}, {"name": "g", "code": 1, "ago": 0}]}
                      for i in range(7)] + [{"name": "s%d" % i, "running": 0, "total": 1, "done": 0, "starting": 0,
                                              "stopped": 1, "unhealthy": [], "restarting": [], "failed": []}
                                             for i in range(9)],
         "other": {"running": 1, "total": 4, "restarting": ["loop-" + LONG], "unhealthy": ["sick"]},
         "expected": {"want": 12, "up": 3, "missing": ["c%d-%s" % (i, LONG) for i in range(9)]}},
        {"engine": "up", "binary": "docker", "projects": [], "other": {"running": 2, "total": 2},
         "expected": {"want": 2, "up": 2, "missing": []}, "badge": "2/2"},
    ],
    "prs": [
        {"error": "gh: no route", "status": "unknown"},
        {"error": "gh: unauthorized", "status": "unknown"},
        {"error": LONG, "status": "unknown"},
        {"mine": [], "review": [], "forges": ["gh"], "open": 0, "review_open": 0},
        {"mine": [{"forge": "gl", "ref": "group/sub/%s!%d" % (LONG, i), "n": i, "title": LONG, "merge": "unknown",
                   "approvals": i, "changes": 0, "ci": ["success", "failure", "pending", None][i % 4], "draft": i == 3,
                   "updated": NOW - i * 7200} for i in range(9)],
         "review": [{"forge": "gh", "ref": "o/%s#%d" % (LONG, i), "n": i, "updated": NOW} for i in range(6)],
         "forges": ["gh", "gl"], "open": 40, "review_open": 9, "errors": {"gh": "no route " + LONG, "gl": "timeout"}},
    ],
    "aws_auth": [
        {"cli": True, "static": [], "env": False, "sso": {"profiles": []}, "sts": {"state": "none", "at": NOW}},
        {"cli": True, "static": ["default"], "env": False, "profile": "default", "sso": {"profiles": []},
         "login": "aws configure", "sts": {"state": "rejected", "error": "InvalidClientTokenId", "at": NOW}},
        {"cli": True, "static": [], "env": True, "profile": "dev", "region": "eu-west-1",
         "sso": {"profiles": ["dev"], "account": "123456789012", "expires": NOW - 7200, "refreshable": False},
         "login": "aws sso login --profile dev", "sts": {"state": "unreachable", "error": "rc 124", "at": NOW}},
        {"cli": True, "static": [], "env": False, "profile": "dev",
         "sso": {"profiles": ["dev"], "account": None, "expires": NOW + 1200, "refreshable": False},
         "sts": {"state": "error", "error": LONG, "at": NOW}},
        {"cli": True, "static": [], "env": False, "sso": {"profiles": ["dev"], "expires": None}, "sts": {}},
        {"cli": True, "static": [], "env": False, "profile": "dev",
         "sso": {"profiles": ["dev"], "expires": NOW - 60, "refreshable": True},
         "sts": {"state": "ok", "principal": "role/" + LONG, "account": "123456789012", "at": NOW}},
    ],
    "reach": [
        {"socks": None, "ssh": [{"alias": "bastion", "ok": False, "error": "no route"},
                                {"alias": "devbox", "ok": False, "error": "timeout"}],
         "endpoints": [{"label": "git", "status": 0, "ok": False, "dns": False, "error": "no dns"}]},
        {"socks": None, "ssh": [{"alias": "a-" + LONG, "ok": False, "error": "no ssh config"}],
         "endpoints": [{"label": "e%d" % i, "status": [0, 200, 503, 404][i % 4], "ok": i % 4 in (1, 3),
                        "via": "socks" if i == 1 else "direct", "error": "timeout"} for i in range(9)]},
        {"socks": "1080", "ssh": [], "endpoints": []},
    ],
    "instances": [
        {"error": "describe-instances: ExpiredToken", "status": "unknown"},
        {"src": None, "rows": [], "total": 0, "running": 0, "state": "none"},
        {"src": "cache", "asof": NOW - 7200, "aws_error": "ExpiredToken " + LONG, "total": 12, "running": 10,
         "rows": [{"name": "box-%d-%s" % (i, LONG), "type": "m7i.24xlarge", "ip": "10.0.%d.%d" % (i, i),
                   "id": "i-0abc%013d" % i, "launched": NOW - i * 86400, "state": "running"} for i in range(10)] +
                 [{"name": "off-%d" % i, "type": "t3.micro", "ip": "", "id": "i-0def%013d" % i, "launched": 0,
                   "state": ["stopped", "pending"][i], "start": "aws ec2 start-instances --instance-ids i-0def" + LONG}
                  for i in range(2)]},
    ],
}
PANEL_OF = {"docker": "docker", "prs": "prs", "aws_auth": "access", "reach": "access", "instances": "instances"}


class PanelTest(unittest.TestCase):
    WIDTHS = (60, 90, 130)

    def setUp(self):
        support.clear_cache()

    def tearDown(self):
        T.set_mode("truecolor")
        T.set_ascii(False)
        support.clear_cache()

    def build(self, key, cw, toml=""):
        h = support.host(toml)
        x = RCtx(h, registry.Cache(), {"vitals": {}}, [], cw)
        return BUILDERS[key](x)

    def check(self, key, cw, toml="", ctx=""):
        p = self.build(key, cw, toml)
        if p is None:
            return None
        for line in p.lines:  # what the builder made already fits: nothing relies on the frame's truncation
            self.assertLessEqual(T.width(line), cw - 1, "%s cw=%d %s: %r" % (key, cw, ctx, T.strip(line)))
        for line in p.render(cw, T.ACCENT):
            self.assertLessEqual(T.width(line), cw, "%s cw=%d %s: %r" % (key, cw, ctx, T.strip(line)))
        self.assertIn(p.level, ("ok", "warn", "crit", "unknown"))
        return p

    def test_demo_at_every_width_and_mode(self):
        for mode, ascii_ in (("truecolor", False), ("256", False), ("none", False), ("none", True)):
            T.set_mode(mode)
            T.set_ascii(ascii_)
            support.clear_cache()
            for name, data in demo_data().items():
                put(name, data)
            for cw in self.WIDTHS:
                for key in ("docker", "prs", "access", "instances"):
                    p = self.check(key, cw, ctx=mode)
                    self.assertIsNotNone(p, key)
                    if ascii_:
                        self.assertTrue(all(ord(ch) < 128 for line in p.render(cw, T.ACCENT) for ch in T.strip(line)),
                                        "%s has non-ASCII glyphs in ASCII mode" % key)

    def test_demo_content(self):
        for name, data in demo_data().items():
            put(name, data)
        text = {k: "\n".join(T.strip(line) for line in self.build(k, 130).lines) for k in ("docker", "prs", "access", "instances")}
        self.assertIn("worker restarting", text["docker"])
        self.assertIn("stopped: blog, sandbox", text["docker"])
        self.assertEqual(self.build("docker", 130).level, "crit")
        self.assertIn("conflicts", text["prs"])
        self.assertIn("changes", text["prs"])
        self.assertIn("3 waiting on you", text["prs"])
        self.assertIn("gh acme/widgets#128", text["prs"])  # forge marks: rows come from two forges
        self.assertIn("role/DeveloperAccess @ 123456789012", text["access"])
        self.assertIn("renews", text["access"])
        self.assertIn("wiki 200 " + T.g("via"), text["access"])
        self.assertIn("stopped: gpu-box, scratch", text["instances"])
        self.assertIn("start: aws ec2 start-instances", text["instances"])
        self.assertEqual(self.build("instances", 130).note, "3/5 running")
        short = "\n".join(T.strip(line) for line in self.build("prs", 60).lines)
        self.assertIn("widgets#128", short)
        self.assertNotIn("acme/widgets#128", short)  # narrow: owners dropped

    def test_variants(self):
        for name, variants in VARIANTS.items():
            for i, data in enumerate(variants):
                support.clear_cache()
                put(name, data)
                for cw in self.WIDTHS:
                    self.check(PANEL_OF[name], cw, ctx="variant %s[%d]" % (name, i))

    def test_none_without_data(self):
        for key in ("docker", "prs", "access", "instances"):
            self.assertIsNone(self.build(key, 90), key)

    def test_quiet_states(self):
        put("docker", {"engine": "missing", "binary": "docker"})
        self.assertIsNone(self.build("docker", 90))  # no engine, nothing expected: no panel
        self.assertIsNotNone(self.build("docker", 90, "[sources.docker]\nbinary = \"docker\"\n"))
        put("prs", {"forges": [], "mine": [], "review": [], "open": 0})
        self.assertIsNone(self.build("prs", 90))
        put("aws_auth", {"cli": True, "static": [], "env": False, "sso": {"profiles": []}, "sts": {"state": "none"}})
        self.assertIsNone(self.build("access", 90))  # an AWS CLI never set up is not news
        put("instances", {"src": None, "rows": [], "total": 0, "running": 0, "state": "none"})
        self.assertIsNone(self.build("instances", 90))
        p = self.build("instances", 90, "[sources.instances]\nfilters = [\"Name=tag:Owner,Values=me\"]\n")
        self.assertIn("no instances match", T.strip(p.lines[0]))

    def test_levels(self):
        put("aws_auth", VARIANTS["aws_auth"][1])
        self.assertEqual(self.build("access", 90).level, "crit")
        support.clear_cache()
        put("reach", {"ssh": [], "endpoints": [{"label": "ci", "status": 503, "ok": False}]})
        self.assertEqual(self.build("access", 90).level, "warn")
        support.clear_cache()
        put("reach", VARIANTS["reach"][0])
        p = self.build("access", 90)
        text = "\n".join(T.strip(line) for line in p.lines)
        self.assertIn("no route to bastion, devbox", text)
        self.assertIn("ssh -f -N -D 1080 bastion", text)
        self.assertEqual(p.level, "ok")  # off VPN is state
        support.clear_cache()
        put("docker", VARIANTS["docker"][3])
        p = self.build("docker", 90)
        self.assertEqual(p.level, "crit")
        self.assertIn("down: web, db-*", "\n".join(T.strip(line) for line in p.lines))

    def text(self, key, cw=130):
        return "\n".join(T.strip(line) for line in self.build(key, cw).lines)

    def test_access_wording(self):
        base = {"cli": True, "static": [], "env": False, "profile": "default", "login": "aws sso login --profile dev",
                "sts": {"state": "none", "at": NOW}}
        # SSO profiles exist but the CLI's active profile has nothing: say which, not "nothing configured"
        put("aws_auth", dict(base, sso={"profiles": ["dev"], "expires": NOW + 7230, "refreshable": True}))
        self.assertIn("profile default has no credentials · profiles: dev", self.text("access"))
        self.assertIn("token 2h left · renews", self.text("access"))
        # an expired access token renews while its session plausibly lives; days later it needs a login
        put("aws_auth", dict(base, sso={"profiles": ["dev"], "expires": NOW - 3 * 3600, "refreshable": True}))
        self.assertIn("token expired 3h ago · renews on next use", self.text("access"))
        put("aws_auth", dict(base, sso={"profiles": ["dev"], "expires": NOW - 3 * 86400, "refreshable": True}))
        self.assertIn("token expired 3d ago · aws sso login --profile dev", self.text("access"))
        put("aws_auth", dict(base, sso={"profiles": ["dev"], "expires": NOW - 600, "refreshable": False}))
        self.assertIn("token expired 10m ago · aws sso login --profile dev", self.text("access"))
        support.clear_cache()
        # the off-VPN hint jumps through an alias that answered, not one that timed out
        put("reach", {"socks": None, "ssh": [{"alias": "home", "ok": False, "error": "timeout"},
                                             {"alias": "bastion", "ok": True, "ms": 9}],
                      "endpoints": [{"label": "wiki", "status": 0, "ok": False, "error": "no route"}]})
        self.assertIn("ssh -f -N -D 1080 bastion", self.text("access"))

    def test_socks_hint_is_detectable(self):
        """The suggested proxy command must be one the procs census recognizes, or
        following the hint would leave reach without its proxy."""
        from porchlight.sources import system
        put("reach", VARIANTS["reach"][0])
        line = next(ln for ln in self.text("access").splitlines() if "1080" in ln)
        tun = system.parse_tunnel(line[line.index("ssh "):].strip())
        self.assertEqual((tun["kind"], tun["ports"], tun["host"]), ("D", ["1080"], "bastion"))

    def test_stale_note(self):
        put("instances", demo_data()["instances"], ok=False, age=5 * 3600, error="describe-instances: ExpiredToken",
            last_ok=NOW - 5 * 3600)
        self.assertIn(T.g("crit"), self.build("instances", 90).note)


class DemoDataTest(unittest.TestCase):
    def test_shapes_match_the_sources(self):
        d = demo_data()
        self.assertEqual(sorted(d), ["aws_auth", "azure_auth", "docker", "gcp_auth", "instances", "prs", "reach"])
        self.assertTrue(all(name in registry.SOURCES for name in d))
        for m in d["prs"]["mine"] + d["prs"]["review"]:
            self.assertEqual(set(m), {"forge", "ref", "n", "title", "merge", "approvals", "changes", "ci", "draft",
                                      "updated", "url"})
        self.assertEqual(dev.pr_alerts(d["prs"]["mine"]), d["prs"]["alerts"])  # alerts as the source would raise them
        self.assertEqual({p["name"] for p in d["docker"]["projects"]}, {"gadgets", "widgets", "blog", "sandbox"})
        for name, data in d.items():
            self.assertFalse(collect._soft_failure(data), name)

    def test_times_are_relative(self):
        """No absolute epoch in the file (it would age), and every relative time sits in a time field."""
        with open(DEMO, encoding="utf-8") as f:
            raw = json.load(f)
        found = []

        def walk(v, key=""):
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(x, k)
            elif isinstance(v, list):
                for x in v:
                    walk(x, key)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                self.assertFalse(1.5e9 < v < 2.5e9, "absolute epoch under %r" % key)
            elif isinstance(v, str) and len(v) > 1 and v[0] in "+-" and v[1:].isdigit():
                found.append(key)
        walk(raw)
        self.assertEqual(set(found), {"expires", "at", "asof", "launched", "updated"})
        self.assertIn("_comment", raw)

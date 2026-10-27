"""The AWS account and its bill: sources/aws.py, aws_prices.py and aws_cost.py, and the
AWS panel (panels/aws.py) with its MONEY and INFRA lines.

Offline: every aws command goes through ctx.run, which a fake CLI answers by command
(the JSON the real CLI prints for the same --query), recording each call with its
profile, region and environment. The AWS config and credentials files are temporary
ones; the real ~/.aws is never read. Every name and figure is fictional.
"""
import support  # noqa: F401 — first: scratch cache and config

import calendar
import json
import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from porchlight import collect, core, registry, render, secrets, theme as T
from porchlight.panels import aws as paws, cloud  # noqa: F401 — registers the panels
from porchlight.sources import aws, aws_cost, aws_prices as P, saas
from porchlight.ui import BUILDERS, RCtx
from support import host, put

ACCT = "210987654321"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "porchlight", "sources")
DEMO = os.path.join(ROOT, "porchlight", "data", "demo", "aws.json")
AWS_CFG = ("[default]\nregion = us-east-1\n\n[sso-session corp]\nsso_start_url = https://example.awsapps.com/start\n\n"
           "[profile admin]\nsso_session = corp\nregion = us-east-1\n\n"
           "[profile readonly]\ncredential_process = /bin/true\nregion = us-east-1\n")
AWS_CREDS = "[ci]\naws_access_key_id = fixture\naws_secret_access_key = fixture\n"
NO_CREDS = 'Unable to locate credentials. You can configure credentials by running "aws configure".\n'
SSO_EXPIRED = "\nError when retrieving token from sso: Token has expired and refresh failed\n"
DENIED = "\nAn error occurred (UnauthorizedOperation) when calling the DescribeInstances operation: not authorized\n"
OD = {"m7i.large": 0.1008, "c7i.xlarge": 0.1785, "t3.large": 0.0912, "r7i.2xlarge": 0.5292}
EBS = {("gp3", "Storage"): ("0.08", "GB-Mo"), ("gp3", "System Operation"): ("0.005", "IOPS-Mo"),
       ("gp3", "Provisioned Throughput"): ("40.96", "GiBps-mo"), ("gp2", "Storage"): ("0.10", "GB-Mo")}
HOST_PRICES = {"mac2-m2": 0.878}


def utc(y, mo, d, h=0, mi=0):
    return float(calendar.timegm((y, mo, d, h, mi, 0)))


NOW = utc(2026, 10, 27, 13)


def product(usd, unit="Hrs"):
    """One Price List product as the API returns it: a JSON string."""
    return json.dumps({"terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"unit": unit, "pricePerUnit": {"USD": str(usd)}}}}}}})


def inst(name, typ, state, iid, ip=None, life=None, why="", ten="default", az="us-east-1a", launched="2026-10-06T08:00:00+00:00"):
    return {"id": iid, "n": name, "t": typ, "s": state, "l": launched, "why": why, "life": life, "os": "Linux/UNIX",
            "az": az, "ten": ten, "ip": ip}


def vol(vid, gb, att, iops=3000, mbps=125, ty="gp3"):
    return {"id": vid, "gb": gb, "ty": ty, "iops": iops, "mbps": mbps, "att": att}


def account():
    """Two running boxes (one spot), two stopped ones, one with an Elastic IP, in one home region."""
    return {"us-east-1": {
        "inst": [inst("api-1", "m7i.large", "running", "i-a1", ip="198.51.100.10"),
                 inst("worker-1", "c7i.xlarge", "running", "i-w1", life="spot", az="us-east-1c"),
                 inst("batch-1", "r7i.2xlarge", "stopped", "i-b1", why="User initiated (2026-10-25 07:07:58 GMT)"),
                 inst("sandbox", "t3.large", "stopped", "i-s1", why="User initiated")],
        "vols": [vol("v-a1", 30, "i-a1"), vol("v-w1", 50, "i-w1", mbps=250), vol("v-b1", 500, "i-b1", iops=6000, mbps=500),
                 vol("v-s1", 100, "i-s1", ty="gp2"), vol("v-loose", 20, None)],
        "eips": [{"ip": "198.51.100.10", "inst": "i-a1"}, {"ip": "198.51.100.20", "inst": "i-s1"},
                 {"ip": "198.51.100.30", "inst": None}]}}


BUDGET = {"n": "monthly-total", "u": "MONTHLY", "lim": "120.0", "cur": "USD", "act": "150.48", "fc": None,
          "up": "2026-10-27T08:46:56.983000-04:00", "f": None, "fx": None}
SCOPED = {"n": "experiments", "u": "MONTHLY", "lim": "500.0", "cur": "USD", "act": "1.10", "fc": None,
          "up": "2026-10-27T08:48:45-04:00", "f": {"TagKeyValue": ["user:team$lab"]}, "fx": None}


class FakeAWS:
    """The aws CLI, by command. regions: {region: {inst, vols, eips, hosts}}. fail(svc, cmd, region)
    returns stderr text to fail with, or None. Every call is recorded."""

    def __init__(self, regions=None, enabled=("us-east-1", "us-east-2"), budgets=(), sts_ok=("readonly",), spot="0.0712",
                 ce=None, fail=None, sts_err=NO_CREDS, delay=0.0):
        self.regions, self.enabled, self.budgets = regions or {}, list(enabled), list(budgets)
        self.sts_ok, self.spot, self.ce, self.fail, self.sts_err, self.delay = sts_ok, spot, ce, fail, sts_err, delay
        self.calls, self.lock, self.active, self.peak = [], threading.Lock(), 0, 0

    def count(self, svc, cmd=None):
        return sum(1 for c in self.calls if c["svc"] == svc and (cmd is None or c["cmd"] == cmd))

    def __call__(self, argv, timeout=None, env=None, input_text=None, cwd=None):
        args = argv[1:]
        svc, cmd = args[0], args[1]
        prof = args[args.index("--profile") + 1] if "--profile" in args else None
        region = args[args.index("--region") + 1] if "--region" in args else None
        with self.lock:
            self.calls.append({"svc": svc, "cmd": cmd, "profile": prof, "region": region, "argv": list(argv), "env": env,
                               "timeout": timeout})
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return self.answer(svc, cmd, prof, region, args)
        finally:
            with self.lock:
                self.active -= 1

    def answer(self, svc, cmd, prof, region, args):
        why = self.fail and self.fail(svc, cmd, region)
        if why:
            return 254, "", why
        if (svc, cmd) == ("sts", "get-caller-identity"):
            if (prof or "default") in self.sts_ok:
                return 0, json.dumps({"Account": ACCT, "UserId": "AROAEXAMPLE:someone@example.com",
                                      "Arn": "arn:aws:sts::%s:assumed-role/%s/someone@example.com" % (ACCT, prof)}), ""
            return 254, "", self.sts_err
        reg = self.regions.get(region) or {}
        if svc == "ec2":
            table = {"describe-regions": self.enabled, "describe-instances": reg.get("inst", []),
                     "describe-volumes": reg.get("vols", []), "describe-addresses": reg.get("eips", []),
                     "describe-hosts": reg.get("hosts", []), "describe-spot-price-history": self.spot}
            return 0, json.dumps(table[cmd]), ""
        if (svc, cmd) == ("budgets", "describe-budgets"):
            assert args[args.index("--account-id") + 1] == ACCT
            return 0, json.dumps(self.budgets), ""
        if (svc, cmd) == ("pricing", "get-products"):
            f = dict(re.match(r"Type=TERM_MATCH,Field=(\w+),Value=(.*)$", a).groups() for a in args if a.startswith("Type="))
            if f.get("productFamily") == "Dedicated Host":
                got = HOST_PRICES.get(f["instanceType"])
                return 0, json.dumps({"FormatVersion": "aws_v1", "PriceList": [product(got)] if got else []}), ""
            if "volumeApiName" in f:
                got = EBS.get((f["volumeApiName"], f["productFamily"]))
                return 0, json.dumps({"PriceList": [product(got[0], got[1])] if got else []}), ""
            got = OD.get(f.get("instanceType"))
            return 0, json.dumps({"PriceList": [product(got)] if got else []}), ""
        if (svc, cmd) == ("ce", "get-cost-and-usage"):
            return 0, json.dumps(self.ce), ""
        raise AssertionError("unexpected aws call: %r" % (args,))


class Base(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        d = tempfile.mkdtemp(prefix="aws-files-")
        self.cfg_path, self.creds_path = os.path.join(d, "config"), os.path.join(d, "credentials")
        with open(self.cfg_path, "w") as f:
            f.write(AWS_CFG)
        with open(self.creds_path, "w") as f:
            f.write(AWS_CREDS)
        self.env = {"AWS_CONFIG_FILE": self.cfg_path, "AWS_SHARED_CREDENTIALS_FILE": self.creds_path}
        p = mock.patch.object(aws, "aws_bin", return_value="/fake/aws")
        p.start()
        self.addCleanup(p.stop)

    def fake(self, **kw):
        self.aws = FakeAWS(**kw)
        return self.aws

    def ctx(self, name, toml="", prev=None, env=None):
        h = host(toml)
        c = support.ctx(h, registry.SOURCES[name].config(h), dict(self.env, **(env or {})), prev)
        c.run = self.aws
        return c

    def run_account(self, toml="", prev=None, env=None, now=NOW):
        with mock.patch.object(saas, "_now", lambda: now):
            return registry.SOURCES["aws_account"].fn(self.ctx("aws_account", toml, prev, env))


TOML = '[sources.aws_account]\nprofile = "readonly"\nhome_regions = ["us-east-1"]\n'


class Inventory(Base):
    def test_one_run_prices_the_burn_the_idle_spend_and_the_budget(self):
        f = self.fake(regions=account(), budgets=[BUDGET, SCOPED])
        r = self.run_account(TOML)
        d = r["details"]
        by = {i["name"]: i for i in d["instances"]}
        self.assertEqual([i["name"] for i in d["instances"]], ["api-1", "worker-1", "batch-1", "sandbox"])  # running first, dearest first
        self.assertEqual((by["api-1"]["usd_h"], by["api-1"]["price"], by["api-1"]["disk_usd_mo"]), (0.1008, "od", 2.4))
        self.assertEqual((by["worker-1"]["usd_h"], by["worker-1"]["price"], by["worker-1"]["disk_usd_mo"]), (0.0712, "spot", 9.0))
        self.assertEqual(by["batch-1"]["disk_usd_mo"], 70.0)            # 500 GiB, 6000 IOPS, 500 MiB/s of gp3
        self.assertEqual(by["batch-1"]["since"], int(utc(2026, 10, 25, 7, 7) + 58))
        self.assertEqual(by["sandbox"]["since"], 0)                     # "User initiated" without a date
        b = d["burn"]
        self.assertEqual((b["compute_h"], b["ipv4"]), (round(0.1008 + 0.0712, 4), 3))  # api-1's address is counted once
        self.assertAlmostEqual(b["disk_h"], (2.4 + 9.0 + 70.0 + 10.0 + 1.6) / 730.0, places=4)
        self.assertEqual(d["idle"]["items"], [{"name": "batch-1", "usd_mo": 70.0}, {"name": "sandbox", "usd_mo": 13.65},
                                              {"name": "unassociated IPs", "usd_mo": 3.65},
                                              {"name": "unattached volumes", "usd_mo": 1.6}])
        self.assertEqual(r["alerts"], [{"sev": "warn", "msg": "aws: $150 spent this month, 125% of the $120 budget"}])
        self.assertEqual((r["status"], r["summary"]), ("warn", "2/4 EC2 running · $0.31/h"))
        self.assertEqual((d["account_last4"], d["profile"], d["principal_type"]), ("4321", "readonly", "assumed-role"))
        blob = json.dumps(r)
        for kept_out in (ACCT, "someone@example.com", "198.51.100.", "AROAEXAMPLE"):
            self.assertNotIn(kept_out, blob)  # the account id, the session name, public addresses
        self.assertEqual(f.count("sts"), 1)
        self.assertEqual({c["region"] for c in f.calls if c["svc"] == "pricing"}, {"us-east-1"})

    def test_the_next_run_reuses_prices_and_regions_and_sweeps_on(self):
        regions = account()
        regions["us-west-1"] = {"inst": [inst("stray", "m7i.large", "running", "i-x", az="us-west-1a")]}
        f = self.fake(regions=regions, enabled=("eu-west-1", "us-east-1", "us-east-2", "us-west-1", "us-west-2"))
        r1 = self.run_account(TOML)
        d1 = r1["details"]
        self.assertEqual(sorted(d1["regions"]["swept"]), ["eu-west-1", "us-east-1", "us-east-2"])  # home + two swept
        self.assertEqual(d1["regions"]["active"], [])
        self.assertEqual(f.count("ec2", "describe-regions"), 1)
        f.calls[:] = []
        r2 = self.run_account(TOML, prev=r1)
        d2 = r2["details"]
        self.assertEqual((f.count("pricing"), f.count("ec2", "describe-regions")), (1, 0))  # only the stray's type is new
        self.assertIn({"sev": "warn", "msg": "aws: 1 instance in us-west-1, outside the home regions"}, r2["alerts"])
        self.assertEqual(d2["regions"]["active"], ["us-west-1"])
        f.calls[:] = []
        self.run_account(TOML, prev=r2)
        self.assertEqual({c["region"] for c in f.calls if c["cmd"] == "describe-addresses"}, {"us-east-1", "us-west-1"})
        self.assertEqual({c["region"] for c in f.calls if c["cmd"] == "describe-instances"},
                         {"us-east-1", "us-west-1", "eu-west-1", "us-east-2"})  # the sweep wrapped round
        self.assertEqual(f.count("pricing"), 0)  # every price is cached for a week
        self.assertEqual(aws.plan(["us-east-1"], ["a-b-1", "us-east-1"], {}, 0), (["us-east-1"], [], 0))  # sweep = 0: home only

    def test_a_home_region_that_fails_twice_alerts_and_an_active_one_stays_active(self):
        f = self.fake(regions=account(), enabled=("us-east-1", "us-west-1"),
                      fail=lambda s, c, r: DENIED if (r, c) == ("us-west-1", "describe-instances") else None)
        prev = {"details": {"regions": {"active": ["us-west-1"], "enabled": ["us-east-1", "us-west-1"], "at": NOW}}}
        r1 = self.run_account(TOML, prev=prev)
        self.assertEqual(r1["details"]["regions"]["errors"], {"us-west-1": "inst: access-denied"})
        self.assertEqual(r1["details"]["regions"]["active"], ["us-west-1"])
        self.assertTrue(r1["summary"].endswith("(partial)"))
        self.assertFalse(any("could not read" in a["msg"] for a in r1.get("alerts") or []))  # one miss is a blip
        r2 = self.run_account(TOML, prev=r1)
        self.assertIn({"sev": "warn", "msg": "aws: could not read us-west-1 (partial data)"}, r2["alerts"])
        self.assertEqual(f.count("ec2", "describe-regions"), 0)

    def test_a_budget_that_only_leaves_out_credits_covers_the_account(self):
        console = dict(BUDGET, n="console-default", lim="200.0", f=None,
                       fx={"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}})
        by_service = dict(BUDGET, n="ec2-only", lim="900.0", f=None,
                          fx={"And": [{"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Elastic Compute Cloud - Compute"]}},
                                      {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit"]}}}]})
        old_style = dict(BUDGET, n="old-style", lim="950.0", f={"RecordType": ["Usage"]}, fx=None)
        self.fake(regions=account(), budgets=[console, by_service, SCOPED, old_style])
        rows = self.run_account(TOML)["details"]["budgets"]
        self.assertEqual([b["scoped"] for b in rows], [False, True, True, False])
        self.assertEqual(aws.account_budget(rows)["name"], "old-style")  # the largest that covers everything
        self.assertTrue(aws.narrows([{"Tags": {"Key": "team", "Values": ["lab"]}}]))
        self.assertFalse(aws.narrows(None))

    def test_a_dedicated_host_without_an_instance_bills_and_alerts(self):
        regions = {"us-east-1": {"hosts": [{"id": "h-1", "fam": "mac2-m2", "ty": "mac2-m2.metal", "st": "available",
                                             "at": "2026-10-25T04:00:00+00:00", "n": 0}]}}
        self.fake(regions=regions)
        r = self.run_account(TOML)
        d = r["details"]
        self.assertEqual(d["burn"]["host_h"], 0.878)
        self.assertEqual(d["idle"]["items"], [{"name": "host mac2-m2", "usd_mo": round(0.878 * 730, 2)}])
        self.assertEqual(d["hosts"][0], {"family": "mac2-m2", "type": "mac2-m2.metal", "region": "us-east-1",
                                         "state": "available", "since": int(utc(2026, 10, 25, 4)), "instances": 0, "usd_h": 0.878})
        self.assertIn({"sev": "warn", "msg": "aws: dedicated host mac2-m2 in us-east-1 has no instance ($0.88/h)"}, r["alerts"])

    def test_an_instance_on_a_host_costs_nothing_itself(self):
        regions = {"us-east-1": {"inst": [inst("mac-ci", "mac2-m2.metal", "running", "i-m", ten="host")],
                                 "hosts": [{"id": "h-1", "fam": "mac2-m2", "ty": "mac2-m2.metal", "st": "available",
                                            "at": "2026-10-25T04:00:00+00:00", "n": 1}]}}
        self.fake(regions=regions)
        r = self.run_account(TOML)
        i = r["details"]["instances"][0]
        self.assertEqual((i["usd_h"], i["price"]), (0.0, "host"))
        self.assertEqual(r["details"]["burn"]["total_h"], 0.878)
        self.assertNotIn("alerts", r)

    def test_an_unpriced_resource_is_counted_never_guessed(self):
        regions = {"us-east-1": {"inst": [inst("odd", "x9.huge", "running", "i-o")], "vols": [vol("v-st1", 500, "i-o", ty="st1")]}}
        self.fake(regions=regions)
        b = self.run_account(TOML)["details"]["burn"]
        self.assertEqual((b["compute_h"], b["unpriced"]), (0.0, ["v-st1", "odd"]))

    def test_profile_choice(self):
        f = self.fake(regions=account(), sts_ok=("admin", "readonly"))
        self.assertEqual(self.run_account(TOML)["details"]["profile"], "readonly")
        self.assertEqual([c["profile"] for c in f.calls if c["svc"] == "sts"], ["readonly"])
        f.calls[:] = []
        f.sts_ok = ("admin",)
        r = self.run_account(TOML)  # the configured profile is the only one tried: never a fallback to an admin login
        self.assertEqual([c["profile"] for c in f.calls if c["svc"] == "sts"], ["readonly"])
        self.assertEqual((r["status"], r["summary"]), ("unknown", "no usable AWS credentials"))
        f.calls[:] = []
        f.sts_ok = ("ci",)
        r = self.run_account("")  # no profile set: AWS_PROFILE, last run's, then the files' profiles in order
        self.assertEqual([c["profile"] for c in f.calls if c["svc"] == "sts"], ["default", "admin", "readonly", "ci"])
        self.assertEqual(r["details"]["profile"], "ci")
        f.calls[:] = []
        self.run_account("", prev=r, env={"AWS_PROFILE": "readonly"})
        self.assertEqual([c["profile"] for c in f.calls if c["svc"] == "sts"], ["readonly", "ci"])
        f.calls[:] = []
        self.run_account("", prev={"details": {"profile": "deleted-profile"}})
        self.assertNotIn("deleted-profile", [c["profile"] for c in f.calls])

    def test_finding_a_profile_has_a_time_budget(self):
        f = self.fake(regions=account(), sts_ok=(), delay=0.1)
        with mock.patch.object(aws, "IDENTITY_BUDGET", 2.05):
            r = self.run_account("")
        self.assertEqual(f.count("sts"), 1)  # expired logins can each take seconds: the rest wait for the next run
        self.assertEqual(r["details"]["profile_errors"]["admin"], "not tried: out of time")
        self.assertEqual(r["status"], "unknown")

    def test_no_config_at_all_uses_the_clis_own_chain(self):
        f = self.fake(regions=account(), sts_ok=("default",))
        r = self.run_account(TOML.replace('profile = "readonly"\n', ""), env={"AWS_CONFIG_FILE": "/nonexistent/config",
                                                                            "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent/c"})
        self.assertEqual([c["profile"] for c in f.calls if c["svc"] == "sts"], [None])
        self.assertTrue(all("--profile" not in c["argv"] for c in f.calls))
        f.calls[:] = []
        self.run_account(TOML.replace('profile = "readonly"\n', ""), prev=r,
                         env={"AWS_CONFIG_FILE": "/nonexistent/config", "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent/c"})
        self.assertTrue(all("--profile" not in c["argv"] for c in f.calls))  # "default" is a label here, not a flag

    def test_an_expired_login_is_a_soft_failure_that_keeps_the_last_inventory(self):
        self.fake(regions=account(), sts_ok=("readonly",))
        h = host(TOML)
        env = dict(self.env)
        with mock.patch.object(registry.Ctx, "run", staticmethod(self.aws)), mock.patch.object(saas, "_now", lambda: NOW):
            good = collect.run_one(registry.SOURCES["aws_account"], h, env)
            self.assertTrue(good["ok"])
            self.aws.sts_ok, self.aws.sts_err = (), SSO_EXPIRED
            rec = collect.run_one(registry.SOURCES["aws_account"], h, env)
        self.assertEqual((rec["ok"], rec["soft"], rec["kept"]), (False, True, True))
        self.assertEqual(rec["data"]["details"]["instances"], good["data"]["details"]["instances"])
        self.assertEqual(rec["error"], "no usable AWS credentials: sso-expired (aws sso login --profile readonly)")

    def test_the_cli_runs_read_only_settings_bounded_and_without_other_secrets(self):
        f = self.fake(regions=account(), budgets=[BUDGET])
        env = {"STRIPE_SECRET_KEY": "sk_live_" + "x" * 24, "AWS_BEARER_TOKEN_BEDROCK": "bedrock-fixture", "AWS_REGION": "us-east-1"}
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_" + "y" * 36}):
            self.run_account(TOML, env=env)
        self.assertTrue(f.calls)
        for c in f.calls:
            e, argv = c["env"], c["argv"]
            self.assertEqual((e["AWS_EC2_METADATA_DISABLED"], e["AWS_PAGER"], e["AWS_CLI_AUTO_PROMPT"]), ("true", "", "off"))
            self.assertEqual(e["AWS_CONFIG_FILE"], self.cfg_path)          # AWS settings ride along
            for k in ("STRIPE_SECRET_KEY", "AWS_BEARER_TOKEN_BEDROCK", "GITHUB_TOKEN"):
                self.assertNotIn(k, e)                                        # nothing else does
            self.assertLessEqual(c["timeout"], aws.CLI_TIMEOUT)
            self.assertEqual(argv[argv.index("--output") + 1], "json")
            self.assertEqual(argv[argv.index("--profile") + 1], "readonly")
            if c["svc"] != "sts":
                self.assertIsNotNone(c["region"])

    def test_at_most_max_concurrent_aws_processes_run_at_once(self):
        regions = account()
        regions["us-east-2"] = {"inst": [inst("b", "m7i.large", "running", "i-b2", az="us-east-2a")]}
        f = self.fake(regions=regions, enabled=("us-east-1", "us-east-2", "us-west-1", "us-west-2"), delay=0.02)
        self.run_account(TOML + 'max_concurrent = 2\nsweep = 3\n')
        self.assertGreaterEqual(len(f.calls), 10)
        self.assertLessEqual(f.peak, 2)

    def test_a_call_still_queued_at_the_deadline_never_starts(self):
        started = []
        res = saas._pmap({i: (lambda i=i: (started.append(i), time.sleep(0.3))[0]) for i in range(6)}, timeout=0.1, workers=2)
        time.sleep(0.4)
        self.assertEqual(len(started), 2)
        self.assertEqual(sorted(v[1] for v in res.values()), ["timeout"] * 6)

    def test_the_cli_missing_makes_no_call(self):
        f = self.fake()
        with mock.patch.object(aws, "aws_bin", return_value=None):
            r = self.run_account(TOML)
        self.assertEqual((r["status"], r["summary"]), ("unknown", "aws CLI not installed"))
        self.assertEqual(f.calls, [])

    def test_garbage_from_the_cli_never_raises_or_leaks(self):
        for out in ("<html>", "[1, 2]", '{"Account": 5}', "null", json.dumps({"Reservations": "x"})):
            def run(argv, timeout=None, env=None, **kw):
                if argv[1] == "sts":
                    return 0, json.dumps({"Account": ACCT, "Arn": "arn:aws:sts::%s:assumed-role/R/x" % ACCT}), ""
                return 0, out, ""
            self.aws = run
            r = self.run_account(TOML)
            self.assertNotEqual(r["summary"], "internal error", r.get("error"))
            self.assertNotIn(ACCT, json.dumps(r))


class AccountIds(Base):
    """A record keeps an account id's last four digits, never the whole id: not in a profile
    name (the SSO wizard puts one there), not in an error that quotes a caller's ARN."""
    SSO_NAME = "AdministratorAccess-" + ACCT

    def setUp(self):
        super().setUp()
        with open(self.cfg_path, "a") as fh:
            fh.write("\n[profile %s]\nsso_session = corp\nregion = us-east-1\n" % self.SSO_NAME)

    def test_profile_names_and_errors_are_masked(self):
        arn = "arn:aws:sts::%s:assumed-role/AdministratorAccess/someone" % ACCT
        f = self.fake(regions=account(), sts_ok=(self.SSO_NAME,), ce=ce_reply(),
                      fail=lambda s, c, r: "\nAn error occurred (Throttling) for %s: slow down\n" % arn
                      if (c, r) == ("describe-volumes", "us-east-1") else None)
        toml = '[sources.aws_account]\nprofile = "%s"\nhome_regions = ["us-east-1"]\ncost_explorer = "daily"\n' % self.SSO_NAME
        r = self.run_account(toml)
        d = r["details"]
        self.assertEqual(d["profile"], "AdministratorAccess-…4321")
        self.assertIn("…4321", d["regions"]["errors"]["us-east-1"])
        with mock.patch.object(saas, "_now", lambda: NOW):
            c = aws_cost.aws_cost(self.ctx("aws_cost", toml))
        self.assertEqual(c["profile"], "AdministratorAccess-…4321")
        f.fail = lambda s, cmd, r: "\nAn error occurred (AccessDeniedException): User: %s is not authorized\n" % arn \
            if s == "ce" else None
        with mock.patch.object(saas, "_now", lambda: utc(2026, 10, 28, 13)):
            c2 = aws_cost.aws_cost(self.ctx("aws_cost", toml, prev=c))
        f.sts_ok = ()
        f.sts_err = "\nAn error occurred (AccessDenied) when calling GetCallerIdentity: %s\n" % arn
        soft = self.run_account(toml)
        blob = json.dumps([r, c, c2, soft])
        self.assertNotIn(ACCT, blob)
        self.assertEqual(list(soft["details"]["profile_errors"]), ["AdministratorAccess-…4321"])
        self.assertEqual(aws.masked("x %s y 1234567890123" % ACCT), "x …4321 y 1234567890123")  # only 12-digit runs

    def test_last_runs_profile_is_known_by_its_masked_name(self):
        f = self.fake(regions=account(), sts_ok=(self.SSO_NAME,))
        r = self.run_account("")
        self.assertEqual(r["details"]["profile"], "AdministratorAccess-…4321")
        f.calls[:] = []
        self.run_account("", prev=r)
        self.assertEqual([c["profile"] for c in f.calls if c["svc"] == "sts"], [self.SSO_NAME])  # straight to it


class ReadOnly(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(SRC, name), encoding="utf-8") as fh:
            return fh.read()

    def test_every_aws_command_is_a_read(self):
        """Guard rail: a future edit must not add a mutating AWS command, or a second way to run one, unnoticed."""
        src = self.read("aws.py")
        cmds = set(re.findall(r'cli\(ctx, \[\s*"(\w+)", "([\w-]+)"', src))
        self.assertEqual(cmds, {("sts", "get-caller-identity"), ("ec2", "describe-regions"), ("ec2", "describe-instances"),
                                ("ec2", "describe-volumes"), ("ec2", "describe-hosts"), ("ec2", "describe-addresses"),
                                ("ec2", "describe-spot-price-history"), ("pricing", "get-products"),
                                ("budgets", "describe-budgets")})
        self.assertEqual(len(re.findall(r"(?<!def )\bcli\(ctx, ", src)), len(re.findall(r'\bcli\(ctx, \[\s*"', src)))
        self.assertEqual(src.count("ctx.run("), 1)  # inside cli(), the one way out
        cost = self.read("aws_cost.py")
        self.assertEqual(re.findall(r'\["(ce)", "([\w-]+)"', cost), [("ce", "get-cost-and-usage")])
        self.assertEqual(re.findall(r"\bcli\(", cost), ["cli("])  # the one query, through aws.cli
        self.assertIn('"--no-paginate"', cost)  # a second page would be a second billed request
        for name in ("aws.py", "aws_prices.py", "aws_cost.py"):
            text = self.read(name)
            self.assertIsNone(re.search(r"\bsubprocess\b|\bPopen\(|os\.system\(|core\.run\(|\.http\(|urlopen\(", text), name)
            for verb in ("create-", "delete-", "terminate-", "start-", "stop-", "modify-", "put-", "update-", "run-instances"):
                self.assertNotIn('"%s' % verb, text, name)


class Prices(unittest.TestCase):
    def plist(self, pairs):
        f = dict(pairs)
        if f.get("productFamily") == "Dedicated Host":
            return [json.loads(product(HOST_PRICES[f["instanceType"]]))] if f["instanceType"] in HOST_PRICES else []
        if "volumeApiName" in f:
            if f["volumeApiName"] == "io2" and f["productFamily"] == "System Operation":
                return [json.loads(product(0.065, "IOPS-Mo")), json.loads(product(0.032, "IOPS-Mo"))]
            if f["volumeApiName"] == "io2" and f["productFamily"] == "Storage":
                return [json.loads(product(0.125, "GB-Mo"))]
            got = EBS.get((f["volumeApiName"], f["productFamily"]))
            return [json.loads(product(*got))] if got else []
        return [json.loads(product(OD[f["instanceType"]]))] if f["instanceType"] in OD else []

    def test_lookups(self):
        self.assertEqual(P.lookup(self.plist, "od|us-east-1|m7i.large|Linux/UNIX|Shared"), {"usd_h": 0.1008})
        self.assertEqual(P.lookup(self.plist, "host|us-east-1|mac2-m2"), {"usd_h": 0.878})
        self.assertEqual(P.lookup(self.plist, "ebs|us-east-1|gp3"), {"gb_mo": 0.08, "iops_mo": 0.005, "mibps_mo": 0.04})
        self.assertEqual(P.lookup(self.plist, "ebs|us-east-1|io2"), {"gb_mo": 0.125, "iops_mo": 0.065})  # the first tier
        self.assertIsNone(P.lookup(self.plist, "od|us-east-1|x9.huge|Linux/UNIX|Shared"))
        self.assertIsNone(P.lookup(self.plist, "ebs|us-east-1|st1"))  # no price listed: unpriced, not guessed
        self.assertIsNone(P.lookup(self.plist, "junk|us-east-1"))

    def test_the_filters_the_price_list_is_asked_with(self):
        seen = []
        P.lookup(lambda pairs: (seen.append(P.filters(pairs)), [])[1], "od|eu-west-1|t3.large|Windows|Dedicated")
        self.assertEqual(seen[0], ["Type=TERM_MATCH,Field=regionCode,Value=eu-west-1", "Type=TERM_MATCH,Field=instanceType,Value=t3.large",
                                   "Type=TERM_MATCH,Field=operatingSystem,Value=Windows", "Type=TERM_MATCH,Field=tenancy,Value=Dedicated",
                                   "Type=TERM_MATCH,Field=preInstalledSw,Value=NA", "Type=TERM_MATCH,Field=capacitystatus,Value=Used",
                                   "Type=TERM_MATCH,Field=licenseModel,Value=No License required"])

    def test_volume_math(self):
        gp3 = {"gb_mo": 0.08, "iops_mo": 0.005, "mibps_mo": 0.04}
        self.assertAlmostEqual(P.volume_usd_mo(vol("v", 200, None, iops=6000, mbps=500), gp3), 46.0)
        self.assertAlmostEqual(P.volume_usd_mo(vol("v", 200, None, mbps=250), gp3), 21.0)
        self.assertAlmostEqual(P.volume_usd_mo(vol("v", 100, None, ty="gp2"), {"gb_mo": 0.1}), 10.0)
        self.assertAlmostEqual(P.volume_usd_mo(vol("v", 100, None, iops=1000, ty="io2"), {"gb_mo": 0.125, "iops_mo": 0.065}), 77.5)
        self.assertIsNone(P.volume_usd_mo(vol("v", 100, None), None))

    def test_refresh_keeps_fresh_refetches_stale_survives_a_failure_and_spreads_lookups(self):
        now = time.time()
        cache = {"od|r|a|Linux/UNIX|Shared": {"usd_h": 1.0, "at": now - 60},
                 "od|r|b|Linux/UNIX|Shared": {"usd_h": 2.0, "at": now - 8 * 86400},
                 "od|r|gone|Linux/UNIX|Shared": {"usd_h": 9.0, "at": now}}
        looked = []

        def pmap(calls):
            looked.extend(calls)
            return {k: (None, "throttled") for k in calls}
        out, errs = P.refresh(lambda pairs: [], cache, ["od|r|a|Linux/UNIX|Shared", "od|r|b|Linux/UNIX|Shared"], now, pmap)
        self.assertEqual(looked, ["od|r|b|Linux/UNIX|Shared"])             # only the stale one
        self.assertEqual(out["od|r|b|Linux/UNIX|Shared"]["usd_h"], 2.0)     # kept through the failure
        self.assertNotIn("od|r|gone|Linux/UNIX|Shared", out)                # unwanted keys drop out
        self.assertEqual(errs, {"od|r|b|Linux/UNIX|Shared": "throttled"})
        looked[:] = []
        wanted = ["od|r|t%d|Linux/UNIX|Shared" % i for i in range(5)]
        P.refresh(lambda pairs: [], {}, wanted, now, pmap, limit=2)
        self.assertEqual(len(looked), 2)                                    # the rest wait for the next run


# ── Cost Explorer ───────────────────────────────────────────────────────────
EC2 = "Amazon Elastic Compute Cloud - Compute"


def ce_day(day, amounts, estimated=True):
    return {"TimePeriod": {"Start": day, "End": day}, "Estimated": estimated, "Total": {},
            "Groups": [{"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v), "Unit": "USD"}}} for k, v in amounts.items()]}


def ce_reply(days=None, token=None):
    days = days or [ce_day("2026-10-26", {EC2: 42.09, "Amazon Simple Storage Service": 0.5}), ce_day("2026-10-27", {EC2: 3.0})]
    out = {"GroupDefinitions": [{"Type": "DIMENSION", "Key": "SERVICE"}], "ResultsByTime": days, "DimensionValueAttributes": []}
    if token:
        out["NextPageToken"] = token
    return out


CE_ON = TOML + 'cost_explorer = "daily"\n'


class CostExplorer(Base):
    def run_cost(self, toml=CE_ON, prev=None, env=None, now=NOW):
        with mock.patch.object(saas, "_now", lambda: now):
            return aws_cost.aws_cost(self.ctx("aws_cost", toml, prev, env))

    def test_due_once_a_day_after_noon_utc(self):
        last = utc(2026, 10, 26, 12, 5)
        self.assertTrue(aws_cost.due(utc(2026, 10, 26, 3), 0))                    # never asked
        self.assertFalse(aws_cost.due(utc(2026, 10, 27, 6), last))                # 18h: too soon
        self.assertFalse(aws_cost.due(utc(2026, 10, 27, 9), last))                # 21h, but before noon UTC
        self.assertTrue(aws_cost.due(utc(2026, 10, 27, 12, 1), last))             # the next noon
        self.assertTrue(aws_cost.due(utc(2026, 10, 28, 1), utc(2026, 10, 26, 12)))  # slept through noon: 37h
        self.assertTrue(aws_cost.due(NOW, NOW + 3 * 86400))                       # a mark from a clock since moved back

    def test_window_reaches_two_weeks_back_early_in_the_month(self):
        self.assertEqual(aws_cost.window(utc(2026, 11, 3, 13)), ("2026-10-21", "2026-11-04", "2026-11-01"))
        self.assertEqual(aws_cost.window(utc(2026, 10, 27, 13)), ("2026-10-01", "2026-10-28", "2026-10-01"))

    def test_the_query_is_one_page(self):
        asked = []
        days = [ce_day("2026-10-31", {EC2: 40, "Amazon Simple Storage Service": 1.0}),
                ce_day("2026-11-01", {EC2: 30, "EC2 - Other": 2.0, "AWS Key Management Service": 0.5}),
                ce_day("2026-11-02", {EC2: 20}), ce_day("2026-11-03", {EC2: 1})]
        out = aws_cost.query(lambda a: (asked.append(a), ce_reply(days, token="p2"))[1], utc(2026, 11, 3, 13))
        self.assertEqual(len(asked), 1)                       # never a second, billed, page
        self.assertIn("--no-paginate", asked[0])
        self.assertTrue(out["partial"])
        self.assertEqual((out["month"], out["mtd"], out["through"]), ("2026-11", 53.5, "2026-11-03"))
        self.assertEqual(out["services"], [["EC2", 51.0], ["EC2-Other", 2.0], ["KMS", 0.5]])  # this month only
        self.assertEqual(out["days"][-1], ["2026-11-03", 1.0])
        self.assertEqual(out["per_day"], {"ec2": 30.0, "ec2_other": round(2.0 / 3, 4), "other": 0.5})  # complete days only
        self.assertEqual(aws_cost.short("Amazon Elastic Container Service for Kubernetes"), "EKS")
        self.assertEqual(aws_cost.short("Amazon Some Very Long Service Name Here"), "Some Very Long")  # whole words

    def test_one_billed_request_a_day_even_when_forced(self):
        f = self.fake(ce=ce_reply())
        d1 = self.run_cost()
        self.assertEqual((f.count("ce"), d1["mtd"], d1["tried_at"]), (1, 45.59, NOW))
        ce = next(c for c in f.calls if c["svc"] == "ce")
        self.assertEqual((ce["env"]["AWS_MAX_ATTEMPTS"], ce["region"], ce["profile"]), ("1", "us-east-1", "readonly"))
        f.calls[:] = []
        for hour in (14, 18, 23):  # collect --force, collect aws_cost, the timer, a retry: the same day
            self.assertEqual(self.run_cost(prev=d1, now=utc(2026, 10, 27, hour)), d1)
        self.assertEqual(f.calls, [])  # not even STS
        self.run_cost(prev=d1, now=utc(2026, 10, 28, 12, 30))
        self.assertEqual(f.count("ce"), 1)

    def test_forced_collector_runs_make_no_second_request(self):
        """The real path: `porchlight collect --force` (and `collect aws_cost`) run the source
        every time; the source itself refuses a second request inside the gap."""
        f = self.fake(ce=ce_reply())
        h = host(CE_ON)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(secrets, "_ENV", None), \
                mock.patch.object(registry.Ctx, "run", staticmethod(f)), mock.patch.object(saas, "_now", lambda: NOW), \
                mock.patch.object(core, "which", lambda name, extra=(): "/fake/" + name), \
                mock.patch("porchlight.snapshot.write", lambda host: {}):
            for _ in range(3):
                collect.collect(h, force=True, only=["aws_cost"])
            collect.collect(h, only=["aws_cost"])
        self.assertEqual(f.count("ce"), 1)
        self.assertEqual(core.read_json(registry.src_path("aws_cost"))["data"]["mtd"], 45.59)

    def test_the_attempt_is_noted_before_the_request(self):
        """A run abandoned mid-request (the collector's deadline, a crash) still counts."""
        f = self.fake(ce=ce_reply())
        seen, real = [], f.answer

        def answer(svc, cmd, prof, region, args):
            if svc == "ce":
                seen.append(aws_cost.last_mark())
                raise KeyboardInterrupt  # the run never comes back, as if the collector abandoned it
            return real(svc, cmd, prof, region, args)
        f.answer = answer
        with self.assertRaises(KeyboardInterrupt):
            self.run_cost()
        self.assertEqual(seen, [NOW])  # the mark was down before the request left
        f.answer, f.calls[:] = real, []
        self.assertEqual(self.run_cost(prev=None, now=NOW + 3600), {})  # no record survived, but the mark did
        self.assertEqual(f.count("ce"), 0)

    def test_never_twice_on_one_utc_date(self):
        self.assertFalse(aws_cost.due(utc(2026, 10, 28, 23, 30), utc(2026, 10, 28, 0, 30)))  # 23h on, but the same date
        self.assertFalse(aws_cost.due(utc(2026, 10, 29, 12, 30), utc(2026, 10, 28, 23, 30)))  # a new date, 13h on
        self.assertTrue(aws_cost.due(utc(2026, 10, 29, 19, 30), utc(2026, 10, 28, 23, 30)))

    def test_a_peer_never_asks_while_its_hub_is_alive(self):
        f = self.fake(ce=ce_reply())
        hub_data = {"month": "2026-10", "mtd": 99.0, "tried_at": NOW - 3600, "as_of": NOW - 3600}

        def bundle(age):
            core.ensure_dirs()
            core.write_json_atomic(core.BUNDLE_PATH, {"v": 1, "from": "hub-1", "ts": NOW - age, "sources": {
                "aws_cost": {"v": 1, "name": "aws_cost", "ts": NOW - age, "ok": True, "data": hub_data}}})
        bundle(600)
        for _ in range(3):  # collect --force on a peer, again and again
            self.assertEqual(self.run_cost(), hub_data)  # the hub's reading, never a request of its own
        self.assertEqual((f.calls, aws_cost.last_mark()), ([], 0.0))
        bundle(4 * 86400)  # the hub has been gone for days: the peer takes over
        self.assertEqual(self.run_cost()["mtd"], 45.59)
        self.assertEqual(f.count("ce"), 1)
        support.clear_cache()
        bundle(600)
        self.assertEqual(self.run_cost(toml=CE_ON + '[fleet]\npeers = ["web-1"]\n')["mtd"], 45.59)  # the hub asks

    def test_any_failure_counts_as_the_days_request(self):
        f = self.fake(ce=ce_reply())
        with mock.patch.object(aws_cost, "short", side_effect=TypeError("a parser bug")):
            d = self.run_cost(prev={"mtd": 1.0})
        self.assertEqual((d["mtd"], d["ce_error"], d["tried_at"]), (1.0, "TypeError: a parser bug", NOW))
        self.assertEqual(self.run_cost(prev=d, now=NOW + 3600), d)
        self.assertEqual(f.count("ce"), 1)

    def test_an_unwritable_state_dir_means_no_request(self):
        f = self.fake(ce=ce_reply())
        with mock.patch.object(aws_cost, "mark", return_value=False):
            d = self.run_cost()
        self.assertEqual(d["status"], "unknown")
        self.assertEqual(f.count("ce"), 0)

    def test_the_switch_failures_and_retries_never_add_a_request(self):
        f = self.fake(ce=ce_reply())
        self.assertEqual(self.run_cost(toml=TOML), {"off": True})                  # the default: off
        self.assertEqual(registry.state(registry.SOURCES["aws_cost"], host(TOML)), "opt-in")
        self.assertEqual(registry.state(registry.SOURCES["aws_cost"], host(CE_ON)), "on")
        self.assertEqual(registry.state(registry.SOURCES["aws_cost"], host(TOML + "cost_explorer = true\n")), "on")  # 0.1.0 configs
        self.assertEqual(registry.state(registry.SOURCES["aws_cost"], host(CE_ON + "[sources.aws_cost]\nenabled = false\n")),
                         "disabled")
        self.assertEqual(f.calls, [])
        f.sts_ok = ()
        d = self.run_cost(prev={"mtd": 1.0, "tried_at": utc(2026, 10, 26, 12)})
        self.assertEqual(d["status"], "unknown")  # soft: the reading is kept; STS is free, so the next run may retry
        self.assertTrue(collect._soft_failure(d))
        self.assertEqual((f.count("ce"), aws_cost.last_mark()), (0, 0.0))
        f.sts_ok = ("readonly",)
        f.fail = lambda s, c, r: "\nAn error occurred (AccessDeniedException): not enabled for cost explorer\n" if s == "ce" else None
        d = self.run_cost(prev={"mtd": 1.0, "tried_at": utc(2026, 10, 26, 12)})
        self.assertEqual((d["mtd"], d["ce_error"], d["tried_at"]), (1.0, "access-denied", NOW))
        self.assertEqual(self.run_cost(prev=d, now=utc(2026, 10, 27, 20)), d)  # the gap holds after a failure too
        self.assertEqual(f.count("ce"), 1)


# ── Panels ──────────────────────────────────────────────────────────────────
def demo():
    with open(DEMO, encoding="utf-8") as fh:
        raw = json.load(fh)
    from porchlight import demo as pdemo
    return {k: pdemo._relative(v, time.time()) for k, v in raw.items() if not k.startswith("_")}


AUTO = '[sources.aws_account]\ncost_explorer = "daily"\nhome_regions = ["us-east-1"]\n'


class Panel(unittest.TestCase):
    def setUp(self):
        support.clear_cache()
        self.addCleanup(support.clear_cache)
        self.addCleanup(T.set_mode, "truecolor")
        self.addCleanup(T.set_ascii, False)

    def load(self, data=None):
        for name, d in (data or demo()).items():
            put(name, d)

    def build(self, key="aws", cw=58, toml=AUTO):
        return BUILDERS[key](RCtx(host(toml), registry.Cache(), {"vitals": {}}, [], cw))

    def text(self, key="aws", cw=58, toml=AUTO):
        p = self.build(key, cw, toml)
        return [T.strip(ln) for ln in p.lines] if p else None

    def test_the_month_prefers_the_budget_and_carries_the_burn_to_the_month_end(self):
        self.load()
        x = RCtx(host(AUTO), registry.Cache(), {"vitals": {}}, [], 58)
        mv = paws.month_view(x)
        left = paws._month_end(time.time()) - time.time()
        self.assertEqual(mv["mtd"], 287.66)
        # Per bucket, the larger of the hourly model and Cost Explorer's week: EC2-Other (2.24 > 0.0926 x 24) and
        # VPC (0.60 > 0.02 x 24) from Cost Explorer, instances from the model, the rest from Cost Explorer.
        day = 0.364 * 24 + 2.24 + 0.6 + 0.34
        self.assertAlmostEqual(paws.per_day(paws.account(x), paws.cost(x)), day, places=4)
        self.assertAlmostEqual(mv["forecast"], 287.66 + day * left / 86400, places=0)
        d = demo()
        d["aws_account"]["details"]["budgets"] = []
        d["aws_cost"]["mtd"] = 250.0
        self.load(d)
        self.assertEqual(paws.month_view(RCtx(host(AUTO), registry.Cache(), {"vitals": {}}, [], 58))["mtd"], 250.0)  # CE's
        d["aws_cost"]["month"] = "1999-01"  # last month's bill is not this month's
        self.load(d)
        self.assertIsNone(paws.month_view(RCtx(host(AUTO), registry.Cache(), {"vitals": {}}, [], 58)))

    def test_rows_name_the_money(self):
        self.load()
        rows = self.text(cw=64)
        self.assertTrue(rows[0].startswith("month    $288 ") and "72% of $400 budget" in rows[0], rows[0])
        self.assertTrue(rows[1].startswith("daily    ") and "yesterday $11.37" in rows[1], rows[1])
        self.assertTrue(rows[2].startswith("burn     $0.48/h ≈$11/day · ec2 $0.36 · ebs $0.09"), rows[2])
        self.assertTrue(rows[3].startswith("services EC2 $205 · EC2-Other $58.21 · VPC $15.62 · +3 $8.73"), rows[3])
        self.assertTrue(any(r.startswith("running  ▶ api-1") and "$0.10/h" in r for r in rows), rows)
        self.assertTrue(any("build-eu" in r and r.endswith("eu-west-1") for r in rows), rows)  # outside the home regions
        self.assertTrue(any("worker-1" in r and r.endswith("spot") for r in rows), rows)
        self.assertIn("stopped  2 · $54/mo idle: batch-1 $40 · sandbox $14", rows)
        p = self.build(cw=64)
        self.assertEqual((p.level, p.note), ("warn", "prod …4821"))  # the account's alert (a box outside home)

    def test_fits_every_width_without_cutting_a_word(self):
        self.load()
        vocab = set()
        for rec in demo().values():
            vocab |= set(re.split(r"[\s,]+", json.dumps(rec)))
        for mode, ascii_ in (("truecolor", False), ("256", False), ("none", True)):
            T.set_mode(mode)
            T.set_ascii(ascii_)
            for cw in (58, 64, 78, 90, 130):
                for key in ("aws", "money"):
                    p = self.build(key, cw)
                    rows = p.lines if key == "aws" else [ln for ln in p.lines if T.strip(ln).startswith("aws")]
                    self.assertTrue(rows, key)
                    for ln in rows:
                        self.assertLessEqual(T.width(ln), cw - 1, "%s %d: %r" % (key, cw, T.strip(ln)))
                        self.assertNotRegex(T.strip(ln), r"\w(…|\.\.\.)", "%s %d: %r" % (key, cw, T.strip(ln)))
                    for ln in p.render(cw, T.ACCENT):
                        self.assertLessEqual(T.width(ln), cw)

    def test_the_daily_row_counts_full_days_only(self):
        d = demo()
        c = d["aws_cost"]
        today = time.strftime("%Y-%m-%d", time.gmtime())
        c["through"] = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))  # read yesterday: its last day was partial
        c["days"] = [x for x in c["days"] if x[0] < today]
        c["days"][-1][1] = 0.42
        self.load(d)
        daily = next(r for r in self.text(cw=90) if r.startswith("daily"))
        two_ago = time.strftime("%b %d", time.gmtime(time.time() - 2 * 86400))
        self.assertIn("%s $12.98" % two_ago, daily)  # the last full day, named by its date
        self.assertNotIn("$0.42", daily)

    def test_what_does_not_fit_is_counted(self):
        d = demo()
        det = d["aws_account"]["details"]
        det["idle"]["items"] = [{"name": "box-%d" % i, "usd_mo": 10.0 + i} for i in range(9)][::-1]
        det["idle"]["usd_mo"] = 126.0
        self.load(d)
        stopped = next(r for r in self.text() if r.startswith("stopped"))
        self.assertRegex(stopped, r"box-8 \$18 · .* · \+\d+$")
        running = [dict(det["instances"][0], name="api-%d" % i) for i in range(8)]
        det["instances"] = running + det["instances"][4:]
        self.load(d)
        self.assertIn("+3 more running · $0.30/h", "\n".join(self.text()))

    def test_the_money_line_and_infra(self):
        self.load()
        money = "\n".join(self.text("money", 90))
        self.assertIn("aws        $288 ", money)
        self.assertIn("72% of $400 budget", money)
        self.assertIsNone(self.build("infra", 90))  # the AWS panel is in the layout: INFRA leaves it the detail
        explicit = AUTO + '[layout]\npanels = ["system", "infra", "money"]\n'
        infra = "\n".join(self.text("infra", 90, explicit))
        self.assertIn("aws      ● prod …4821 · 4/6 running · $0.48/h", infra)

    def test_over_budget_turns_the_panel_and_money_amber(self):
        d = demo()
        d["aws_account"]["details"]["budgets"][0]["actual"] = 455.0
        self.load(d)
        self.assertEqual(self.build().level, "warn")
        self.assertIn("114% of $400 budget", self.text()[0])
        self.assertEqual(self.build("money").level, "warn")

    def test_cost_explorer_switched_off_hides_its_record(self):
        self.load()
        rows = self.text(toml='[sources.aws_account]\nhome_regions = ["us-east-1"]\n')
        self.assertFalse(any(r.startswith(("daily", "services")) for r in rows), rows)
        self.assertTrue(rows[0].startswith("month    $288"))  # the budget still says where the month is

    def test_quiet_states(self):
        put("aws_account", {"status": "unknown", "summary": "no usable AWS credentials", "details": {"profile_errors": {}},
                            "error": "no usable AWS credentials: no-credentials"}, ok=False, soft=True)
        self.assertIsNone(self.build(toml=""))  # an AWS CLI that was never set up is not news
        p = self.build()  # ...unless AWS was asked for here
        self.assertIn("no usable AWS credentials", T.strip(p.lines[0]))
        put("aws_account", {"status": "unknown", "summary": "no usable AWS credentials", "details": {},
                            "error": "no usable AWS credentials: sso-expired (aws sso login --profile prod)"}, ok=False, soft=True)
        self.assertIn("aws sso login --profile prod", " ".join(" ".join(self.text(toml="")).split()))  # wrapped between words
        support.clear_cache()
        self.assertIsNone(self.build())
        d = demo()
        put("aws_account", d["aws_account"], ok=False, soft=True, kept=True, last_ok=time.time() - 7200,
            error="no usable AWS credentials: sso-expired (aws sso login --profile prod)")
        rows = self.text(cw=90)
        self.assertTrue(rows[0].startswith("✖ no usable AWS credentials: sso-expired"), rows[0])  # the kept reading says why
        self.assertTrue(self.build(cw=90).note.endswith("2h"))

    def test_malformed_records_never_break_a_panel(self):
        class Mem:
            def __init__(self, recs):
                self.recs = recs

            def rec(self, name):
                return {"ok": True, "ts": time.time(), "data": self.recs[name]} if name in self.recs else None

            def data(self, name, default=None):
                return self.recs.get(name, default)

            def error(self, name):
                return None

        def paths(obj, path=()):
            items = obj.items() if isinstance(obj, dict) else enumerate(obj[:2]) if isinstance(obj, list) else ()
            for k, v in items:
                yield path + (k,)
                yield from paths(v, path + (k,))
        base, h, n = demo(), host(AUTO), 0
        for name in base:
            for path in list(paths(base[name])) + [()]:
                for junk in (5, "x", None, [1, "a", None, {"k": 5}], {"k": 5}, True):
                    recs = json.loads(json.dumps(base))
                    if path:
                        node = recs[name]
                        for part in path[:-1]:
                            node = node[part]
                        node[path[-1]] = junk
                    else:
                        recs[name] = junk
                    for key in ("aws", "money", "infra"):
                        p = BUILDERS[key](RCtx(h, Mem(recs), {"vitals": {}}, [], 58))
                        if p:
                            p.render(58, T.ACCENT)
                        n += 1
        self.assertGreater(n, 1000)

    def test_the_full_dashboard_fits(self):
        self.load()
        with support.calm_probes():
            for w in (60, 120, 200):
                out = render.render(host(AUTO), registry.Cache(), width=w)
                for ln in out.splitlines():
                    self.assertLessEqual(T.width(ln), w)
                self.assertNotIn("panel error", T.strip(out))
            wide = T.strip(render.render(host(AUTO), registry.Cache(), width=200))
        self.assertIn("aws: 1 instance, 1 volume in eu-west-1, outside the home regions", wide)  # the attention strip


class DemoData(Base):
    def test_the_demo_records_have_the_sources_shapes(self):
        regions = account()
        regions["us-east-1"]["hosts"] = [{"id": "h-1", "fam": "mac2-m2", "ty": "mac2-m2.metal", "st": "available",
                                          "at": "2026-10-25T04:00:00+00:00", "n": 1}]
        self.fake(regions=regions, budgets=[BUDGET, SCOPED], ce=ce_reply())
        live = {"aws_account": self.run_account(TOML), "aws_cost": None}
        with mock.patch.object(saas, "_now", lambda: NOW):
            live["aws_cost"] = aws_cost.aws_cost(self.ctx("aws_cost", CE_ON))
        d = demo()
        a, la = d["aws_account"]["details"], live["aws_account"]["details"]
        self.assertEqual(set(d["aws_account"]) - set(live["aws_account"]), set())
        self.assertEqual(set(a) - set(la), set())
        for key in ("instances", "budgets"):
            self.assertEqual({k for it in a[key] for k in it} - {k for it in la[key] for k in it}, set(), key)
        for key in ("burn", "idle", "volumes", "regions"):
            self.assertEqual(set(a[key]) - set(la[key]), set(), key)
        self.assertEqual(set(d["aws_cost"]) - set(live["aws_cost"]), set())
        text = json.dumps(d)
        self.assertIsNone(re.search(r"\b\d{12}\b", text))  # no full account id
        for host_ in re.findall(r"[a-z0-9.-]+\.(?:com|org|net|io|ai|dev)\b", text):
            self.assertTrue(host_.endswith(("example.com", "example.org", "example.net")), host_)

    def test_the_reference_page_documents_every_setting(self):
        with open(os.path.join(ROOT, "docs", "reference", "aws.md"), encoding="utf-8") as fh:
            doc = fh.read()
        for name in ("aws_account", "aws_cost"):
            src = registry.SOURCES[name]
            self.assertEqual(src.scope, "global", name)
            self.assertIn("## `%s`" % name, doc)
            for key in src.defaults:
                self.assertIn(key, doc, "%s.%s is undocumented" % (name, key))


if __name__ == "__main__":
    unittest.main()

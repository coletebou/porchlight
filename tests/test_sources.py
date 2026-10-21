"""Host sources: process census, tunnels, custom commands, endpoints, backups,
file budgets, deploy drift, weather, quotes, Tailscale and MongoDB redaction."""
import json
import os
import subprocess
import time
import unittest
from unittest import mock

import support  # noqa: F401 — must be first
from support import FakeHTTP, ctx, host, resp

from porchlight import core, registry
from porchlight.sources import custom, system, weather


class Tunnels(unittest.TestCase):
    def test_forwards(self):
        self.assertEqual(system.parse_tunnel("ssh -N -L 5173:localhost:5173 -L 3000:localhost:3000 devbox"),
                         {"kind": "L", "ports": ["5173", "3000"], "host": "devbox"})
        self.assertEqual(system.parse_tunnel("/usr/bin/ssh -fN -D 1080 me@bastion"),
                         {"kind": "D", "ports": ["1080"], "host": "bastion"})
        self.assertEqual(system.parse_tunnel("autossh -M 0 -R 8080:localhost:80 relay")["kind"], "R")
        self.assertEqual(system.parse_tunnel("ssh -L127.0.0.1:9000:db:5432 jump")["ports"], ["9000"])
        self.assertEqual(system.parse_tunnel("ssh -o ServerAliveInterval=5 -p 2222 -D1081 host")["ports"], ["1081"])
        self.assertEqual(system.parse_tunnel("ssh -fND 1080 bastion"), {"kind": "D", "ports": ["1080"], "host": "bastion"})
        self.assertEqual(system.parse_tunnel("ssh -fNL 5173:localhost:5173 devbox")["ports"], ["5173"])
        self.assertEqual(system.parse_tunnel("ssh -D 127.0.0.1:1080 -N bastion")["ports"], ["1080"])
        self.assertEqual(system.parse_tunnel("ssh -NfL8080:web:80 -J jump box")["host"], "box")

    def test_not_tunnels(self):
        self.assertIsNone(system.parse_tunnel("ssh devbox"))
        self.assertIsNone(system.parse_tunnel("vim notes.txt -L"))
        self.assertIsNone(system.parse_tunnel(""))


def fake_ps(rows):
    return lambda: rows


class Census(unittest.TestCase):
    def run_procs(self, rows, toml=""):
        h = host(toml)
        with mock.patch.object(system, "ps_rows", fake_ps(rows)):
            return system.procs(ctx(h, registry.SOURCES["procs"].config(h)))

    def test_agents_orphans_mcp_zombies_tunnels(self):
        now_rows = [
            (10, 1, "me", 4e8, "S", "node /usr/local/bin/claude --resume x", False, 900),   # orphan: no tty, ppid 1, old
            (11, 9, "me", 3e8, "S", "/opt/homebrew/bin/claude", True, 50),
            (12, 9, "me", 1e8, "S", "codex app-server --listen", False, 900),              # app-server: not an orphan
            (13, 9, "me", 1e8, "S", "codex", True, 30),
            (14, 1, "me", 5e7, "S", "node /x/node_modules/@playwright/mcp/cli.js", False, 99),
            (15, 9, "me", 1, "Z", "[defunct]", False, 5),
            (16, 9, "me", 1e7, "S", "ssh -N -D 1080 bastion", False, 400),
            (17, 9, "me", 1e9, "S", "/Applications/Claude.app/Contents/MacOS/Claude", True, 400),  # the desktop app
            (18, 9, "me", 1e6, "S", "python3 -m aider --model x", True, 10),
            (19, 9, "me", 1e6, "S", "worker --queue", True, 10),
        ]
        d = self.run_procs(now_rows, '[sources.procs]\nwatch = [{ name = "worker", match = "worker --queue", min = 2 }]')
        self.assertEqual(d["agents"]["claude"]["count"], 2)
        self.assertEqual(d["agents"]["claude"]["orphans"], [10])
        self.assertEqual(d["agents"]["codex"]["sub"], {"app-server": 1, "tui": 1})
        self.assertEqual(d["agents"]["codex"]["orphans"], [])
        self.assertEqual(d["agents"]["aider"]["count"], 1)
        self.assertEqual(d["mcp"], {"count": 1, "orphans": 1})
        self.assertEqual(d["zombies"], 1)
        self.assertEqual(d["tunnels"][0]["ports"], ["1080"])
        self.assertEqual(d["top"][0]["name"], "Claude")
        msgs = " | ".join(a["msg"] for a in d["alerts"])
        self.assertIn("1 orphaned claude process", msgs)
        self.assertIn("worker: 1 running (want 2)", msgs)
        self.assertTrue(any(a.get("fix") == "kill 10" for a in d["alerts"]))

    def test_etime(self):
        self.assertEqual(system._etime("05:03"), 303)
        self.assertEqual(system._etime("1-02:00:00"), 93600)


class Custom(unittest.TestCase):
    def test_json(self):
        d = custom.parse(json.dumps({"status": "warn", "summary": "2 stale", "rows": [{"label": "a", "value": "b"}],
                                     "badge": "3/4"}), 0)
        self.assertEqual((d["status"], d["summary"], d["badge"]), ("warn", "2 stale", "3/4"))

    def test_nagios_conventions(self):
        d = custom.parse("DISK WARNING - free space: / 3 GB (8%) | /=27GB;28;30\nextra line\n", 1)
        self.assertEqual(d["status"], "warn")
        self.assertEqual(d["summary"], "DISK WARNING - free space: / 3 GB (8%)")
        self.assertEqual(d["rows"], ["extra line"])
        self.assertEqual(custom.parse("ok", 0)["status"], "ok")
        self.assertEqual(custom.parse("boom", 2)["status"], "crit")
        self.assertEqual(custom.parse("?", 3)["status"], "unknown")

    def test_lines_and_bad_json(self):
        self.assertEqual(custom.parse("a\nb\n", 0, "lines")["rows"], ["a", "b"])
        self.assertEqual(custom.parse("not json", 0, "json")["status"], "unknown")

    def test_register_runs_command_and_alerts(self):
        h = host('[[custom]]\nname = "probe1"\ncommand = "echo \'{\\"status\\": \\"crit\\", \\"summary\\": \\"down\\"}\'"\n')
        custom.register(h)
        try:
            src = registry.SOURCES["probe1"]
            d = src.fn(ctx(h, src.config(h), env=dict(os.environ)))
            self.assertEqual(d["status"], "crit")
            self.assertEqual(d["alerts"][0]["msg"], "probe1: down")
        finally:
            registry.SOURCES.pop("probe1", None)

    def test_never_shadows_builtin(self):
        h = host('[[custom]]\nname = "procs"\ncommand = "true"\n')
        self.assertEqual(custom.entries(h), [])


class Endpoints(unittest.TestCase):
    def test_two_strikes(self):
        h = host()
        cfg = {"targets": [{"label": "api", "url": "http://127.0.0.1:9/health"}], "strikes": 2, "slow_ms": 1500}
        with mock.patch.object(core, "http", side_effect=ConnectionRefusedError()):
            d1 = system.check_targets(ctx(h, cfg))
            self.assertNotIn("alerts", d1)
            d2 = system.check_targets(ctx(h, cfg, prev=d1))
        self.assertIn("api down (refused)", d2["alerts"][0]["msg"])
        with mock.patch.object(core, "http", return_value=resp(200, {"version": "1.2"})):
            d3 = system.check_targets(ctx(h, cfg, prev=d2))
        self.assertNotIn("alerts", d3)
        self.assertEqual(d3["fails"]["api"], 0)

    def test_expect_contains_show(self):
        with mock.patch.object(core, "http", return_value=resp(200, {"build": {"sha": "abc123"}, "x": "ready"})):
            r = system.probe({"url": "http://x/", "label": "x", "show": "build.sha", "format": "@{}"})
            self.assertEqual(r["info"], "@abc123")
            self.assertFalse(system.probe({"url": "http://x/", "expect": 204})["up"])
            self.assertFalse(system.probe({"url": "http://x/", "contains": "nope"})["up"])
        with mock.patch.object(core, "http", return_value=resp(503, "busy")):
            self.assertFalse(system.probe({"url": "http://x/"})["up"])


class Backups(unittest.TestCase):
    def test_service_result_overdue_and_files(self):
        now = time.time()
        runs = {"good": {"result": "success", "last": now - 3600, "next": now + 20000},
                "bad": {"result": "exit-code", "last": now - 3600, "next": now + 20000},
                "late": {"result": "success", "last": now - 3 * 86400, "next": now - 86400 * 2 + 10, "ended": 0}}
        logf = os.path.join(support.TMP, "nightly.log")
        open(logf, "w").close()
        os.utime(logf, (now - 40 * 3600, now - 40 * 3600))
        cfg = {"units": ["good", "bad", "late", "gone"], "files": [{"label": "nightly", "path": logf, "max_age_hours": 26}]}
        with mock.patch.object(system, "unit_runs", return_value=runs), mock.patch.object(core, "which", return_value="/bin/systemctl"):
            d = system.backups(ctx(host(), cfg))
        msgs = {a["msg"]: a["sev"] for a in d["alerts"]}
        self.assertEqual(msgs.get("backup bad exit-code"), "crit")
        self.assertTrue(any(m.startswith("backup late overdue") for m in msgs))
        self.assertTrue(any("unit gone not found" in m for m in msgs))
        self.assertTrue(any(m.startswith("backup nightly is") for m in msgs))
        self.assertFalse(any("good" in m for m in msgs))


class Files(unittest.TestCase):
    def test_budgets(self):
        d = os.path.join(support.TMP, "mem")
        os.makedirs(os.path.join(d, "a"), exist_ok=True)
        os.makedirs(os.path.join(d, "b"), exist_ok=True)
        with open(os.path.join(d, "a", "MEMORY.md"), "w") as f:
            f.write("x" * 950 + "\n" * 10)
        with open(os.path.join(d, "b", "MEMORY.md"), "w") as f:
            f.write("y" * 100)
        out = system.files(ctx(host(), {"targets": [{"glob": os.path.join(d, "*", "MEMORY.md"), "max_bytes": 1000}]}))
        self.assertEqual(out["rows"][0]["label"], "a/MEMORY.md")
        self.assertGreaterEqual(out["rows"][0]["pct"], 90)
        self.assertTrue(any(a["sev"] in ("warn", "crit") for a in out["alerts"]))


class Drift(unittest.TestCase):
    def test_deployed_vs_ref(self):
        repo = os.path.join(support.TMP, "repo")
        os.makedirs(repo, exist_ok=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.com")
        git = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True, env=env, check=True).stdout.strip()  # noqa: E731
        git("init", "-q")
        git("commit", "-q", "--allow-empty", "-m", "one")
        first = git("rev-parse", "HEAD")
        git("commit", "-q", "--allow-empty", "-m", "two")
        git("commit", "-q", "--allow-empty", "-m", "three")
        rev = os.path.join(support.TMP, "REVISION")
        with open(rev, "w") as f:
            f.write(first + "\n")
        d = system.drift(ctx(host(), {"targets": [{"label": "app", "repo": repo, "file": rev}]}))
        self.assertFalse(d["rows"][0]["ok"])
        self.assertEqual(d["rows"][0]["behind"], 2)
        self.assertIn("2 commits behind", d["alerts"][0]["msg"])
        with open(rev, "w") as f:
            f.write(git("rev-parse", "HEAD"))
        self.assertTrue(system.drift(ctx(host(), {"targets": [{"label": "app", "repo": repo, "file": rev}]}))["rows"][0]["ok"])


class Weather(unittest.TestCase):
    OM = {"current": {"temperature_2m": 20.4, "apparent_temperature": 19.6, "weather_code": 2, "wind_speed_10m": 9,
                      "relative_humidity_2m": 55},
          "daily": {"temperature_2m_max": [23.1], "temperature_2m_min": [12.2], "precipitation_probability_max": [30],
                    "sunrise": ["2026-10-21T07:12"], "sunset": ["2026-10-21T18:21"]}}

    def run_weather(self, cfg, routes):
        h = host()
        full = dict(registry.SOURCES["weather"].config(h))
        full.update(cfg)
        fake = FakeHTTP(routes)
        return weather.weather(ctx(h, full, http=fake)), fake

    def test_open_meteo_first_with_coordinates(self):
        d, fake = self.run_weather({"latitude": 48.85, "longitude": 2.35, "units": "metric", "place": "Paris"},
                                   {"api.open-meteo.com": resp(200, self.OM)})
        self.assertEqual((d["src"], d["temp"], d["unit"], d["hi"], d["sunset"]), ("open-meteo", 20, "°C", 23, "18:21"))
        self.assertIn("celsius", fake.calls[0][1])

    def test_falls_back_to_nws_then_wttr(self):
        nws_obs = {"properties": {"temperature": {"value": 10.0}, "textDescription": "Mostly Cloudy",
                                  "relativeHumidity": {"value": 61.2}}}
        d, _ = self.run_weather({"latitude": 38.9, "longitude": -77.0}, {
            "api.open-meteo.com": resp(500, "down"),
            "api.weather.gov/points": resp(200, {"properties": {"observationStations": "https://api.weather.gov/gridpoints/X/stations"}}),
            "gridpoints/X/stations": resp(200, {"features": [{"properties": {"stationIdentifier": "KXYZ"}}]}),
            "stations/KXYZ/observations/latest": resp(200, nws_obs)})
        self.assertEqual((d["src"], d["temp"], d["desc"], d["icon"]), ("nws", 50, "mostly cloudy", "☁️"))
        d, _ = self.run_weather({}, {"wttr.in": resp(200, "⛅️|+61°F|Partly cloudy")})
        self.assertEqual((d["src"], d["temp"], d["desc"]), ("wttr", 61, "partly cloudy"))

    def test_all_fail_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_weather({}, {"wttr.in": resp(503, "")})


class Tailscale(unittest.TestCase):
    def test_parse_and_alerts(self):
        now = time.time()
        soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3 * 86400))
        st = {"BackendState": "Stopped", "Self": {"TailscaleIPs": ["100.64.0.1"], "KeyExpiry": soon},
              "Peer": {"a": {"HostName": "Web-1", "Online": True, "LastSeen": "0001-01-01T00:00:00Z", "KeyExpiry": ""}}}
        with mock.patch.object(system, "tailscale_bin", return_value="/usr/bin/tailscale"), \
                mock.patch.object(core, "run", return_value=(0, json.dumps(st), "")):
            d = system.tailscale(ctx(host(), {"required": True, "expiry_warn_days": 14}))
        self.assertEqual((d["state"], d["ip"], d["rows"][0]["name"], d["rows"][0]["seen"]), ("Stopped", "100.64.0.1", "web-1", 0))
        msgs = [a["msg"] for a in d["alerts"]]
        self.assertTrue(any("tailscale Stopped" in m for m in msgs))
        self.assertTrue(any("key on" in m for m in msgs))
        with mock.patch.object(system, "tailscale_bin", return_value=None):
            self.assertEqual(system.tailscale(ctx(host(), {})), {"installed": False})


class Mongo(unittest.TestCase):
    def test_uri_never_in_argv_or_error(self):
        uri = "mongodb+srv://admin:hunter2secret@cluster0.example.net/app"
        seen = {}

        def fake_run(argv, timeout=4.0, env=None, **kw):
            seen["argv"], seen["env"] = argv, env
            return 1, "", "MongoServerError: bad auth for %s" % uri
        with mock.patch.object(core, "run", side_effect=fake_run), mock.patch.object(core, "which", return_value="/bin/mongosh"):
            with self.assertRaises(RuntimeError) as cm:
                system.mongodb(ctx(host(), {"uri_env": "MONGODB_URI", "env_file": "", "database": "", "slow_ms": 3000},
                                   env={"MONGODB_URI": uri}))
        self.assertNotIn("hunter2", " ".join(seen["argv"]))
        self.assertEqual(seen["env"]["PORCHLIGHT_MONGO_URI"], uri)
        self.assertNotIn("hunter2", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

"""The command line end to end: every command on an empty cache, the shell hook,
the starter config, plugins, and the demo."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

import support  # noqa: F401 — must be first

from porchlight import cli, config, core, install, registry, wizard


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv) + ["--no-refresh"])
    return rc, out.getvalue(), err.getvalue()


class Commands(unittest.TestCase):
    def setUp(self):
        support.clear_cache()

    def test_every_command_runs_on_an_empty_cache(self):
        for argv in (["--width", "120"], ["all", "--width", "80"], ["alerts"], ["doctor"], ["sources"],
                     ["sources", "--json"], ["panels"], ["json"], ["json", "weather"], ["label"], ["version"],
                     ["themes"], ["-h"], ["system", "--width", "70"], ["alerts", "--json"]):
            rc, out, err = run(*argv)
            self.assertIn(rc, (0, 1), (argv, err))
            self.assertNotIn("Traceback", err, argv)

    def test_unknown_command_and_source(self):
        self.assertEqual(run("nonsense")[0], 2)
        self.assertEqual(run("json", "nonsense")[0], 2)

    def test_login_disable(self):
        os.environ["PORCHLIGHT_DISABLE"] = "1"
        try:
            rc, out, _ = run("--login")
            self.assertEqual((rc, out), (0, ""))
        finally:
            del os.environ["PORCHLIGHT_DISABLE"]

    def test_sources_json_lists_states(self):
        rc, out, _ = run("sources", "--json")
        rows = {r["name"]: r for r in json.loads(out)}
        self.assertEqual(rows["procs"]["state"], "on")
        self.assertEqual(rows["sites"]["state"], "needs targets")

    def test_demo_renders(self):
        rc, out, _ = run("demo", "--width", "150", "--color", "none")
        self.assertEqual(rc, 0)
        self.assertIn("ATTENTION", out)
        self.assertIn("FLEET", out)


class ShellHook(unittest.TestCase):
    def test_install_is_idempotent_and_removable(self):
        home = os.path.join(support.TMP, "home")
        os.makedirs(home, exist_ok=True)
        rc_path = os.path.join(home, ".bashrc")
        with open(rc_path, "w") as f:
            f.write("export A=1\n")
        with mock.patch.object(core, "HOME", home), contextlib.redirect_stdout(io.StringIO()):
            install.install_shell("bash")
            install.install_shell("bash")
            text = core.read_text(rc_path)
            self.assertEqual(text.count(install.MARK_BEGIN), 1)
            self.assertTrue(text.startswith("export A=1\n"))
            self.assertIn("PORCHLIGHT_SHOWN", text)
            install.uninstall_shell("bash")
            self.assertEqual(core.read_text(rc_path), "export A=1\n")

    def test_snippets(self):
        self.assertIn("status is-interactive", install.shell_snippet("fish"))
        self.assertIn("--login", install.shell_snippet("zsh"))

    def test_timer_units_quote_cleanly(self):
        service, timer = install._systemd_units(300)
        self.assertIn("OnUnitActiveSec=300s", timer)
        exec_line = next(ln for ln in service.splitlines() if ln.startswith("ExecStart="))
        self.assertEqual(exec_line.count("'"), 2)  # one single-quoted word, nothing shell-only
        self.assertIn("<integer>300</integer>", install._plist(300))
        self.assertNotIn("%", install._cron_line(300).replace("\\%", ""))


class Wizard(unittest.TestCase):
    def test_generated_config_parses_cleanly(self):
        text, notes = wizard.generate()
        cfg = config.loads(text)
        self.assertEqual(cfg.problems, [], text)


class Examples(unittest.TestCase):
    """Every shipped example config parses with no unknown setting or source."""

    def test_examples_are_valid(self):
        from porchlight import sources  # noqa: F401 — registers every source
        root = os.path.join(support.ROOT, "examples")
        names = sorted(n for n in os.listdir(root) if n.endswith(".toml"))
        self.assertTrue(names)
        for n in names:
            with open(os.path.join(root, n)) as f:
                cfg = config.loads(f.read(), host="web-1")
            self.assertEqual(cfg.problems + registry.check_settings(cfg), [], n)

    def test_typo_suggestions(self):
        from porchlight import sources  # noqa: F401
        cfg = config.loads("[sources.wether]\n[sources.weather]\nlatitud = 1\n")
        probs = registry.check_settings(cfg)
        self.assertIn("unknown source sources.wether (did you mean weather?)", probs)
        self.assertIn("unknown setting sources.weather.latitud (did you mean latitude?)", probs)


class Plugins(unittest.TestCase):
    def test_plugin_registers_source_and_panel(self):
        os.makedirs(core.PLUGIN_DIR, exist_ok=True)
        path = os.path.join(core.PLUGIN_DIR, "hello.py")
        with open(path, "w") as f:
            f.write("from porchlight import source, panel, kv\n"
                    "@source('hello_src', ttl=60)\n"
                    "def hello(ctx):\n    return {'n': 3}\n"
                    "@panel('hello', title='HELLO')\n"
                    "def hello_panel(x):\n"
                    "    d = x.data('hello_src')\n"
                    "    return x.panel('hello', [kv('n', str(d['n']))]) if d else None\n")
        with open(os.path.join(core.PLUGIN_DIR, "broken.py"), "w") as f:
            f.write("raise RuntimeError('nope')\n")
        try:
            h = support.host("")
            errors = registry.load_plugins(h)
            self.assertTrue(any("broken.py" in e for e in errors))
            self.assertIn("hello_src", registry.SOURCES)
            support.put("hello_src", {"n": 3})
            rc, out, _ = run("hello", "--width", "80")
            self.assertIn("HELLO", out)
        finally:
            os.remove(path)
            os.remove(os.path.join(core.PLUGIN_DIR, "broken.py"))
            registry.SOURCES.pop("hello_src", None)


if __name__ == "__main__":
    unittest.main()

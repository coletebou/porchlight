"""Shared test scaffolding: a scratch cache, configs from TOML text, fake HTTP.

Import this module before anything from porchlight: it points the cache and
config at a temporary directory and pins the color mode, so no test ever reads
or writes the real ~/.cache or ~/.config.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="porchlight-test-")
os.environ["PORCHLIGHT_CACHE_DIR"] = os.path.join(TMP, "cache")
os.environ["PORCHLIGHT_CONFIG"] = os.path.join(TMP, "config.toml")
os.environ["PORCHLIGHT_CONFIG_DIR"] = os.path.join(TMP, "config")
os.environ["PORCHLIGHT_COLOR"] = "truecolor"
os.environ.pop("NO_COLOR", None)
os.environ.pop("PORCHLIGHT_ASCII", None)
sys.path.insert(0, str(ROOT))

from porchlight import config, core, hosts, registry, theme  # noqa: E402

theme.set_mode("truecolor")
theme.set_ascii(False)


def host(toml: str = "", name: str = "testbox"):
    """A Host from TOML text (the machine id is fixed, so [hosts] globs are testable)."""
    cfg = config.loads(toml, host=name)
    return hosts.Host(cfg)


def put(name, data, ok=True, age=0.0, **extra):
    """Write a cache record as the collector would."""
    core.ensure_dirs()
    rec = {"v": 1, "name": name, "ts": time.time() - age, "ok": ok, "data": data}
    rec.update(extra)
    core.write_json_atomic(registry.src_path(name), rec)
    return rec


def clear_cache():
    import shutil
    shutil.rmtree(core.CACHE_DIR, ignore_errors=True)
    core.ensure_dirs()


class FakeResp(core.Resp):
    pass


def resp(status=200, body=None, ms=12, headers=None):
    return core.Resp(status, body, ms, headers or {})


class FakeHTTP:
    """Route core.http-style calls by URL substring to canned responses.

        fake = FakeHTTP({"api.example.com/status": resp(200, {"ok": True})})
        with fake.patch(module):   # replaces module.http / ctx.http targets
            ...
        fake.calls  # [(method, url, headers)]
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, url, method="GET", headers=None, **kw):
        self.calls.append((method, url, dict(headers or {}), kw))
        for needle in sorted(self.routes, key=len, reverse=True):
            if needle in url:
                r = self.routes[needle]
                if isinstance(r, Exception):
                    raise r
                return r(url, method, headers, kw) if callable(r) else r
        return core.Resp(404, {"error": "no fake route for %s" % url}, 1, {})


def ctx(h, cfg=None, env=None, prev=None, http=None):
    """A source Ctx with optional fake HTTP."""
    c = registry.Ctx(h, dict(env or {}), prev, cfg or {})
    if http is not None:
        c.http = http
    return c


def strip(s: str) -> str:
    return theme.strip(s)


def dumps(obj) -> str:
    return json.dumps(obj, indent=1, default=str)

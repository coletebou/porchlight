"""The source registry and the cache view the renderer reads.

A source is a function decorated with @source(...). The collector calls it
with a Ctx and stores the JSON-able dict it returns as one record under
<cache>/src/<name>.json. A result may carry "alerts": [{"sev", "msg"}] and must
never contain a credential.

Scopes:
  local   about this machine; every machine collects its own.
  global  about an account or the fleet (SaaS, AI plans, weather). In a fleet,
          the hub collects it and ships it to peers in hub/bundle.json; a peer
          collects it itself only when the hub's copy is older than `fallback`
          seconds and the peer holds the credentials.

Identities: a source runs on, and shows alerts on, only the machines whose
identity it serves. [identities.<name>] allow/deny lists narrow that further,
so a work laptop can be limited to an explicit allowlist and never touch a
personal API key, and the reverse.
"""
from __future__ import annotations

import os

from . import core

SOURCES = {}
EVERYONE = "*"


class Source:
    def __init__(self, name, fn, ttl, timeout, scope, hosts, needs, retry, fallback, desc,
                 identities, defaults, requires, optin, share, binaries):
        self.name, self.fn, self.ttl, self.timeout = name, fn, ttl, timeout
        self.scope, self.hosts, self.needs = scope, hosts, tuple(needs)
        self.retry = retry if retry is not None else min(ttl, 180)
        self.fallback = fallback if fallback is not None else max(3 * ttl, 2700)
        self.desc = desc
        self.identities = identities
        self.defaults = dict(defaults or {})
        self.requires = tuple(requires)
        self.optin = optin
        self.share = share
        self.binaries = tuple(binaries)

    def config(self, host) -> dict:
        return host.cfg.source(self.name, self.defaults)


def source(name, ttl=600, timeout=10.0, scope="local", hosts=None, needs=(), retry=None, fallback=None,
           desc="", identities=EVERYONE, defaults=None, requires=(), optin=False, share=False, binaries=()):
    """Register a source.

    ttl        seconds between successful runs; retry: after a failure (default min(ttl, 180))
    timeout    the expected worst case; the source enforces its own I/O timeouts, `doctor`
               flags overshoots, and the collector's deadline caps every run
    scope      "local" or "global" (see the module docstring)
    hosts      None (everywhere), machine names and/or #tags, or a callable(host)
    needs      environment variables (from the shell or secrets files) that must all be set
    identities "*" or a tuple of identity names this source serves by default
    defaults   the source's settings, overridable under [sources.<name>]
    requires   settings that must be non-empty before the source runs (e.g. "targets")
    optin      off unless [sources.<name>] exists or sets enabled = true
    share      ship this host-local record to the hub inside the snapshot (fleet panels)
    binaries   executables that must be on PATH (otherwise the source is "off")
    """
    def deco(fn):
        SOURCES[name] = Source(name, fn, ttl, timeout, scope, hosts, needs, retry, fallback,
                               desc or (fn.__doc__ or "").strip().split("\n")[0], identities,
                               defaults, requires, optin, share, binaries)
        return fn
    return deco


def state(src, host) -> str:
    """Why a source does or does not run here: "on" or a short reason."""
    cfg = src.config(host)
    enabled = cfg.get("enabled")
    ident = host.identity
    allowed = cfg.get("identities", src.identities)
    if allowed not in (EVERYONE, None, ["*"]) and ident not in (allowed if isinstance(allowed, (list, tuple)) else [allowed]):
        return "identity"
    it = host.cfg.identity_table()
    allow, deny = it.get("allow"), it.get("deny") or []
    if isinstance(allow, list) and src.name not in allow and "*" not in allow:
        return "not allowed"
    if src.name in deny:
        return "denied"
    if enabled is False:
        return "disabled"
    if src.optin and enabled is not True and not host.cfg.source_configured(src.name):
        return "opt-in"
    if not host.matches(cfg.get("hosts", src.hosts)):
        return "other hosts"
    for key in src.requires:
        if not cfg.get(key):
            return "needs %s" % key
    for b in src.binaries:
        if not core.which(b, tuple(cfg.get("paths") or ())):
            return "no %s" % b
    return "on"


def serves(src, host) -> bool:
    """The source belongs to this host's identity (alerts from it may show here)."""
    return state(src, host) not in ("identity", "not allowed", "denied")


def applies(src, host) -> bool:
    return state(src, host) == "on"


class Ctx:
    """What a source sees: its host, its settings, the merged environment
    (process env + secrets files) and its own previous data (for deltas, flap
    guards and history rings)."""

    def __init__(self, host, env, prev, cfg=None):
        self.host, self.env, self.prev = host, env, prev or {}
        self.cfg = cfg if cfg is not None else {}

    http = staticmethod(core.http)
    run = staticmethod(core.run)


def src_path(name: str) -> str:
    return os.path.join(core.SRC_DIR, name + ".json")


class Cache:
    """Read-only view over local records, the hub bundle and peers' shared
    records; for each source it picks the most trustworthy fresh copy."""

    def __init__(self):
        self._recs = {}
        self.bundle = core.read_json(core.BUNDLE_PATH) or {}

    def _shared(self, name):
        out = []
        local_fleet = core.read_json(src_path("fleet")) or {}
        tables = [((local_fleet.get("data") or {}).get("hosts") or {}), ((self.bundle.get("fleet") or {}).get("hosts") or {})]
        for table in tables:
            for rec in table.values():
                shared = ((rec or {}).get("snap") or {}).get("shared") or {}
                if isinstance(shared.get(name), dict):
                    out.append(shared[name])
        return out

    def rec(self, name):
        if name not in self._recs:
            local = core.read_json(src_path(name))
            hub = (self.bundle.get("sources") or {}).get(name)
            cands = [r for r in [local, hub] + self._shared(name) if isinstance(r, dict) and r.get("data") is not None]
            # A successful record whose data carries no soft error first (a peer's
            # "token expired" must not shadow the hub's live numbers), then the newest.
            cands.sort(key=lambda r: (bool(r.get("ok")),
                                      not (isinstance(r.get("data"), dict) and r["data"].get("error")),
                                      r.get("ts", 0)), reverse=True)
            self._recs[name] = cands[0] if cands else (local if isinstance(local, dict) else None)
        return self._recs[name]

    def data(self, name, default=None):
        r = self.rec(name)
        d = r.get("data") if r else None
        return d if d is not None else default

    def age(self, name) -> float:
        r = self.rec(name)
        return core.now() - r.get("ts", 0) if r else 1e12

    def error(self, name):
        r = self.rec(name)
        return None if not r or r.get("ok") else (r.get("error") or "failed")

    def fleet_hosts(self, hub: bool) -> dict:
        """The fleet table: the hub's own `fleet` record, or a peer's copy from the bundle."""
        if hub:
            return (self.data("fleet") or {}).get("hosts") or {}
        return (self.bundle.get("fleet") or {}).get("hosts") or {}


def load_plugins(host) -> list:
    """Import user plugins (~/.config/porchlight/plugins/*.py). Returns errors."""
    if not host.cfg.get("general.plugins", True) or not os.path.isdir(core.PLUGIN_DIR):
        return []
    import importlib.util
    errors = []
    for fn in sorted(os.listdir(core.PLUGIN_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(core.PLUGIN_DIR, fn)
        try:
            spec = importlib.util.spec_from_file_location("porchlight_plugin_" + fn[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001 — a broken plugin must not take the dashboard down
            errors.append("plugin %s: %s: %s" % (fn, type(e).__name__, e))
    return errors

"""Configuration: one TOML file, per-host overrides, identities, and defaults.

    ~/.config/porchlight/config.toml   (or $PORCHLIGHT_CONFIG)

Resolution, lowest to highest precedence:
  1. built-in DEFAULTS
  2. the file's top-level tables
  3. every [hosts."<glob>"] table whose glob matches this machine, in file order
  4. PORCHLIGHT_* environment overrides (HOST, IDENTITY, THEME)

Tables merge key by key; lists and scalars replace. One file can therefore
describe a whole fleet and be shared through a dotfiles repo.
"""
from __future__ import annotations

import copy
import fnmatch
import os

from . import core

DEFAULTS = {
    "general": {
        "identity": "default",   # which identity this machine belongs to (see [identities.*])
        "label": "",             # display name; default: the short hostname
        "role": "auto",          # auto | dev | server  (servers show UTC first, "ip" instead of "lan")
        "tags": [],              # extra tags for source/host filters ("db", "gpu", ...)
        "secrets_files": [],     # KEY=VALUE files read by the collector only (never at render)
        "secrets_command": "",   # or a command that prints KEY=VALUE lines (password managers)
        "clock": "auto",         # auto | local | utc
        "clock_secondary": "auto",  # auto | "" (hide) | an IANA zone such as "Europe/Berlin"
        "plugins": True,         # import ~/.config/porchlight/plugins/*.py
        "hints": ["alerts", "doctor"],
    },
    "theme": {"name": "porchlight", "accent": "", "colors": {}, "ascii": "auto"},   # ascii: auto | true | false
    "banner": {"text": "{label}", "font": "auto", "file": "", "image": "", "image_rows": 8, "image_cols": 0,
               "colors": []},
    "layout": {
        "panels": "auto", "mode": "auto", "compact": ["system", "services", "agents", "fleet"], "compact_width": 100,
        "columns": 3, "min_column": 58, "gap": 3, "three_column_width": 180,
        "attention": 10, "attention_compact": 5,
    },
    "header": {"weather": True, "ips": True, "last_visit": True, "user": False},
    "footer": {"quote": True, "qotd": True, "hints": True, "timing": True, "quotes_file": "", "qotd_file": ""},
    "thresholds": {
        "disk_warn": 85, "disk_crit": 92, "mem_warn": 92, "mem_crit": 98,
        "swap_warn": 80, "swap_crit": 95, "load_warn": 1.0, "load_crit": 2.0,
    },
    "alerts": {"ignore": [], "ignore_units": ["motd-news.service"], "peer_max_age": 1800},
    "collector": {"deadline": 50, "workers": 10, "login_refresh": True, "interval": 300, "spawn_gap": 30},
    "fleet": {"peers": [], "order": [], "labels": {}, "ssh_options": [], "connect_timeout": 6, "push_timeout": 25,
              "remote_cache": "", "badge": ""},
    "system": {"mounts": [], "mount_labels": {}},
    "services": {"systemd": [], "systemd_user": [], "launchd": [], "auto": True, "optional": [], "remote_login": True},
    "ports": [],
    "repos": [],
    "sources": {},
    "custom": [],
    "identities": {},
    "hosts": {},
}

_IDENTITY_KEYS = {"allow", "deny", "secrets_files", "secrets_command"}


def machine_id() -> str:
    """$PORCHLIGHT_HOST, else the short lowercase hostname."""
    return (os.environ.get("PORCHLIGHT_HOST") or core.short_host()).strip().lower() or "localhost"


def _tomllib():
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - exercised on 3.9/3.10
        from ._vendor import tomli as tomllib
    return tomllib


def _load_toml(path: str):
    with open(path, "rb") as f:
        return _tomllib().load(f)


def merge(base: dict, over: dict) -> dict:
    """Deep merge: tables merge key by key, everything else replaces."""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _check(tree: dict, ref: dict, where: str, problems: list) -> None:
    """Flag unknown keys and wrong types against DEFAULTS (free-form tables skipped)."""
    for k, v in tree.items():
        if k not in ref:
            problems.append("unknown setting %s%s" % (where, k))
            continue
        want = ref[k]
        if isinstance(want, dict):
            if not isinstance(v, dict):
                problems.append("%s%s should be a table" % (where, k))
            elif want and k not in ("colors", "mount_labels", "labels"):
                _check(v, want, "%s%s." % (where, k), problems)
        elif isinstance(want, bool):
            if not isinstance(v, bool):
                problems.append("%s%s should be true or false" % (where, k))
        elif isinstance(want, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                problems.append("%s%s should be a number" % (where, k))
        elif isinstance(want, list):
            if not isinstance(v, list) and not (k == "panels" and v == "auto"):
                problems.append("%s%s should be a list" % (where, k))


class Config:
    """The resolved view for this machine. `raw` is the file as written."""

    def __init__(self, raw=None, path="", exists=False, problems=None, host=None):
        self.path, self.exists = path, exists
        self.raw = raw or {}
        self.problems = list(problems or [])
        self.machine = host or machine_id()
        top = {k: v for k, v in self.raw.items() if k != "hosts"}
        _check(top, DEFAULTS, "", self.problems)
        data = merge(DEFAULTS, top)
        self.matched = []
        hosts = self.raw.get("hosts") or {}
        if not isinstance(hosts, dict):
            self.problems.append("[hosts] should be a table of glob = {settings}")
            hosts = {}
        for pattern, over in hosts.items():
            if not isinstance(over, dict):
                self.problems.append("hosts.%s should be a table" % pattern)
                continue
            if fnmatch.fnmatchcase(self.machine, str(pattern).lower()):
                self.matched.append(pattern)
                _check({k: v for k, v in over.items() if k != "hosts"}, DEFAULTS, "hosts.%s." % pattern, self.problems)
                data = merge(data, over)
        if os.environ.get("PORCHLIGHT_IDENTITY"):
            data["general"]["identity"] = os.environ["PORCHLIGHT_IDENTITY"]
        if os.environ.get("PORCHLIGHT_THEME"):
            data["theme"]["name"] = os.environ["PORCHLIGHT_THEME"]
        for ident, sec in (data.get("identities") or {}).items():
            if not isinstance(sec, dict):
                self.problems.append("identities.%s should be a table" % ident)
                continue
            for k in sec:
                if k not in _IDENTITY_KEYS:
                    self.problems.append("unknown setting identities.%s.%s (known: %s)" % (
                        ident, k, ", ".join(sorted(_IDENTITY_KEYS))))
        self.data = data

    # ── accessors ──
    def get(self, dotted: str, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def table(self, dotted: str) -> dict:
        v = self.get(dotted, {})
        return v if isinstance(v, dict) else {}

    def items(self, dotted: str) -> list:
        """A list setting; a single table or string becomes a one-item list."""
        v = self.get(dotted, [])
        if isinstance(v, (dict, str)):
            return [v]
        return list(v) if isinstance(v, list) else []

    @property
    def identity(self) -> str:
        return str(self.get("general.identity") or "default")

    def identity_table(self) -> dict:
        t = (self.data.get("identities") or {}).get(self.identity)
        return t if isinstance(t, dict) else {}

    def secrets_files(self) -> list:
        files = list(self.items("general.secrets_files"))
        files += [f for f in self.identity_table().get("secrets_files", []) if f not in files]
        return files

    def secrets_command(self) -> str:
        return self.identity_table().get("secrets_command") or self.get("general.secrets_command") or ""

    def source(self, name: str, defaults=None) -> dict:
        """A source's settings: its declared defaults overlaid with [sources.<name>]."""
        user = (self.data.get("sources") or {}).get(name)
        return merge(defaults or {}, user if isinstance(user, dict) else {})

    def source_configured(self, name: str) -> bool:
        return isinstance((self.raw.get("sources") or {}).get(name), dict) or any(
            isinstance(((h or {}).get("sources") or {}).get(name), dict)
            for p, h in (self.raw.get("hosts") or {}).items() if p in self.matched and isinstance(h, dict))


_CACHE = {}


def load(path=None, host=None, fresh=False) -> Config:
    """Parse and resolve the config file (memoized per path and host)."""
    path = path or core.CONFIG_PATH
    key = (path, host or machine_id(), core.mtime(path))
    if not fresh and key in _CACHE:
        return _CACHE[key]
    raw, problems, exists = {}, [], os.path.exists(path)
    if exists:
        try:
            raw = _load_toml(path)
        except (OSError, ValueError) as e:  # TOMLDecodeError subclasses ValueError
            problems.append("config %s unreadable: %s" % (path, e))
    cfg = Config(raw, path, exists, problems, host)
    _CACHE.clear()
    _CACHE[key] = cfg
    return cfg


def loads(text: str, host=None) -> Config:
    """A Config from TOML text (tests, `porchlight init --check`)."""
    return Config(_tomllib().loads(text), "<string>", True, [], host)

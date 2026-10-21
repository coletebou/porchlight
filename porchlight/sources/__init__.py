"""Importing this package registers every built-in source with porchlight.registry.

Heavy fetchers (SaaS, AI plans) live in modules that import their transport on
first use, so registering them costs the login render almost nothing.
"""
import importlib
import sys

from . import system  # noqa: F401 — the core host sources are not optional

for _mod in ("fleet", "weather", "ai_limits", "saas", "saas_accounts", "dev", "custom"):
    try:
        importlib.import_module("%s.%s" % (__name__, _mod))
    except ImportError as e:  # a missing optional module must not break the rest
        if "No module named" not in str(e):
            print("porchlight: source module %s unavailable: %s" % (_mod, e), file=sys.stderr)

_DONE = set()


def register_all(host) -> list:
    """Built-ins are registered on import; this adds [[custom]] sources and user
    plugins for this host (once per process). Returns plugin errors."""
    from .. import registry
    from . import custom
    key = id(host.cfg)
    if key in _DONE:
        return []
    _DONE.add(key)
    custom.register(host)
    return registry.load_plugins(host)

"""Panel builders. Importing this package registers every built-in panel.

Builders read caches through x.cache and never touch the network. One whose
data is missing returns None (the panel is skipped) or a short "waiting for
data" line when the panel was asked for by name.
"""
import importlib
import sys

from ..ui import BUILDERS, TITLES  # noqa: F401

for _mod in ("core", "ai", "cloud", "dev", "ops"):
    try:
        importlib.import_module("%s.%s" % (__name__, _mod))
    except ImportError as e:  # a missing optional module must not break the rest
        if "No module named" not in str(e):
            print("porchlight: panel module %s unavailable: %s" % (_mod, e), file=sys.stderr)


def custom_keys(host) -> list:
    """Panel keys of [[custom]] sources that asked to be shown."""
    from ..sources import custom as _c
    return [c["panel"] for c in _c.entries(host) if c.get("show", True)]

"""porchlight: a fast, fleet-aware login dashboard for the terminal.

Plugins import the public API from here:

    from porchlight import source, panel, Panel, kv, theme

The names resolve lazily (PEP 562) so that `import porchlight` stays free for
the login render, which never needs them.
"""

__version__ = "0.1.0"

_API = {"source", "panel", "Panel", "kv", "cells", "SEP", "theme", "core", "EVERYONE"}


def __getattr__(name):
    if name in _API:
        from . import api
        return getattr(api, name)
    raise AttributeError("module 'porchlight' has no attribute %r" % name)

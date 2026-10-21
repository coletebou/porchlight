"""This machine's profile: name, label, identity, role, tags and what to show.

Everything comes from the resolved config (config.py), with platform
auto-detection for whatever the file leaves unsaid.
"""
from __future__ import annotations

import os

from . import config as C, core

# Panels in their canonical order; layout.panels = "auto" shows whichever have data.
AUTO_PANELS = ["system", "services", "agents", "docker", "fleet", "network", "ai", "prs", "access",
               "instances", "health", "edge", "infra", "comms", "backups", "files", "money", "repos"]


def _detect_role() -> str:
    """'server' for a headless Linux box, else 'dev'."""
    if core.IS_MAC or core.IS_WSL:
        return "dev"
    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if desktop or os.path.isdir("/usr/share/xsessions") or os.path.isdir("/usr/share/wayland-sessions"):
        return "dev"
    return "server"


def _norm_units(items, default_scope):
    """systemd entries: "nginx" | {unit, label, scope} → (unit, label, scope)."""
    out = []
    for it in items or []:
        if isinstance(it, str):
            label = it.split("@")[0].rsplit(".", 1)[0] if it.endswith(".service") else it
            out.append((it, label, default_scope))
        elif isinstance(it, dict) and it.get("unit"):
            out.append((str(it["unit"]), str(it.get("label") or it["unit"]), str(it.get("scope") or default_scope)))
    return out


def _norm_launchd(items):
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append((it, None))
        elif isinstance(it, dict) and it.get("label"):
            out.append((str(it["label"]), it.get("name")))
    return out


def _norm_ports(items):
    out = []
    for it in items or []:
        if isinstance(it, int):
            out.append((it, str(it), "127.0.0.1"))
        elif isinstance(it, dict) and isinstance(it.get("port"), int):
            out.append((it["port"], str(it.get("label") or it["port"]), str(it.get("host") or "127.0.0.1")))
    return out


class Host:
    def __init__(self, cfg: C.Config):
        self.cfg = cfg
        self.name = cfg.machine
        g = cfg.table("general")
        self.label = str(g.get("label") or self.name)
        self.identity = cfg.identity
        role = str(g.get("role") or "auto")
        self.role = role if role in ("dev", "server") else _detect_role()
        tags = {"#" + str(t).lstrip("#") for t in g.get("tags") or []}
        tags.add("#mac" if core.IS_MAC else "#linux")
        if core.IS_WSL:
            tags.add("#wsl")
        tags.add("#" + self.role)
        tags.add("#" + self.identity)
        self.peers = [str(p) for p in cfg.items("fleet.peers")]
        self.hub = bool(self.peers)
        if self.hub:
            tags.add("#hub")
        self.tags = tags
        sysc = cfg.table("system")
        mounts = [core.expand(m) for m in sysc.get("mounts") or []]
        self.mounts = mounts or (["/System/Volumes/Data"] if core.IS_MAC else ["/"])
        self.mount_labels = dict(sysc.get("mount_labels") or {})
        sv = cfg.table("services")
        self.units = _norm_units(sv.get("systemd"), "system") + _norm_units(sv.get("systemd_user"), "user")
        self.launchd = _norm_launchd(sv.get("launchd"))
        self.auto_services = bool(sv.get("auto", True))
        self.ports = _norm_ports(cfg.items("ports"))
        self.repos = [core.expand(r if isinstance(r, str) else (r or {}).get("path", "")) for r in cfg.items("repos")]
        self.repos = [r for r in self.repos if r]
        lay = cfg.table("layout")
        panels = lay.get("panels", "auto")
        self.auto_panels = panels == "auto" or not isinstance(panels, list)
        self.panels = list(AUTO_PANELS) if self.auto_panels else [str(p) for p in panels]
        self.compact = [str(p) for p in lay.get("compact") or []]
        self.hints = tuple(str(h) for h in (g.get("hints") or ()))

    @property
    def server(self) -> bool:
        return self.role == "server"

    def matches(self, filt) -> bool:
        """A host filter: None (everywhere), names and/or #tags, or a callable(host)."""
        if filt is None:
            return True
        if callable(filt):
            return bool(filt(self))
        items = [filt] if isinstance(filt, str) else list(filt)
        return any(f == self.name or f == self.label or f in self.tags or ("#" + str(f)) in self.tags
                   for f in items)


_CURRENT = None


def current(cfg=None, fresh=False) -> Host:
    global _CURRENT
    if cfg is not None or fresh or _CURRENT is None:
        _CURRENT = Host(cfg or C.load())
    return _CURRENT

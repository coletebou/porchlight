"""`porchlight todos`: every open todo from the cache, and `porchlight todos done <key>`.

Listing reads the cached `todos` record (the hub's copy on a peer) and never touches the
network. `done` writes through the item's own system (porchlight.sources.todos.act),
only with [sources.todos] actions = true, after a y/N prompt unless --yes. A peer that
lacks the credentials can pass --via-hub to run the same command on the hub over ssh.
"""
from __future__ import annotations

import sys

from . import core, theme as T

USAGE = """porchlight todos                 every open todo (cached), grouped by system
porchlight todos asks            ask coverage: which systems each ask id is in
porchlight todos done <key>      mark one done in its own system   [--yes] [--via-hub] [--exact]
porchlight todos --json          the items as JSON"""


def _items(cache) -> list:
    d = cache.data("todos") or {}
    return [i for i in d.get("items") or [] if isinstance(i, dict)]


def run(host, cache, args, flags) -> int:
    from .panels import todos as P
    d = cache.data("todos")
    if args[:1] == ["help"] or "-h" in flags:
        print(USAGE)
        return 0
    if d is None:
        print("porchlight: no todos yet. Configure [sources.todos] (docs/todos.md), then porchlight collect --only todos",
              file=sys.stderr)
        return 2
    items = _items(cache)
    if args[:1] == ["done"]:
        if len(args) < 2:
            print(USAGE, file=sys.stderr)
            return 2
        return done(host, cache, args[1], flags)
    if "--json" in flags:
        import json
        print(json.dumps(items if args[:1] != ["asks"] else d.get("asks") or {}, indent=2))
        return 0
    if args[:1] == ["asks"]:
        asks = d.get("asks") or {}
        if not asks:
            print("porchlight: ask tracking is off (set [sources.todos] ask_pattern and ask_systems)", file=sys.stderr)
            return 2
        systems = d.get("ask_systems") or []
        print(T.dim("%-8s %s" % ("ask", "  ".join("%-8s" % P.SHORT.get(s, s) for s in systems))))
        for a, row in asks.items():
            have = set(row.get("in") or [])
            cells = [T.c("%-8s" % T.g("ok"), "green") if s in have else T.c("%-8s" % "-", "amber") for s in systems]
            print("%-8s %s  %s" % (a, "  ".join(cells), T.dim("%d open" % row.get("open", 0))))
        return 0
    age = core.human_age(cache.age("todos"))
    print(T.dim("%d open · cached %s ago · porchlight todos done <key>" % (len(items), age)))
    by = {}
    for it in items:
        by.setdefault(it.get("provider"), []).append(it)
    for prov, rows in by.items():
        p = (d.get("providers") or {}).get(prov) or {}
        err = "" if p.get("ok", True) else T.c("  %s %s" % (T.g("crit"), p.get("error", "")), "red")
        print("\n" + T.c(prov.upper(), "sky", bold=True) + T.dim(" %d" % len(rows)) + err)
        for it in rows:
            due, dcol = P._due(it.get("due"))
            asks = " ".join(it.get("asks") or [])
            print("  %s %s%s%s" % (T.dim(str(it.get("key"))[:40].ljust(40)), it.get("title"),
                                   (" " + T.c(due, dcol)) if due else "", (" " + T.c(asks, "gold_h")) if asks else ""))
    return 0


def done(host, cache, key, flags) -> int:
    from .sources import todos as S
    try:
        # --exact: another machine named this item (hub_argv), so a prefix must never stand in for it.
        it = S.find(_items(cache), key, exact_only="--exact" in flags)
    except S.ActionError as e:
        print("porchlight: %s" % e, file=sys.stderr)
        return 2
    hub = ""
    if "--via-hub" in flags:
        hub = hub_name(host, cache)
        if not hub:
            print("porchlight: no hub known (set [fleet] hub or wait for a bundle)", file=sys.stderr)
            return 2
    print("%s %s %s" % (T.c("done?", "amber", bold=True), it.get("title"), T.dim("[%s]" % it.get("key"))))
    sys.stdout.flush()
    if "--yes" not in flags and "-y" not in flags:
        if not sys.stdin.isatty():
            print("porchlight: not a terminal; pass --yes to confirm", file=sys.stderr)
            return 2
        where = it.get("provider") + (" via %s" % hub if hub else "")
        if input("mark done in %s? [y/N] " % where).strip().lower() not in ("y", "yes"):
            print("unchanged")
            return 1
    if hub:  # asked here, so the hub runs it with --yes, on exactly this key
        rc, out, err = core.run(hub_argv(hub, str(it["key"])), timeout=40)
        sys.stdout.write(out)
        sys.stderr.write(err)
        return rc
    try:
        what = S.act(host, it, "done")
    except S.ActionError as e:
        print(T.c("%s %s" % (T.g("crit"), e), "red"), file=sys.stderr)
        return 1
    print(T.c("%s %s" % (T.g("ok"), what), "green"))
    forget(it["key"])
    from . import collect
    collect.spawn_background(host, only=["todos"])
    return 0


def forget(key: str) -> None:
    """Drop a finished item from this machine's own todos record at once, so the next
    render does not show it while the refresh runs. (A hub copy is replaced by the hub.)"""
    from . import registry
    path = registry.src_path("todos")
    rec = core.read_json(path)
    if not isinstance(rec, dict) or not isinstance(rec.get("data"), dict):
        return
    items = rec["data"].get("items") or []
    rec["data"]["items"] = [i for i in items if not (isinstance(i, dict) and i.get("key") == key)]
    rec["data"]["total"] = len(rec["data"]["items"])
    try:
        core.write_json_atomic(path, rec)
    except OSError:
        pass


def hub_name(host, cache) -> str:
    """The hub this peer's fleet view comes from: [fleet] hub, else the bundle's sender."""
    if host.hub:
        return ""
    b = cache.bundle if cache is not None and isinstance(cache.bundle, dict) else {}
    return str(host.cfg.get("fleet.hub") or b.get("from") or "")


def via_hub(host, cache) -> str:
    """The hub to act through, when this machine shows the hub's todos rather than its own:
    a peer with no todos record of its own (the credentials live on the hub). Else ""."""
    from . import registry
    if host.hub or core.read_json(registry.src_path("todos")):
        return ""
    return hub_name(host, cache)


def hub_argv(hub: str, key: str) -> list:
    """`porchlight todos done <key> --yes --exact` on the hub, over the same ssh the fleet push
    uses. The caller has asked y/N already; --exact keeps the hub from taking the key as a
    prefix of some other item when this one is gone there."""
    import shlex  # ssh hands the remote shell one string: quote the key, never trust its characters
    return [core.which("ssh") or "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", hub,
            "porchlight todos done %s --yes --exact" % shlex.quote(key)]

"""porchlight: a fast, fleet-aware login dashboard.

  porchlight                 the dashboard (what a login prints)
  porchlight all             every panel, even on a narrow terminal
  porchlight <panel>...      just those panels (porchlight panels lists them)
  porchlight alerts          every current alert, ranked, with fixes where known
  porchlight collect         refresh due sources now   [--force] [--only a,b] [--quiet]
  porchlight doctor          per-source freshness, errors, missing credentials, timers
  porchlight sources         every source: on or off here, and why
  porchlight panels          every panel and whether it has data
  porchlight json [source]   raw cached records (no secrets are ever stored)
  porchlight init            write a starter config from what this machine has
  porchlight install timer   refresh caches every 5 minutes (launchd / systemd user)
  porchlight install shell   show the dashboard once per login (zsh, bash, fish)
  porchlight uninstall ...   remove the timer or the shell hook
  porchlight demo            a sample fleet dashboard, no setup needed
  porchlight themes          preview every theme
  porchlight top [N]         the processes using the most CPU and memory
  porchlight label           this machine's display label (for prompts)
  porchlight version

Options: --width N · --theme NAME · --color truecolor|256|16|none · --ascii
         --compact · --full · --login · --no-refresh · --json
Env:     PORCHLIGHT_CONFIG · PORCHLIGHT_HOST · PORCHLIGHT_DISABLE=1 (skip the login
         render) · PORCHLIGHT_WIDTH · PORCHLIGHT_COLOR · NO_COLOR · PORCHLIGHT_ASCII
"""
from __future__ import annotations

import os
import sys

VALUE_FLAGS = {"--width", "--theme", "--color", "--only", "--shell", "--host", "--config", "--interval"}


def parse(argv):
    flags, opts, words = set(), {}, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS:
            opts[a] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if a.startswith("--") and "=" in a and a.split("=", 1)[0] in VALUE_FLAGS:
            k, v = a.split("=", 1)
            opts[k] = v
        elif a.startswith("-"):
            flags.add(a)
        else:
            words.append(a)
        i += 1
    return flags, opts, words


def _setup(flags, opts):
    """Config, host and theme for this process. Returns (host, problems)."""
    from . import config, core, hosts, theme as T
    if opts.get("--config"):
        core.CONFIG_PATH = os.path.abspath(os.path.expanduser(opts["--config"]))
    if opts.get("--host"):
        os.environ["PORCHLIGHT_HOST"] = opts["--host"]
    if opts.get("--theme"):
        os.environ["PORCHLIGHT_THEME"] = opts["--theme"]
    if opts.get("--color"):
        T.set_mode(opts["--color"])
    cfg = config.load()
    host = hosts.current(cfg)
    problems = T.setup(cfg)
    if "--ascii" in flags:
        T.set_ascii(True)
    return host, problems


def _due_any(host, cache) -> bool:
    from . import collect, core, registry
    now = core.now()
    return any(collect.is_due(s, host, now, cache) for s in registry.SOURCES.values()
               if registry.applies(s, host) and s.name != "fleet")


def main(argv=None) -> int:
    """The console entry point. Ctrl-C and a closed pipe (`porchlight | head`) exit quietly."""
    try:
        return _main(list(sys.argv[1:] if argv is None else argv))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")  # the interpreter flushes stdout again at exit
        except OSError:
            pass
        return 0


def _main(argv) -> int:
    flags, opts, words = parse(argv)
    if "-h" in flags or "--help" in flags or (words and words[0] == "help"):
        print(__doc__.strip())
        return 0
    cmd = words[0] if words else "show"
    if cmd in ("version", "--version") or "--version" in flags:
        from . import __version__
        print("porchlight %s" % __version__)
        return 0
    if "--login" in flags and os.environ.get("PORCHLIGHT_DISABLE"):
        return 0
    if cmd == "demo":
        from . import demo
        return demo.run(flags, opts, words[1:])
    host, problems = _setup(flags, opts)
    from . import registry, sources, theme as T

    if cmd == "collect":
        from . import collect
        only = [s for s in (opts.get("--only") or "").split(",") if s] or words[1:] or None
        quiet = "--quiet" in flags or "-q" in flags
        return collect.collect(host, force="--force" in flags, only=only, quiet=quiet)
    if cmd == "label":
        print(host.label)
        return 0
    if cmd == "init":
        from . import wizard
        return wizard.run(host, flags, opts)
    if cmd in ("install", "uninstall"):
        from . import install
        return install.run(cmd, words[1:], flags, opts, host)
    if cmd == "top":
        from . import top
        return top.run(int(words[1]) if len(words) > 1 and words[1].isdigit() else 10)

    plugin_errors = sources.register_all(host)
    cache = registry.Cache()

    if cmd == "json":
        import json
        if len(words) > 1:
            src = registry.SOURCES.get(words[1])
            if not src:
                print("porchlight: no source named %s (porchlight sources)" % words[1], file=sys.stderr)
                return 2
            if not registry.serves(src, host):
                print("porchlight: %s does not serve this machine's identity (%s)" % (words[1], host.identity), file=sys.stderr)
                return 2
            print(json.dumps(cache.rec(words[1]), indent=2, default=str))
        else:
            print(json.dumps({n: cache.rec(n) for n in sorted(registry.SOURCES)
                              if registry.serves(registry.SOURCES[n], host)}, indent=2, default=str))
        return 0
    if cmd == "doctor":
        from . import doctor
        return doctor.run(host, cache, list(problems) + list(plugin_errors))
    if cmd == "sources":
        return list_sources(host, cache, "--json" in flags)
    if cmd == "themes":
        from . import demo
        return demo.themes(opts)
    if cmd == "alerts":
        from . import alerts
        found = alerts.gather(host, cache, alerts.probe_all(host))
        if "--json" in flags:
            import json
            print(json.dumps(found, indent=2))
            return 1 if any(a["sev"] == "crit" for a in found) else 0
        if not found:
            print(T.c("%s no alerts" % T.g("ok"), "green"))
        for a in found:
            gl, col = alerts.glyph(a["sev"])
            print("%s %s %s" % (T.c(gl, col), a["msg"], T.dim("[%s]" % a.get("src", ""))))
            if a.get("fix"):
                print("  " + T.dim("fix: ") + T.c(a["fix"], "slate"))
        return 1 if any(a["sev"] == "crit" for a in found) else 0

    from . import panels, render
    only = None
    full = cmd == "all"
    if full:
        cmd = "show"
    if cmd == "panels":
        return list_panels(host, cache)
    if cmd != "show":
        known = set(panels.BUILDERS)
        only = [w for w in words if w in known]
        unknown = [w for w in words if w not in known]
        if unknown:
            print("porchlight: unknown command or panel: %s (porchlight -h)" % ", ".join(unknown), file=sys.stderr)
            return 2
    width = int(opts["--width"]) if (opts.get("--width") or "").isdigit() else None
    compact = True if "--compact" in flags else (False if "--full" in flags or full else None)
    login = "--login" in flags
    sys.stdout.write(render.render(host, cache, width=width, only=only, login=login, compact=compact) + "\n")
    sys.stdout.flush()
    if "--no-refresh" not in flags and host.cfg.get("collector.login_refresh", True) and _due_any(host, cache):
        from . import collect
        collect.spawn_background(host)
    return 0


def list_sources(host, cache, as_json=False) -> int:
    from . import core, registry, secrets, theme as T
    env = secrets.load(host.cfg)
    rows = []
    for name in sorted(registry.SOURCES):
        s = registry.SOURCES[name]
        why = registry.state(s, host)
        miss = [k for k in s.needs if not env.get(k)]
        rec = cache.rec(name)
        rows.append({"name": name, "state": why, "scope": s.scope, "ttl": s.ttl, "needs": list(s.needs),
                     "missing": miss, "desc": s.desc, "age": (core.now() - rec["ts"]) if rec else None,
                     "ok": bool(rec and rec.get("ok"))})
    if as_json:
        import json
        print(json.dumps(rows, indent=2))
        return 0
    print(T.dim("%-16s %-12s %-6s %6s  %s" % ("source", "here", "scope", "ttl", "what")))
    for r in rows:
        if r["state"] != "on":
            st = T.dim(r["state"][:12])
        elif r["missing"]:
            st = T.c("needs creds", "amber")
        else:
            st = T.c("on", "green")
        need = (T.dim(" · needs ") + ", ".join((T.c(k, "red") if k in r["missing"] else T.c(k, "green")) for k in r["needs"])) if r["needs"] else ""
        print("%-16s %s %-6s %6s  %s%s" % (r["name"][:16], T.pad(st, 12), r["scope"], core.human_age(r["ttl"]),
                                          T.dim(r["desc"][:70]), need))
    return 0


def list_panels(host, cache) -> int:
    from . import alerts, panels, render, theme as T
    from .ui import RCtx
    lv = alerts.probe_all(host, repos=list(host.repos))
    al = alerts.gather(host, cache, lv)
    _, cw = render.layout(render.term_width(), host.cfg)
    ctx = RCtx(host, cache, lv, al, cw)
    shown = set(host.panels) | set(panels.custom_keys(host))
    for key in sorted(panels.BUILDERS):
        try:
            p = panels.BUILDERS[key](ctx)
        except Exception as e:  # noqa: BLE001
            p, err = None, "%s: %s" % (type(e).__name__, e)
        else:
            err = ""
        state = T.c("data", "green") if p and p.lines else T.dim("empty")
        if err:
            state = T.c("error", "red")
        where = T.dim(" (in layout)") if key in shown else ""
        print("%-12s %s%s%s" % (key, T.pad(state, 6), where, T.dim("  " + err) if err else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

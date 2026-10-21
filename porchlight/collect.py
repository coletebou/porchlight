"""The collector: run due sources concurrently and write one record each.

One instance per machine (flock). Each source enforces its own I/O timeouts;
the run as a whole has a deadline, after which unfinished threads are abandoned
(the process ends with os._exit so a hung socket cannot pin it). A failed source
keeps its last good data, marked ok=false with the error, so the dashboard
degrades to "stale" instead of going blank.
"""
from __future__ import annotations

import fcntl
import os
import sys
import time

from . import core, registry, secrets
from .registry import SOURCES, Ctx, applies, src_path

PHASE2 = ("fleet",)  # needs this run's fresh records and snapshot first
SKIP_RECHECK = 6 * 3600


def _soft_failure(data) -> bool:
    """A result that reports its own failure instead of raising: {error, status:
    unknown} or {error, windows: []} (an expired login, a send-only key)."""
    if not isinstance(data, dict) or not data.get("error"):
        return False
    if "windows" in data:
        return not data.get("windows")
    return data.get("status") in (None, "unknown")


def is_due(src, host, now, cache) -> bool:
    rec = core.read_json(src_path(src.name))
    if registry.scope_of(src, host) == "global" and not host.hub:
        hub_rec = (cache.bundle.get("sources") or {}).get(src.name)
        if hub_rec and now - hub_rec.get("ts", 0) < src.fallback:
            return False
    if not rec:
        return True
    age = now - rec.get("ts", 0)
    if rec.get("skip"):
        return age >= SKIP_RECHECK
    # Soft failures (an expired login) are not transient: back off to the normal cadence.
    return age >= (src.ttl if rec.get("ok") or rec.get("soft") else src.retry)


def run_one(src, host, env) -> dict:
    prev = core.read_json(src_path(src.name)) or {}
    missing = [k for k in src.needs if not env.get(k)]
    t0 = time.monotonic()
    rec = {"v": 1, "name": src.name, "host": host.name}
    if missing:
        rec.update(ok=False, skip=True, error="missing env: " + ", ".join(missing), data=prev.get("data"))
    else:
        try:
            data = src.fn(Ctx(host, env, prev.get("data"), src.config(host)))
            if not _soft_failure(data):
                rec.update(ok=True, data=data, last_ok=core.now())
            elif str(data.get("error") or "").startswith("missing env"):
                # A source that checks its own either/or credentials: as quiet as `needs`.
                rec.update(ok=False, skip=True, error=secrets.redact(str(data["error"]), env, 200), data=prev.get("data"))
            else:
                # Keep the last good reading (marked kept) across consecutive soft failures.
                keep = prev.get("data") if (prev.get("ok") or prev.get("kept")) else None
                rec.update(ok=False, soft=True, kept=keep is not None,
                           error=secrets.redact(str(data.get("error")), env, 200),
                           data=keep if keep is not None else data, last_ok=prev.get("last_ok"),
                           fails=int(prev.get("fails", 0)) + 1)
        except Exception as e:  # noqa: BLE001 — a source must never take the run down
            rec.update(ok=False, error=secrets.redact("%s: %s" % (type(e).__name__, e), env),
                       data=prev.get("data"), last_ok=prev.get("last_ok"), fails=int(prev.get("fails", 0)) + 1,
                       kept=bool(prev.get("ok") or prev.get("kept")))  # so a soft failure next still keeps it
    rec["ts"] = core.now()
    rec["ms"] = int((time.monotonic() - t0) * 1000)
    core.write_json_atomic(src_path(src.name), rec)
    return rec


def _lock():
    core.ensure_dirs()
    fh = open(core.LOCK_PATH, "a+")  # noqa: SIM115 — held for the whole run: it is the lock
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def lock_free() -> bool:
    fh = _lock()
    if fh is None:
        return False
    fh.close()
    return True


def log(line: str) -> None:
    try:
        core.ensure_dirs()
        if core.mtime(core.LOG_PATH) and os.path.getsize(core.LOG_PATH) > 256 * 1024:
            os.replace(core.LOG_PATH, core.LOG_PATH + ".1")
        with open(core.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line + "\n")
    except OSError:
        pass


def collect(host, force=False, only=None, quiet=True) -> int:
    fh = _lock()
    if fh is None:
        if not quiet:
            print("porchlight: a collector is already running", file=sys.stderr)
        return 0
    # Imported here, not at the top: the login render imports this module for
    # is_due(), and concurrent.futures drags in logging (see core.parallel).
    from concurrent.futures import ThreadPoolExecutor, wait

    from . import snapshot, sources
    sources.register_all(host)
    env = secrets.load(host.cfg)
    cache = registry.Cache()
    now = core.now()
    picked = set(only or ())
    unknown = [n for n in picked if n not in SOURCES]
    if unknown and not quiet:
        print("porchlight: unknown source(s): %s" % ", ".join(sorted(unknown)), file=sys.stderr)
    todo = [s for s in SOURCES.values()
            if applies(s, host) and s.name not in PHASE2 and (not picked or s.name in picked)
            and (force or picked or is_due(s, host, now, cache))]
    t0 = time.monotonic()
    deadline = float(host.cfg.get("collector.deadline", 50))
    scheduled = len(todo)
    ex = ThreadPoolExecutor(max_workers=max(1, int(host.cfg.get("collector.workers", 10))))
    futs = {ex.submit(run_one, s, host, env): s for s in todo}
    done, pending = wait(futs, timeout=deadline)
    results = [f.result() for f in done]
    try:
        snapshot.write(host)
    except Exception as e:  # noqa: BLE001
        log("snapshot failed: %s" % e)
    fleet = SOURCES.get("fleet")
    if fleet and applies(fleet, host) and (not picked or "fleet" in picked) \
            and (force or picked or is_due(fleet, host, now, registry.Cache())):
        left = max(8.0, deadline - (time.monotonic() - t0))
        scheduled += 1
        f2 = ex.submit(run_one, fleet, host, env)
        d2, p2 = wait([f2], timeout=left)
        results += [f.result() for f in d2]
        pending |= p2
    took = time.monotonic() - t0
    errs = ["%s: %s" % (r["name"], r.get("error")) for r in results if not r.get("ok") and not r.get("skip")]
    log("ran %d/%d sources in %.1fs%s%s" % (
        len(results), scheduled, took,
        " · timed out: " + ",".join(futs[f].name for f in pending if f in futs) if pending else "",
        " · errors: " + " | ".join(errs) if errs else ""))
    if not quiet:
        for r in sorted(results, key=lambda r: r["name"]):
            state = next((s for s in ("ok", "skip", "soft") if r.get(s)), "FAIL")
            print("%-4s %-20s %6dms  %s" % (state, r["name"], r.get("ms", 0), r.get("error") or ""))
        for f in pending:
            print("TIME %-20s (abandoned at the deadline)" % (futs[f].name if f in futs else "fleet"))
        print("- %d sources in %.1fs" % (len(results), took))
    sys.stdout.flush()
    if pending:
        os._exit(0)  # a stuck thread must not keep the process alive
    ex.shutdown(wait=False)
    fh.close()
    return 0


def spawn_background(host) -> bool:
    """Fire-and-forget `porchlight collect` from the login render
    (stale-while-revalidate). Rate-limited, so twenty tmux panes opening at
    once start one collector."""
    import subprocess
    stamp = os.path.join(core.STATE_DIR, "last_spawn")
    gap = float(host.cfg.get("collector.spawn_gap", 30))
    if core.now() - core.mtime(stamp) < gap or not lock_free():
        return False
    try:
        core.ensure_dirs()
        with open(stamp, "w") as f:
            f.write(str(int(core.now())))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = "import sys; sys.path.insert(0, %r); from porchlight.cli import main; sys.exit(main(['collect', '--quiet']))" % root
        logf = open(core.LOG_PATH, "a")  # noqa: SIM115 — handed to the detached child
        env = dict(os.environ, PORCHLIGHT_HOST=host.name, PORCHLIGHT_CACHE_DIR=core.CACHE_DIR)
        if host.cfg.path and host.cfg.path != "<string>":
            env["PORCHLIGHT_CONFIG"] = host.cfg.path  # --config must reach the child, or it collects another config
        subprocess.Popen([sys.executable, "-I", "-c", code], stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                         start_new_session=True, close_fds=True, env=env)
        return True
    except OSError:
        return False

"""`porchlight top [N]`: where the CPU and memory went, without leaving the shell."""
from __future__ import annotations

from . import core, live, theme as T
from .sources.system import ps_rows, short_name


def _cpu_rows():
    """(pid, pcpu, rss, args) from ps; procfs has no instantaneous CPU%."""
    if core.IS_MAC:
        argv = ["/bin/ps", "-axww", "-o", "pid=,pcpu=,rss=,args="]
    else:
        argv = ["ps", "-eww", "-o", "pid=,pcpu=,rss=,args="]
    rc, out, _ = core.run(argv, 6)
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit():
            try:
                rows.append((int(parts[0]), float(parts[1]), int(parts[2]) * 1024, parts[3]))
            except ValueError:
                continue
    return rows


def run(n: int = 10) -> int:
    from . import config, hosts
    host = hosts.current(config.load())
    v = live.vitals(host)
    m, s = v["mem"], v["swap"]
    print(T.c("porchlight top", T.gradient(T.ACCENT, 0.4), bold=True) + T.dim("  ·  %s · load %.2f on %d cores · mem %s/%s%s" % (
        host.label, v["load"][0], v["cores"], core.human_bytes(m["used"]), core.human_bytes(m["total"]),
        (" · swap %s/%s" % (core.human_bytes(s["used"]), core.human_bytes(s["total"]))) if s["total"] else "")))
    rows = _cpu_rows()
    if not rows:
        rows = [(r[0], 0.0, r[3], r[5]) for r in ps_rows()]
    width = 60
    print()
    print(T.dim("%6s %6s %8s  %s" % ("pid", "cpu%", "rss", "top CPU")))
    for pid, cpu, rss, args in sorted(rows, key=lambda r: -r[1])[:n]:
        print("%6d %s %8s  %s" % (pid, T.pad(T.sev("%5.1f" % cpu, T.level_for(cpu, 50, 90)), 6, ">"),
                                  core.human_bytes(rss), T.fit(short_name(args) + T.dim("  " + args), width + 20)))
    print()
    print(T.dim("%6s %6s %8s  %s" % ("pid", "cpu%", "rss", "top memory")))
    total = m["total"] or 1
    for pid, cpu, rss, args in sorted(rows, key=lambda r: -r[2])[:n]:
        share = 100.0 * rss / total
        print("%6d %6.1f %s  %s" % (pid, cpu, T.pad(T.sev(core.human_bytes(rss), T.level_for(share, 10, 25)), 8, ">"),
                                    T.fit(short_name(args) + T.dim("  " + args), width + 20)))
    return 0

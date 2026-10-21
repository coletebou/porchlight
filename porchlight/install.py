"""`porchlight install timer|shell|all` and `porchlight uninstall timer|shell|all`.

timer  refresh the caches every collector.interval seconds (default 300):
       a launchd agent on macOS, a systemd user timer on Linux, else a crontab line.
shell  print the dashboard once per login session: a marked block in ~/.zshrc,
       ~/.bashrc or fish's conf.d, re-written in place on every install.
"""
from __future__ import annotations

import os
import shlex
import sys

from . import core

LABEL = "dev.porchlight.collect"
MARK_BEGIN, MARK_END = "# >>> porchlight >>>", "# <<< porchlight <<<"


def package_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def collect_argv() -> list:
    """The collector command for timers: this interpreter, isolated, importing this
    package from where it is installed (pipx venv, pip --user or a checkout)."""
    import json
    # json.dumps, not repr: a double-quoted path keeps the code free of single
    # quotes, so shlex.quote() yields one plain '...' word systemd also parses.
    code = 'import sys; sys.path.insert(0, %s); from porchlight.cli import main; sys.exit(main(["collect", "--quiet"]))' % (
        json.dumps(package_root()))
    return _python() + ["-I", "-c", code]


def _python() -> list:
    """A venv's own interpreter, else `env python3` when that is this same
    interpreter: a versioned path (…/python@3.13/bin/python3.13) vanishes on the
    next package-manager upgrade and would silently stop the timer."""
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return [sys.executable]
    on_path = core.which("python3")
    if on_path and os.path.realpath(on_path) == os.path.realpath(sys.executable):
        return ["/usr/bin/env", "python3"]
    return [sys.executable]


def shell_command() -> str:
    """How a login shell should start porchlight."""
    on_path = core.which("porchlight")
    if on_path:
        return "porchlight"
    launcher = os.path.join(package_root(), "bin", "porchlight")
    if os.access(launcher, os.X_OK):
        return shlex.quote(launcher)
    return "%s -I -m porchlight" % shlex.quote(sys.executable)


def _say(msg):
    print(msg)


# ── timers ──────────────────────────────────────────────────────────────────
def _plist(interval: int) -> str:
    from xml.sax.saxutils import escape
    args = "".join("        <string>%s</string>\n" % escape(a) for a in collect_argv())
    path = ":".join([os.path.join(core.HOME, ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                     "/usr/sbin", "/sbin"])
    env = "        <key>PATH</key>\n        <string>%s</string>\n" % escape(path)
    for k in ("PORCHLIGHT_CONFIG", "PORCHLIGHT_CACHE_DIR", "PORCHLIGHT_HOST"):
        if os.environ.get(k):
            env += "        <key>%s</key>\n        <string>%s</string>\n" % (k, escape(os.environ[k]))
    log = os.path.join(core.HOME, "Library", "Logs", "porchlight-collect.log")
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>%s</string>
    <!-- porchlight's collector: refreshes the dashboard caches. It runs in the GUI
         session, where Keychain items (e.g. the Claude Code login) are readable. -->
    <key>ProgramArguments</key>
    <array>
%s    </array>
    <key>StartInterval</key>
    <integer>%d</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>Nice</key>
    <integer>10</integer>
    <key>LowPriorityIO</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
%s    </dict>
    <key>StandardOutPath</key>
    <string>%s</string>
    <key>StandardErrorPath</key>
    <string>%s</string>
</dict>
</plist>
""" % (LABEL, args, interval, env, escape(log), escape(log))


def _systemd_units(interval: int):
    argv = " ".join(shlex.quote(a) for a in collect_argv()).replace("%", "%%")
    env_lines = "".join("Environment=%s=%s\n" % (k, os.environ[k].replace("%", "%%"))
                        for k in ("PORCHLIGHT_CONFIG", "PORCHLIGHT_CACHE_DIR", "PORCHLIGHT_HOST") if os.environ.get(k))
    service = """[Unit]
Description=Refresh the porchlight dashboard caches
Documentation=https://github.com/coletebou/porchlight

[Service]
Type=oneshot
# User units get a minimal PATH; sources shell out to tailscale, git, ssh,
# systemctl, docker and aws.
Environment=PATH=%%h/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
%sExecStart=%s
TimeoutStartSec=120
Nice=15
IOSchedulingClass=idle
""" % (env_lines, argv)
    minutes = max(1, interval // 60)
    timer = """[Unit]
Description=Refresh the porchlight dashboard caches every %d min
Documentation=https://github.com/coletebou/porchlight

[Timer]
OnBootSec=1min
OnUnitActiveSec=%ds
# A wall-clock tick as a floor: a monotonic-only timer can wedge at
# NextElapse=infinity after a daemon re-exec; this self-heals it.
OnCalendar=*:0/%d
RandomizedDelaySec=30
Persistent=true

[Install]
WantedBy=timers.target
""" % (minutes, interval, max(minutes * 2, 10))
    return service, timer


def _cron_line(interval: int) -> str:
    minutes = max(1, interval // 60)
    cmd = " ".join(shlex.quote(a) for a in collect_argv()).replace("%", "\\%")  # cron turns a bare % into a newline
    return "*/%d * * * * %s >/dev/null 2>&1 %s" % (minutes, cmd, MARK_BEGIN)


def install_timer(host, force_cron=False) -> int:
    interval = int(host.cfg.get("collector.interval", 300))
    if core.IS_MAC and not force_cron:
        d = os.path.join(core.HOME, "Library", "LaunchAgents")
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(core.HOME, "Library", "Logs"), exist_ok=True)
        path = os.path.join(d, LABEL + ".plist")
        with open(path, "w") as f:
            f.write(_plist(interval))
        uid = str(os.getuid())
        core.run(["/bin/launchctl", "bootout", "gui/%s/%s" % (uid, LABEL)], 5)
        rc, _, err = core.run(["/bin/launchctl", "bootstrap", "gui/" + uid, path], 8)
        if rc != 0:  # older macOS or no GUI domain (ssh): the legacy verb
            rc, _, err = core.run(["/bin/launchctl", "load", "-w", path], 8)
        _say(("installed %s (every %ds)" % (path, interval)) if rc == 0 else "wrote %s but launchctl said: %s" % (path, err.strip()))
        if sys.executable == "/usr/bin/python3":
            _say("note: Apple's /usr/bin/python3 links LibreSSL, which fails TLS with some APIs; "
                 "installing porchlight with Homebrew's python3 or pipx avoids that.")
        return 0 if rc == 0 else 1
    if core.which("systemctl") and not force_cron:
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(core.HOME, ".config"), "systemd", "user")
        os.makedirs(d, exist_ok=True)
        service, timer = _systemd_units(interval)
        with open(os.path.join(d, "porchlight-collect.service"), "w") as f:
            f.write(service)
        with open(os.path.join(d, "porchlight-collect.timer"), "w") as f:
            f.write(timer)
        core.run(["systemctl", "--user", "daemon-reload"], 10)
        rc, _, err = core.run(["systemctl", "--user", "enable", "--now", "porchlight-collect.timer"], 15)
        if rc != 0:
            _say("wrote the units to %s but systemctl said: %s" % (d, err.strip()))
            _say("no user systemd session (a container, or ssh without lingering)? try: porchlight install timer --cron")
            return 1
        _say("installed porchlight-collect.timer (every %ds) in %s" % (interval, d))
        rc, out, _ = core.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"], 3)
        if "Linger=no" in out:
            _say("note: user timers stop when you log out. To keep collecting: loginctl enable-linger $USER")
        return 0
    return _install_cron(interval)


def _install_cron(interval: int) -> int:
    if not core.which("crontab"):
        _say("no launchd, systemd or crontab here: run `porchlight collect --quiet` from your scheduler every 5 minutes")
        return 1
    rc, cur, _ = core.run(["crontab", "-l"], 5)
    lines = [ln for ln in (cur.splitlines() if rc == 0 else []) if MARK_BEGIN not in ln]
    lines.append(_cron_line(interval))
    rc, _, err = core.run(["crontab", "-"], 5, input_text="\n".join(lines) + "\n")
    _say("installed a crontab entry (every %d min)" % max(1, interval // 60) if rc == 0 else "crontab failed: %s" % err.strip())
    return 0 if rc == 0 else 1


def uninstall_timer() -> int:
    done = []
    if core.IS_MAC:
        path = os.path.join(core.HOME, "Library", "LaunchAgents", LABEL + ".plist")
        core.run(["/bin/launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], 5)
        if os.path.exists(path):
            os.remove(path)
            done.append(path)
    if core.which("systemctl"):
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(core.HOME, ".config"), "systemd", "user")
        core.run(["systemctl", "--user", "disable", "--now", "porchlight-collect.timer"], 10)
        for n in ("porchlight-collect.timer", "porchlight-collect.service"):
            p = os.path.join(d, n)
            if os.path.exists(p):
                os.remove(p)
                done.append(p)
        core.run(["systemctl", "--user", "daemon-reload"], 10)
    if core.which("crontab"):
        rc, cur, _ = core.run(["crontab", "-l"], 5)
        if rc == 0 and MARK_BEGIN in cur:
            keep = [ln for ln in cur.splitlines() if MARK_BEGIN not in ln]
            core.run(["crontab", "-"], 5, input_text="\n".join(keep) + ("\n" if keep else ""))
            done.append("crontab entry")
    _say("removed: " + (", ".join(done) if done else "nothing was installed"))
    return 0


# ── shell hook ──────────────────────────────────────────────────────────────
def shell_snippet(shell: str) -> str:
    cmd = shell_command()
    if shell == "fish":
        return "\n".join([
            MARK_BEGIN,
            "# The dashboard once per login session (not in every tmux pane or subshell).",
            "if status is-interactive; and not set -q PORCHLIGHT_SHOWN",
            "    set -gx PORCHLIGHT_SHOWN 1",
            "    %s --login" % cmd,
            "end",
            MARK_END])
    return "\n".join([
        MARK_BEGIN,
        "# The dashboard once per login session (not in every tmux pane or subshell).",
        'if [[ $- == *i* && -z "$PORCHLIGHT_SHOWN" ]]; then',
        "    export PORCHLIGHT_SHOWN=1",
        "    if command -v timeout >/dev/null 2>&1; then timeout 5 %s --login; else %s --login; fi" % (cmd, cmd),
        "fi",
        MARK_END])


def rc_file(shell: str) -> str:
    if shell == "zsh":
        return os.path.join(os.environ.get("ZDOTDIR") or core.HOME, ".zshrc")
    if shell == "fish":
        return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(core.HOME, ".config"),
                            "fish", "conf.d", "porchlight.fish")
    return os.path.join(core.HOME, ".bashrc")


def _strip_block(text: str) -> str:
    out, skip = [], False
    for line in text.splitlines():
        if line.strip() == MARK_BEGIN:
            skip = True
            continue
        if line.strip() == MARK_END:
            skip = False
            continue
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip("\n") + ("\n" if out else "")


def install_shell(shell: str, hushlogin=False) -> int:
    path = rc_file(shell)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cur = core.read_text(path)
    body = _strip_block(cur)
    new = (body + ("\n" if body and not body.endswith("\n\n") else "") + shell_snippet(shell) + "\n") if shell != "fish" \
        else shell_snippet(shell) + "\n"
    with open(path, "w") as f:
        f.write(new)
    _say("%s the porchlight block in %s" % ("updated" if MARK_BEGIN in cur else "added", path))
    if shell == "bash" and core.IS_MAC:
        _say("note: macOS Terminal starts bash as a login shell, which reads ~/.bash_profile; make it source ~/.bashrc.")
    if hushlogin:
        hl = os.path.join(core.HOME, ".hushlogin")
        if not os.path.exists(hl):
            open(hl, "a").close()
            _say("created %s (silences the system MOTD and 'Last login' line)" % hl)
    return 0


def uninstall_shell(shell: str) -> int:
    path = rc_file(shell)
    cur = core.read_text(path)
    if MARK_BEGIN not in cur:
        _say("no porchlight block in %s" % path)
        return 0
    if shell == "fish":
        os.remove(path)
    else:
        with open(path, "w") as f:
            f.write(_strip_block(cur))
    _say("removed the porchlight block from %s" % path)
    return 0


def _shell(opts) -> str:
    sh = opts.get("--shell") or os.path.basename(os.environ.get("SHELL") or "bash")
    return sh if sh in ("zsh", "bash", "fish") else "bash"


def run(cmd, words, flags, opts, host) -> int:
    what = words[0] if words else ""
    if what not in ("timer", "shell", "all"):
        print("usage: porchlight %s timer|shell|all [--shell zsh|bash|fish] [--cron] [--hushlogin] [--print]" % cmd)
        return 2
    if "--print" in flags:
        if what in ("shell", "all"):
            print(shell_snippet(_shell(opts)))
        if what in ("timer", "all"):
            interval = int(host.cfg.get("collector.interval", 300))
            print(_plist(interval) if core.IS_MAC else "\n".join(_systemd_units(interval)) if core.which("systemctl")
                  else _cron_line(interval))
        return 0
    rc = 0
    if cmd == "install":
        if what in ("timer", "all"):
            rc |= install_timer(host, force_cron="--cron" in flags)
        if what in ("shell", "all"):
            rc |= install_shell(_shell(opts), hushlogin="--hushlogin" in flags)
    else:
        if what in ("timer", "all"):
            rc |= uninstall_timer()
        if what in ("shell", "all"):
            rc |= uninstall_shell(_shell(opts))
    return rc

"""Actions the interactive dashboard (porchlight -i) runs on a panel's items.

A panel builder attaches ui.Item rows with ui.Action entries; each Action names a
handler registered with @action. The built-ins (open_url, copy, run) live here; a
source registers its own write handlers next to its API client (todos.done in
sources/todos.py). Importing this module is cheap (no network, no third-party
imports); only porchlight -i ever runs a handler.

    from porchlight.actions import action, Result

    @action("todos.done", writes=True)
    def todo_done(host, args):
        ...                       # load credentials with secrets.load(host.cfg), call the API
        return Result(True, "marked done")

Policy (enforced by run(), not by each handler):

- Read-only by default. An action with writes=True is refused unless the config says
  [interactive] allow_writes = true, and the TUI asks the Action's confirm question
  (y/N) before it calls run(..., confirmed=True).
- A handler loads its own credentials when it runs (secrets.load), never from the
  Action's args, and must never put a credential in its Result message: run() masks
  token shapes in every message anyway.
- A handler that raises becomes Result(False, "<Type>: <message>") with the message masked.
- After a write the TUI asks the collector to refresh the sources named in
  Result.refresh (porchlight collect --only ...), so the panel shows the new state.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HANDLERS = {}
# Programs the "run" action may start: argv[0]'s basename must be one of these
# ([interactive] run_allow replaces the list). Cached text can name a session or a
# host, never which program runs.
RUN_ALLOW = ("tmux", "ssh", "drover", "ai", "porchlight", "gh", "glab")


class Result:
    def __init__(self, ok, msg="", refresh=()):
        self.ok, self.msg = bool(ok), str(msg or "")
        self.refresh = tuple(refresh or ())


class Handler:
    def __init__(self, name, fn, writes, desc):
        self.name, self.fn, self.writes, self.desc = name, fn, writes, desc


def action(name, writes=False, desc=""):
    """Register an action handler: fn(host, args) -> Result. writes=True marks a handler
    that changes state anywhere (the Action on an item must say writes=True as well)."""
    def deco(fn):
        HANDLERS[name] = Handler(name, fn, bool(writes), desc or (fn.__doc__ or "").strip().split("\n")[0])
        return fn
    return deco


def writes_allowed(host) -> bool:
    return host.cfg.get("interactive.allow_writes", False) is True


def run(host, act, confirmed=False) -> Result:
    """Run one ui.Action under the policy above."""
    from . import secrets
    h = HANDLERS.get(act.name)
    if h is None:
        return Result(False, "no action named %s" % act.name)
    writes = h.writes or act.writes
    if writes and not writes_allowed(host):
        return Result(False, "read-only: set [interactive] allow_writes = true to %s" % act.label)
    if writes and not confirmed:
        return Result(False, "not confirmed")
    try:
        res = h.fn(host, dict(act.args))
    except Exception as e:  # noqa: BLE001 — an action must never take the TUI down
        res = Result(False, "%s: %s" % (type(e).__name__, e))
    if not isinstance(res, Result):
        res = Result(bool(res), "")
    res.msg = secrets.mask_shapes(res.msg)[:300]
    return res


# ── Built-ins ───────────────────────────────────────────────────────────────
def _opener():
    for exe in (("open",) if sys.platform == "darwin" else ("xdg-open", "wslview")):
        path = shutil.which(exe)
        if path:
            return path
    return None


@action("open_url")
def open_url(host, args):
    """Open args["url"] in the browser (open / xdg-open), or show it when there is none
    (an SSH session): the TUI prints the URL so the terminal can make it clickable."""
    url = str(args.get("url") or "")
    if not url.startswith(("https://", "http://")):
        return Result(False, "not a web URL")
    exe = _opener()
    if not exe or (os.environ.get("SSH_CONNECTION") and sys.platform != "darwin"):
        return Result(True, url)
    try:
        subprocess.Popen([exe, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return Result(False, str(e))
    return Result(True, "opened " + url)


@action("copy")
def copy(host, args):
    """Copy args["text"] to the clipboard with OSC 52 (works over SSH and in tmux with
    set-clipboard on)."""
    import base64
    text = str(args.get("text") or "")
    if not text:
        return Result(False, "nothing to copy")
    seq = "\033]52;c;%s\a" % base64.b64encode(text.encode()).decode()
    if os.environ.get("TMUX"):
        seq = "\033Ptmux;\033%s\033\\" % seq
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(seq)
    except OSError as e:
        return Result(False, str(e))
    return Result(True, "copied")


@action("run")
def run_command(host, args):
    """Run args["argv"] (a list, never a shell string) in the foreground: the TUI leaves
    the alternate screen first and comes back when it exits. For "jump to a Drover
    session" (tmux switch-client / ssh -t host tmux attach) and similar."""
    argv = args.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return Result(False, "bad argv")
    allow = host.cfg.get("interactive.run_allow", list(RUN_ALLOW))
    if os.path.basename(argv[0]) not in (allow if isinstance(allow, list) else RUN_ALLOW):
        return Result(False, "%s is not in [interactive] run_allow" % os.path.basename(argv[0]))
    if not shutil.which(argv[0]):
        return Result(False, "%s not found" % argv[0])
    try:
        rc = subprocess.call(argv)
    except OSError as e:
        return Result(False, str(e))
    return Result(rc == 0, "exit %d" % rc)

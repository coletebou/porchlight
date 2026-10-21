# Troubleshooting

Start with:

```sh
porchlight doctor      # config problems, timer state, every source: age, state, last error, missing credentials
porchlight sources     # every source and why it is or is not on here
porchlight alerts      # every alert, ranked, with fix hints
porchlight json NAME   # the raw cached record of one source
porchlight collect --force --only NAME   # run one source now, in the foreground
```

## A panel is empty or missing

- `porchlight panels` shows which panels have data. In `auto` layout a panel
  without data is hidden.
- `porchlight sources` explains why a source is off: `needs targets` (add the
  setting), `opt-in` (add `[sources.NAME]`), `no docker` (binary not on PATH),
  `identity` / `not allowed` (this machine's identity does not serve it).
- `doctor` shows `skip · missing env: X`: the variable is not in your
  environment or `secrets_files`. Remember the timer's environment is not your
  shell's: put credentials in a secrets file, not only in `~/.zshrc`.

## The data is stale

- `doctor` says the timer is not loaded: `porchlight install timer`.
- Linux user timers stop when you log out unless lingering is on:
  `loginctl enable-linger $USER`.
- No systemd (containers, some minimal images): `porchlight install timer --cron`.
- A login with stale data starts one background collector itself (at most one
  per 30 s), so the next login is fresh even without a timer.

## The dashboard looks garbled

- Boxes or `_` instead of glyphs inside tmux: the locale is not UTF-8 (SSH
  often drops `LANG`). `export LANG=C.UTF-8` (or `en_US.UTF-8`), and ask your SSH
  client to send it (`SendEnv LANG LC_*`). porchlight switches to ASCII mode
  automatically in that case.
- Odd colors: try `PORCHLIGHT_COLOR=256`, or a theme made for your background
  (`daylight` for light terminals).
- Misaligned emoji: some fonts draw weather emoji narrow. `[header] weather =
  false`, or ASCII mode, removes them.

## macOS

- `ssl.SSLError` against some APIs from Apple's `/usr/bin/python3`: it links
  LibreSSL 2.8.3. Install with Homebrew's Python or pipx.
- The Claude Code login lives in the Keychain, readable from your GUI session
  (the launchd timer, a GUI terminal) but not over SSH: the AI panel says so
  instead of failing. Use the timer, or `fallback_hosts` on another machine.
- Disk usage is the Data volume (`/System/Volumes/Data`), which is what fills up.

## Fleet

- `doctor` on a peer shows the hub bundle's age; on the hub, FLEET shows each
  peer's last error.
- The hub uses `ssh -o BatchMode=yes <alias>`: test with `ssh -o BatchMode=yes
  <alias> true`. Extra options go in `fleet.ssh_options`.
- A peer shows "reachable · porchlight not installed yet" until its first
  collector run writes a snapshot.

## Slow login

`rendered Nms` in the footer is the in-process render time (usually 10-40
ms). If a login feels slow, check `time porchlight --no-refresh`: Python's own
startup dominates on slow disks. A hung network mount is guarded (0.8 s, then
"not responding"); everything else at login is local.

## Starting over

```sh
rm -rf ~/.cache/porchlight          # the cache: rebuilt by the next collect
porchlight uninstall all            # the timer and the shell hook
```

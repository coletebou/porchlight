#!/bin/sh
# porchlight installer without pip: a git checkout plus a launcher on your PATH.
#   curl -fsSL https://raw.githubusercontent.com/coletebou/porchlight/master/install.sh | sh
# Re-run to update. Options (env): PORCHLIGHT_HOME (checkout, default ~/.local/share/porchlight),
# PORCHLIGHT_BIN (launcher dir, default ~/.local/bin), PORCHLIGHT_SETUP=1 (also run init + install all).
set -eu
repo="https://github.com/coletebou/porchlight.git"
home="${PORCHLIGHT_HOME:-$HOME/.local/share/porchlight}"
bin="${PORCHLIGHT_BIN:-$HOME/.local/bin}"

py=""
for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    py="$c"; break
  fi
done
[ -n "$py" ] || { echo "porchlight needs Python 3.9 or newer" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "porchlight's installer needs git" >&2; exit 1; }

if [ -d "$home/.git" ]; then
  git -C "$home" pull --ff-only --quiet
  echo "updated $home"
else
  mkdir -p "$(dirname "$home")"
  git clone --quiet --depth 1 "$repo" "$home"
  echo "installed $home"
fi
mkdir -p "$bin"
pyabs="$(command -v "$py")"
rm -f "$bin/porchlight"   # an older install left a symlink here
printf '#!/bin/sh\nexec "%s" "%s/bin/porchlight" "$@"\n' "$pyabs" "$home" > "$bin/porchlight"
chmod +x "$bin/porchlight"
echo "installed $bin/porchlight (runs $pyabs)"
case ":$PATH:" in *":$bin:"*) ;; *) echo "note: add $bin to your PATH" ;; esac

"$home/bin/porchlight" version
if [ "${PORCHLIGHT_SETUP:-0}" = "1" ]; then
  [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/porchlight/config.toml" ] || "$home/bin/porchlight" init
  "$home/bin/porchlight" install all
else
  echo "next: porchlight demo · porchlight init · porchlight install all"
fi

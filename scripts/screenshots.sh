#!/bin/sh
# Regenerate the README screenshots from the demo fleet.
#   scripts/screenshots.sh            → docs/img/*.svg
# With rsvg-convert installed, PNG previews land next to them in /tmp.
set -eu
cd "$(dirname "$0")/.."
out=docs/img
mkdir -p "$out"
pl() { python3 bin/porchlight "$@"; }
svg() { python3 scripts/ansi2svg.py "$@"; }

pl demo --width 190 --color truecolor | svg --title "ssh atlas" > "$out/demo.svg"
pl demo --width 132 --color truecolor | svg --title "ssh atlas · 132 columns" > "$out/demo-laptop.svg"
pl demo --width 78 --color truecolor | svg --title "ssh atlas · phone" > "$out/demo-compact.svg"
PORCHLIGHT_COLOR=truecolor pl themes --color truecolor | svg --title "porchlight themes" > "$out/themes.svg"
pl demo --width 132 --theme nord --color truecolor | svg --title "theme: nord" > "$out/demo-nord.svg"
pl demo --width 120 --ascii --color none | svg --title "ASCII mode, no color" > "$out/demo-ascii.svg"

if command -v rsvg-convert >/dev/null 2>&1; then
  for f in "$out"/*.svg; do rsvg-convert "$f" -o "/tmp/$(basename "${f%.svg}").png"; done
  echo "PNG previews in /tmp"
fi
ls -la "$out"

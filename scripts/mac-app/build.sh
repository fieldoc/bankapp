#!/bin/bash
# Build BankApp.app (the macOS dashboard launcher) and install it to ~/Applications.
#
#   scripts/mac-app/build.sh              # build from the committed PNG masters
#   scripts/mac-app/build.sh --render     # re-rasterize the PNG masters from the SVGs first
#
# Reproducible: builds the iconset, compiles the stay-open applet, embeds the icon,
# installs to ~/Applications, and re-registers with LaunchServices so Spotlight/Dock
# pick it up. Safe to re-run; it replaces any prior install.
#
# Two icon masters, because one drawing cannot serve every size. At 16px the detailed
# mark's thin ribbon is ~1.25px wide and disappears, so sizes <=64px use icon-small.svg
# (heavier ribbons, wider gap, flat colour) and sizes >=128px use icon.svg. The SVGs are
# the source of truth; the PNGs beside them are committed so a plain build needs no
# rasterizer. Pass --render to regenerate them (needs Google Chrome; there is no
# rsvg-convert/Inkscape/ImageMagick on this machine).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$SCRIPT_DIR/build"          # gitignored staging area
APP="$STAGE/BankApp.app"
DEST="$HOME/Applications/BankApp.app"
FINANCE="$HOME/BankApp/.venv/bin/finance"
BIG="$SCRIPT_DIR/icon-1024.png"
SMALL="$SCRIPT_DIR/icon-small-1024.png"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

echo "==> Sanity: stable finance install"
if [[ ! -x "$FINANCE" ]]; then
  echo "WARNING: $FINANCE not found/executable — the app will fail to start the server." >&2
  echo "         (Build continues; fix the install or edit the path in BankApp.applescript.)" >&2
fi

if [[ "${1:-}" == "--render" ]]; then
  echo "==> Rasterizing PNG masters from SVG"
  if [[ ! -x "$CHROME" ]]; then
    echo "ERROR: --render needs Google Chrome at $CHROME" >&2
    exit 1
  fi
  for pair in "icon.svg:$BIG" "icon-small.svg:$SMALL"; do
    svg="$SCRIPT_DIR/${pair%%:*}"; out="${pair##*:}"
    "$CHROME" --headless --disable-gpu --hide-scrollbars --screenshot="$out" \
      --window-size=1024,1024 --default-background-color=00000000 "file://$svg" >/dev/null 2>&1
    [[ -s "$out" ]] || { echo "ERROR: failed to render $svg" >&2; exit 1; }
    echo "    $(basename "$out")"
  done
fi

for f in "$BIG" "$SMALL"; do
  [[ -s "$f" ]] || { echo "ERROR: missing icon master $f (run with --render)" >&2; exit 1; }
done

echo "==> Building iconset"
rm -rf "$STAGE"; mkdir -p "$STAGE"
ICONSET="$STAGE/BankApp.iconset"; mkdir -p "$ICONSET"
# name:pixels:master — <=64px gets the simplified drawing, >=128px the detailed one.
for spec in 16:16:S 16@2x:32:S 32:32:S 32@2x:64:S \
            128:128:B 128@2x:256:B 256:256:B 256@2x:512:B 512:512:B 512@2x:1024:B; do
  name="${spec%%:*}"; rest="${spec#*:}"; px="${rest%%:*}"; which="${rest##*:}"
  src="$BIG"; [[ "$which" == "S" ]] && src="$SMALL"
  /usr/bin/sips -z "$px" "$px" "$src" --out "$ICONSET/icon_${name}.png" >/dev/null 2>&1
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$STAGE/AppIcon.icns"

echo "==> Compiling stay-open applet"
# -s = stay-open (its `on quit` runs the server-teardown); -x would be execute-only (we keep it readable).
/usr/bin/osacompile -s -o "$APP" "$SCRIPT_DIR/BankApp.applescript"

echo "==> Embedding icon"
# AppleScript applets reference Contents/Resources/applet.icns (CFBundleIconFile=applet),
# so replacing that file swaps the generic applet icon for ours.
cp "$STAGE/AppIcon.icns" "$APP/Contents/Resources/applet.icns"
# Nudge Finder/Dock to drop the cached generic icon.
touch "$APP"

echo "==> Installing to ~/Applications"
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
cp -R "$APP" "$DEST"
touch "$DEST"

# Re-register so Spotlight and Dock see the fresh copy (best-effort).
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
[[ -x "$LSREGISTER" ]] && "$LSREGISTER" -f "$DEST" || true

echo
echo "Installed: $DEST"
echo "Launch it from Spotlight (⌘-Space → \"BankApp\"), Finder, or drag it to your Dock."
echo "Quitting the app (⌘-Q) stops the dashboard server."

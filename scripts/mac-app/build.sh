#!/bin/bash
# Build BankApp.app (the macOS dashboard launcher) and install it to ~/Applications.
#
#   scripts/mac-app/build.sh
#
# Reproducible: regenerates the icon, compiles the stay-open applet, embeds the icon,
# installs to ~/Applications, and re-registers with LaunchServices so Spotlight/Dock
# pick it up. Safe to re-run; it replaces any prior install.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$SCRIPT_DIR/build"          # gitignored staging area
APP="$STAGE/BankApp.app"
DEST="$HOME/Applications/BankApp.app"
FINANCE="$HOME/BankApp/.venv/bin/finance"

echo "==> Sanity: stable finance install"
if [[ ! -x "$FINANCE" ]]; then
  echo "WARNING: $FINANCE not found/executable — the app will fail to start the server." >&2
  echo "         (Build continues; fix the install or edit the path in BankApp.applescript.)" >&2
fi

echo "==> Generating icon"
rm -rf "$STAGE"; mkdir -p "$STAGE"
/usr/bin/python3 "$SCRIPT_DIR/make_icon.py" "$STAGE/icon_1024.png" >/dev/null

ICONSET="$STAGE/BankApp.iconset"; mkdir -p "$ICONSET"
# name:size pairs for a full macOS iconset
for pair in 16:16 16@2x:32 32:32 32@2x:64 128:128 128@2x:256 256:256 256@2x:512 512:512 512@2x:1024; do
  name="${pair%%:*}"; px="${pair##*:}"
  /usr/bin/sips -z "$px" "$px" "$STAGE/icon_1024.png" --out "$ICONSET/icon_${name}.png" >/dev/null 2>&1
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

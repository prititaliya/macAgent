#!/usr/bin/env bash
# Build Release MacAgent.app, package a DMG, then replace the installed app
# and relaunch (quits the old UI + daemon first).
#
# Env:
#   SKIP_INSTALL=1   — only build the DMG (no quit / replace / relaunch)
#   INSTALL_DIR=…    — override install location (default: /Applications)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT/MacAgentApp"
DIST="$ROOT/dist"
DERIVED="$APP_DIR/DerivedDataRelease"
VOL="MacAgent"
STAGE="$DIST/dmg-stage"
INSTALL_DIR="${INSTALL_DIR:-/Applications}"
INSTALLED_APP="$INSTALL_DIR/MacAgent.app"

"$ROOT/automation/sync_version.sh"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
DMG_NAME="MacAgent-${VERSION}.dmg"

cd "$APP_DIR"
if command -v xcodegen >/dev/null 2>&1; then
  xcodegen generate >/dev/null
  mkdir -p MacAgent.xcodeproj/project.xcworkspace/xcshareddata
  cp -f WorkspaceSettings.xcsettings MacAgent.xcodeproj/project.xcworkspace/xcshareddata/ 2>/dev/null || true
fi

echo "Building Release ${VERSION}…"
xcodebuild \
  -project MacAgent.xcodeproj \
  -scheme MacAgent \
  -configuration Release \
  -derivedDataPath "$DERIVED" \
  -quiet \
  build

APP="$DERIVED/Build/Products/Release/MacAgent.app"
if [[ ! -d "$APP" ]]; then
  echo "Missing $APP" >&2
  exit 1
fi

# Ad-hoc sign so Gatekeeper doesn't report an unsigned download as "damaged".
# (Developer ID + notarization still required for zero-friction installs.)
echo "Ad-hoc signing…"
codesign --force --deep --sign - "$APP"
xattr -cr "$APP" 2>/dev/null || true

rm -rf "$STAGE" "$DIST/$DMG_NAME"
mkdir -p "$STAGE" "$DIST"

cp -R "$APP" "$STAGE/MacAgent.app"
xattr -cr "$STAGE/MacAgent.app" 2>/dev/null || true
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/README.txt" <<EOF
MacAgent ${VERSION}
===================

1. Drag MacAgent.app into Applications.
2. If macOS says the app is "damaged", it is Gatekeeper quarantine on an
   unsigned build — not a corrupt download. In Terminal run:
     xattr -cr /Applications/MacAgent.app
   then open MacAgent from Applications again.
3. Clone the backend (one-time):
     git clone https://github.com/prititaliya/macAgent.git ~/MacAgent
     cd ~/MacAgent && ./automation/setup_backend.sh
4. Open MacAgent from Applications (menu bar sparkles icon).
5. Grant Accessibility when prompted (needed for Control-Option-Space).
6. Press Control-Option-Space to summon the overlay.

Full setup guide: https://github.com/prititaliya/macAgent#setup

Hotkey: Control-Option-Space
EOF

echo "Creating DMG…"
hdiutil create \
  -volname "$VOL" \
  -srcfolder "$STAGE" \
  -ov \
  -format UDZO \
  "$DIST/$DMG_NAME"

rm -rf "$STAGE"
echo "Wrote $DIST/$DMG_NAME"
ls -lh "$DIST/$DMG_NAME"

if [[ "${SKIP_INSTALL:-0}" == "1" ]]; then
  echo "SKIP_INSTALL=1 — leaving the running app alone."
  exit 0
fi

echo
echo "Replacing installed MacAgent + restarting daemon…"

# Quit UI and anything listening on the daemon port.
"$ROOT/automation/kill_macagent.sh" --daemon >/dev/null || true
# Extra sweep: any uvicorn/python serving this repo's main.py.
pkill -9 -f "${ROOT}/main\\.py" 2>/dev/null || true
pkill -9 -f "${HOME}/MacAgent/main\\.py" 2>/dev/null || true
pkill -9 -f "${HOME}/Projects/MacAgent/main\\.py" 2>/dev/null || true
lsof -ti :8081 | xargs kill -9 2>/dev/null || true
sleep 0.4

install_app() {
  local dest="$1"
  local parent
  parent="$(dirname "$dest")"
  mkdir -p "$parent"
  rm -rf "$dest"
  # ditto preserves quarantine/signing metadata better than cp -R.
  if command -v ditto >/dev/null 2>&1; then
    ditto "$APP" "$dest"
  else
    cp -R "$APP" "$dest"
  fi
  xattr -dr com.apple.quarantine "$dest" 2>/dev/null || true
  echo "  Installed → $dest"
}

# Always update /Applications (or INSTALL_DIR). Also refresh ~/Applications if present.
if ! install_app "$INSTALLED_APP" 2>/dev/null; then
  echo "  Could not write $INSTALLED_APP — trying ~/Applications…" >&2
  INSTALLED_APP="$HOME/Applications/MacAgent.app"
  install_app "$INSTALLED_APP"
fi

if [[ -d "$HOME/Applications/MacAgent.app" && "$INSTALLED_APP" != "$HOME/Applications/MacAgent.app" ]]; then
  install_app "$HOME/Applications/MacAgent.app" || true
fi

echo "  Launching ${INSTALLED_APP}…"
open "$INSTALLED_APP"

# Wait briefly for the app to bring the daemon up from this repo (or ~/MacAgent).
for _ in 1 2 3 4 5 6 7 8; do
  if curl -sf "http://127.0.0.1:8081/health" >/dev/null 2>&1; then
    echo "  Daemon ready on :8081"
    echo "Done — MacAgent ${VERSION} is live (⌃⌥Space)."
    exit 0
  fi
  sleep 0.5
done

echo "  App launched; daemon still starting (check menu bar / Logs/MacAgent if needed)."
echo "Done — MacAgent ${VERSION} installed."

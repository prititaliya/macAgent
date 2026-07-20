#!/usr/bin/env bash
# Build Release MacAgent.app and package a DMG.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT/MacAgentApp"
DIST="$ROOT/dist"
DERIVED="$APP_DIR/DerivedDataRelease"
VOL="MacAgent"
STAGE="$DIST/dmg-stage"

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

rm -rf "$STAGE" "$DIST/$DMG_NAME"
mkdir -p "$STAGE" "$DIST"

cp -R "$APP" "$STAGE/MacAgent.app"
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/README.txt" <<EOF
MacAgent ${VERSION}
===================

1. Drag MacAgent.app into Applications.
2. Clone the backend (one-time):
     git clone https://github.com/prititaliya/macAgent.git ~/MacAgent
     cd ~/MacAgent && ./automation/setup_backend.sh
3. Open MacAgent from Applications (menu bar sparkles icon).
4. Grant Accessibility when prompted (needed for Control-Option-Space).
5. Press Control-Option-Space to summon the overlay.

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

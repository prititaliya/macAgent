#!/usr/bin/env bash
# Build Release MacAgent.app and package a DMG.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT/MacAgentApp"
DIST="$ROOT/dist"
DERIVED="$APP_DIR/DerivedDataRelease"
VOL="MacAgent"
DMG_NAME="MacAgent-0.5.0.dmg"
STAGE="$DIST/dmg-stage"

cd "$APP_DIR"
if command -v xcodegen >/dev/null 2>&1; then
  xcodegen generate >/dev/null
  mkdir -p MacAgent.xcodeproj/project.xcworkspace/xcshareddata
  cp -f WorkspaceSettings.xcsettings MacAgent.xcodeproj/project.xcworkspace/xcshareddata/ 2>/dev/null || true
fi

echo "Building Release…"
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

# Brief install note
cat > "$STAGE/README.txt" <<'EOF'
MacAgent
========

1. Drag MacAgent.app into Applications.
2. Open MacAgent (menu bar icon).
3. Grant Accessibility when prompted (needed for ⌃⌥Space).
4. Point FreeFlow LLM base URL at: http://127.0.0.1:8081/v1
5. Keep the Python project + venv at the same machine path the app
   was built against, or set MacAgentRoot / run from the repo so the
   daemon can start (venv/bin/python main.py).

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

#!/usr/bin/env bash
# Fix Xcode "accessing build database" / invalid reuse by resetting DerivedData
# and pointing this project at MacAgentApp/DerivedData.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/MacAgentApp"
PROJ="$APP/MacAgent.xcodeproj"
WS="$PROJ/project.xcworkspace/xcshareddata"

echo "Stopping MacAgent / xcodebuild…"
pkill -x MacAgent 2>/dev/null || true
pkill -f "xcodebuild.*MacAgent" 2>/dev/null || true
sleep 1

echo "Removing corrupted global DerivedData…"
rm -rf "$HOME/Library/Developer/Xcode/DerivedData/MacAgent-"*

echo "Removing project-local DerivedData…"
rm -rf "$APP/DerivedData"

mkdir -p "$WS"
cp "$APP/WorkspaceSettings.xcsettings" "$WS/WorkspaceSettings.xcsettings"

if command -v xcodegen >/dev/null 2>&1; then
  (cd "$APP" && xcodegen generate)
  # xcodegen may recreate workspace — re-apply settings
  mkdir -p "$WS"
  cp "$APP/WorkspaceSettings.xcsettings" "$WS/WorkspaceSettings.xcsettings"
fi

echo "Building once with local DerivedData…"
xcodebuild \
  -project "$PROJ" \
  -scheme MacAgent \
  -configuration Debug \
  -derivedDataPath "$APP/DerivedData" \
  -quiet \
  build

echo
echo "Done. In Xcode:"
echo "  1. Quit Xcode completely (Cmd+Q)"
echo "  2. open $PROJ"
echo "  3. Product → Run"
echo
echo "Or: ./automation/open_macagent.sh"

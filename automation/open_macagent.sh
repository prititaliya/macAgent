#!/usr/bin/env bash
# Build and open MacAgent overlay using project-local DerivedData.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/MacAgentApp"
PROJ="$APP/MacAgent.xcodeproj"
DERIVED="$APP/DerivedData"
WS="$PROJ/project.xcworkspace/xcshareddata"

if [[ ! -d "$PROJ" ]] || command -v xcodegen >/dev/null 2>&1; then
  if command -v xcodegen >/dev/null 2>&1; then
    (cd "$APP" && xcodegen generate)
  fi
fi

mkdir -p "$WS"
cp "$APP/WorkspaceSettings.xcsettings" "$WS/WorkspaceSettings.xcsettings"

mkdir -p "$DERIVED"
echo "Building MacAgent…"
xcodebuild \
  -project "$PROJ" \
  -scheme MacAgent \
  -configuration Debug \
  -derivedDataPath "$DERIVED" \
  -quiet \
  build

BUILT="$DERIVED/Build/Products/Debug/MacAgent.app"
if [[ ! -d "$BUILT" ]]; then
  echo "App missing at $BUILT" >&2
  exit 1
fi

open "$BUILT"
echo "Opened MacAgent — menu bar sparkles; ⌃⌥Space toggles overlay."

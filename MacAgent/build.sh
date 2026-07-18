#!/usr/bin/env bash
# Build MacAgent.app (SwiftUI desktop) without opening Xcode.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/MacAgent/Sources"
OUT_DIR="$ROOT/MacAgent/build"
APP="$OUT_DIR/MacAgent.app"
MACOS="$APP/Contents/MacOS"
RES="$APP/Contents/Resources"

mkdir -p "$MACOS" "$RES"
rm -rf "$APP"
mkdir -p "$MACOS" "$RES"

swiftc \
  -target arm64-apple-macos13.0 \
  -sdk "$(xcrun --show-sdk-path --sdk macosx)" \
  -parse-as-library \
  -O \
  -o "$MACOS/MacAgent" \
  "$SRC/MacAgentApp.swift" \
  "$SRC/AppModel.swift" \
  "$SRC/DaemonManager.swift" \
  "$SRC/ContentView.swift" \
  "$SRC/LivePanelView.swift" \
  "$SRC/HistoryView.swift" \
  "$SRC/SitesView.swift" \
  "$SRC/AppsView.swift" \
  "$SRC/SettingsView.swift" \
  "$SRC/DebugView.swift" \
  "$SRC/StatusView.swift"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>MacAgent</string>
  <key>CFBundleIdentifier</key>
  <string>com.macagent.app</string>
  <key>CFBundleName</key>
  <string>MacAgent</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.4.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>LSUIElement</key>
  <false/>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
  <key>MacAgentRoot</key>
  <string>${ROOT}</string>
</dict>
</plist>
EOF

echo "Built $APP"

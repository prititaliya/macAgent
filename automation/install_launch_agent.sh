#!/usr/bin/env bash
# Install / uninstall LaunchAgent so MacAgent runs at login (no terminal needed).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.macagent.daemon"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PYTHON="$ROOT/venv/bin/python3"
LOG_DIR="$HOME/Library/Logs/MacAgent"

usage() {
  echo "Usage: $0 install|uninstall|status"
  exit 1
}

[[ $# -ge 1 ]] || usage
CMD="$1"

if [[ "$CMD" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $PLIST"
  exit 0
fi

if [[ "$CMD" == "status" ]]; then
  launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | head -20 || echo "Not loaded"
  curl -s "http://127.0.0.1:8081/health" || echo "Daemon not responding on :8081"
  exit 0
fi

[[ "$CMD" == "install" ]] || usage

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing venv python at $PYTHON — create venv and pip install first." >&2
  exit 1
fi

mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"

# Stop any foreground daemon on 8081
if lsof -ti :8081 >/dev/null 2>&1; then
  echo "Stopping process on :8081…"
  lsof -ti :8081 | xargs kill 2>/dev/null || true
  sleep 1
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${ROOT}/main.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/daemon.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/daemon.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl start "$LABEL" || true

sleep 1
echo "Installed LaunchAgent: $PLIST"
echo "Logs: $LOG_DIR/"
curl -s "http://127.0.0.1:8081/health" && echo || echo "Waiting for health… check $LOG_DIR/daemon.err"

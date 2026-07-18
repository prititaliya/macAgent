#!/usr/bin/env bash
# Install Chrome Native Messaging host for MacAgent.
# Usage:
#   ./automation/install_native_host.sh
#   ./automation/install_native_host.sh --extension-id=<chrome-extension-id>

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST_NAME="com.macagent.native_host"
HOST_SCRIPT="$ROOT/automation/native_host.py"
PYTHON_BIN="$ROOT/venv/bin/python3"
NM_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
MANIFEST_PATH="$NM_DIR/${HOST_NAME}.json"

EXTENSION_ID=""
for arg in "$@"; do
  case "$arg" in
    --extension-id=*)
      EXTENSION_ID="${arg#*=}"
      ;;
  esac
done

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

if [[ ! -f "$HOST_SCRIPT" ]]; then
  echo "Missing native host script: $HOST_SCRIPT" >&2
  exit 1
fi

chmod +x "$HOST_SCRIPT"
mkdir -p "$NM_DIR"

if [[ -n "$EXTENSION_ID" ]]; then
  ORIGIN="chrome-extension://${EXTENSION_ID}/"
else
  # Placeholder until you load the unpacked extension and re-run with --extension-id=
  ORIGIN="chrome-extension://MACAGENT_EXTENSION_ID_PLACEHOLDER/"
fi

# Chrome requires an absolute path; wrap via a tiny launcher so shebang+venv work.
LAUNCHER="$ROOT/automation/native_host_launcher.sh"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$PYTHON_BIN" "$HOST_SCRIPT"
EOF
chmod +x "$LAUNCHER"

cat > "$MANIFEST_PATH" <<EOF
{
  "name": "${HOST_NAME}",
  "description": "MacAgent Chrome Native Messaging Host",
  "path": "${LAUNCHER}",
  "type": "stdio",
  "allowed_origins": [
    "${ORIGIN}"
  ]
}
EOF

echo "Wrote $MANIFEST_PATH"
echo "allowed_origins: $ORIGIN"
if [[ -z "$EXTENSION_ID" ]]; then
  echo
  echo "Next:"
  echo "  1. Chrome → chrome://extensions → Developer mode → Load unpacked"
  echo "     Select: $ROOT/automation/extension"
  echo "  2. Copy the extension ID, then re-run:"
  echo "     $0 --extension-id=<ID>"
fi

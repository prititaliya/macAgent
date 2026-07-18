#!/usr/bin/env bash
# Build (if needed) and open the MacAgent desktop app. App starts the FastAPI daemon.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/MacAgent/build/MacAgent.app"

if [[ ! -x "$APP/Contents/MacOS/MacAgent" ]]; then
  echo "Building MacAgent.app…"
  chmod +x "$ROOT/MacAgent/build.sh"
  "$ROOT/MacAgent/build.sh"
fi

open "$APP"
echo "Opened MacAgent — Dock app with Live / History / Sites / Status."
echo "FreeFlow Fn still provides speech; answers and actions appear in the app."

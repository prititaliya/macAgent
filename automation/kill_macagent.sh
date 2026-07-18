#!/usr/bin/env bash
# Quit every MacAgent.app process (leaves the Python daemon alone unless --daemon).
set -euo pipefail

killall -9 MacAgent 2>/dev/null || true
pkill -9 -f 'MacAgent\.app/Contents/MacOS/MacAgent' 2>/dev/null || true

if [[ "${1:-}" == "--daemon" ]]; then
  lsof -ti :8081 | xargs kill -9 2>/dev/null || true
  pkill -9 -f '/Projects/MacAgent/main\.py' 2>/dev/null || true
  echo "Killed MacAgent UI + daemon on :8081"
else
  echo "Killed MacAgent UI (daemon still running if present). Use --daemon to stop :8081 too."
fi

left=$(pgrep -lf 'MacAgent\.app/Contents/MacOS/MacAgent' || true)
if [[ -n "${left}" ]]; then
  echo "WARNING: still running:"
  echo "$left"
else
  echo "No MacAgent UI processes."
fi

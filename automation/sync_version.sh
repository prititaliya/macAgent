#!/usr/bin/env bash
# Sync VERSION file → Info.plist, project.yml, main.py
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid VERSION: $VERSION (expected semver like 1.0.0)" >&2
  exit 1
fi

PLIST="$ROOT/MacAgentApp/Sources/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST"

PROJECT_YML="$ROOT/MacAgentApp/project.yml"
if [[ -f "$PROJECT_YML" ]]; then
  sed -i '' "s/CFBundleShortVersionString: \"[0-9.]*\"/CFBundleShortVersionString: \"$VERSION\"/" "$PROJECT_YML"
fi

MAIN_PY="$ROOT/main.py"
sed -i '' "s/version=\"[0-9.]*\"/version=\"$VERSION\"/" "$MAIN_PY"

echo "Synced version $VERSION"

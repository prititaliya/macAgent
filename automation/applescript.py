"""Phase 4: AppleScript / open helpers for Chrome and app focus."""

from __future__ import annotations

import logging
import shlex
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def focus_app(app_name: str) -> bool:
    """Bring a native app to the foreground via osascript."""
    name = (app_name or "").replace('"', '\\"')
    script = f'tell application "{name}" to activate'
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


def _escape_applescript_string(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


def activate_chrome(url: Optional[str] = None) -> bool:
    """Open URL in Google Chrome (or just activate Chrome).

    Prefers `open -a` (no Automation permission). Falls back to AppleScript.
    """
    if url:
        # Primary: open -a is reliable and avoids repeated Automation prompts.
        result = subprocess.run(
            ["open", "-a", "Google Chrome", url],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "open -a Google Chrome failed: %s", (result.stderr or "").strip()
        )
        safe = _escape_applescript_string(url)
        script = (
            'tell application "Google Chrome"\n'
            "  activate\n"
            f'  open location "{safe}"\n'
            "end tell"
        )
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, check=False
        )
        return result.returncode == 0

    result = subprocess.run(
        ["open", "-a", "Google Chrome"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    script = 'tell application "Google Chrome" to activate'
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


def chrome_installed() -> bool:
    """True if Google Chrome.app can be resolved."""
    result = subprocess.run(
        ["mdfind", "kMDItemCFBundleIdentifier == 'com.google.Chrome'"],
        capture_output=True,
        text=True,
        check=False,
    )
    if (result.stdout or "").strip():
        return True
    from pathlib import Path

    return Path("/Applications/Google Chrome.app").exists()

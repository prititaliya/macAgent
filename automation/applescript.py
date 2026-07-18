"""Phase 4 stub: AppleScript helpers for app focus / Chrome coordination."""

import subprocess
from typing import Optional


def focus_app(app_name: str) -> bool:
    """Bring a native app to the foreground via osascript. Stub for Phase 4."""
    script = f'tell application "{app_name}" to activate'
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False
    )
    return result.returncode == 0


def activate_chrome(url: Optional[str] = None) -> bool:
    """Activate Google Chrome, optionally opening a URL. Stub for Phase 4."""
    if url:
        script = (
            'tell application "Google Chrome"\n'
            "  activate\n"
            f'  open location "{url}"\n'
            "end tell"
        )
    else:
        script = 'tell application "Google Chrome" to activate'
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False
    )
    return result.returncode == 0

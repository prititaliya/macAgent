"""Run short bash commands for MacAgent (timeout + block / confirm)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CMD_CHARS = 2000
_TIMEOUT_SEC = 20
_MAX_OUTPUT = 6000
_HOME = Path.home()

# Never allow — even with user confirmation.
_HARD_BLOCK = re.compile(
    r"(?i)("
    r"\brm\s+(-[a-zA-Z]*\s+)*/\s*($|\*)|"  # rm -rf /
    r"\brm\s+(-[a-zA-Z]*\s+)*/(bin|sbin|usr|System|etc|var|private|Applications)(/|\s|$|\*)|"
    r"\brm\s+-rf\s+~(/|\s|$)|"
    r"\bmkfs\b|\bdiskutil\s+erase|\bdd\s+if=|"
    r":\(\)\s*\{\s*:\|:&\s*\};:|"
    r"\bcurl\b.+\|\s*(ba)?sh\b|"
    r"\bwget\b.+\|\s*(ba)?sh\b|"
    r"\bsudo\b|\bsu\s|"
    r"\blaunchctl\s+bootout|\bkillall\s+Kernel|"
    r">\s*/dev/sd|"
    r"\bchmod\s+-R\s+777\s+/"
    r")"
)

# Allowed after explicit user Approve in the overlay.
_NEEDS_CONFIRM = re.compile(
    r"(?i)("
    r"\brm\b|"
    r"\brmdir\b|"
    r"\bunlink\b|"
    r"\bshred\b|"
    r"empty\s+(the\s+)?trash|"
    r"empty\s+(the\s+)?bin|"
    r"~/\.Trash|"
    r"\$HOME/\.Trash|"
    r"osascript.+\bempty\b.+\btrash\b|"
    r"\bkillall\b|"
    r"\bpkill\b|"
    r"\bmv\b.+\s+/dev/null|"
    r">\s*/dev/null\s+2>&1\s*;\s*rm\b"
    r")"
)

_EMPTY_TRASH_CMD = (
    "osascript -e 'tell application \"Finder\" to empty the trash'"
)


def empty_trash_command() -> str:
    return _EMPTY_TRASH_CMD


def summarize_command(command: str) -> str:
    cmd = (command or "").strip()
    lower = cmd.lower()
    if "empty the trash" in lower or ".trash" in lower:
        return "Empty the Trash (permanently delete everything in Bin)"
    if re.search(r"(?i)\brm\b", cmd):
        return f"Delete files via shell:\n{cmd}"
    if re.search(r"(?i)\bkillall\b|\bpkill\b", cmd):
        return f"Force-quit process(es):\n{cmd}"
    return f"Run this command:\n{cmd}"


def classify_command(command: str) -> str:
    """Return 'hard_block' | 'needs_confirm' | 'ok'."""
    cmd = (command or "").strip()
    if not cmd:
        return "ok"
    if _HARD_BLOCK.search(cmd):
        return "hard_block"
    if _NEEDS_CONFIRM.search(cmd):
        return "needs_confirm"
    return "ok"


def run_bash(
    command: str,
    timeout: float = _TIMEOUT_SEC,
    *,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Run a bash -lc command; return stdout/stderr.

    Destructive commands require confirmed=True (user approved in UI).
    Catastrophic patterns are always blocked.
    """
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "command required"}
    if len(cmd) > _MAX_CMD_CHARS:
        return {"ok": False, "error": f"command too long (max {_MAX_CMD_CHARS} chars)"}

    kind = classify_command(cmd)
    if kind == "hard_block":
        return {
            "ok": False,
            "error": "command blocked: too dangerous (system paths / privilege escalation)",
            "command": cmd[:800],
        }
    if kind == "needs_confirm" and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "command": cmd[:800],
            "summary": summarize_command(cmd),
            "error": "needs_confirm",
        }

    env = os.environ.copy()
    env["HOME"] = str(_HOME)
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(_HOME),
            env=env,
            check=False,
        )
        stdout = (proc.stdout or "")[:_MAX_OUTPUT]
        stderr = (proc.stderr or "")[:_MAX_OUTPUT]
        ok = proc.returncode == 0
        out: dict[str, Any] = {
            "ok": ok,
            "returncode": proc.returncode,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "command": cmd[:800],
            "confirmed": confirmed,
        }
        if not ok and not stdout and stderr:
            out["error"] = stderr.strip()[:500]
        return out
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s", "command": cmd[:800]}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "command": cmd[:800]}

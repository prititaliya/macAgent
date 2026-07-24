"""Run short bash commands for MacAgent (timeout + block / confirm)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Unbounded find under ~ is a common cause of shell timeouts.
_SLOW_FIND_RE = re.compile(
    r"(?i)\bfind\s+(~|\$HOME|/Users/)[^\n|;]*(-type\s+d|-iname\b)"
)
# Whole-disk / volume walks return empty or hang; always require a scoped path.
_ROOT_FIND_RE = re.compile(
    r"(?i)\bfind\s+(/Volumes(?:/\S*)?|/(?:\s|$)|/System\b)"
)
_DISCOVERY_RE = re.compile(
    r"(?i)\b(find|du|ls|stat|mdfind|locate|wc|head|tail|cat|grep|awk|sort)\b"
)


def _command_timeout(command: str) -> float:
    """Shorter timeout for simple commands; longer for multi-step shell."""
    cmd = (command or "").strip()
    if not cmd:
        return _QUICK_TIMEOUT_SEC
    if _SLOW_FIND_RE.search(cmd) and "-maxdepth" not in cmd.lower():
        return _QUICK_TIMEOUT_SEC
    if re.match(r"(?i)^(ls|open|mv|cp|echo|pwd|head|tail|stat|du)\b", cmd):
        return _QUICK_TIMEOUT_SEC
    return _TIMEOUT_SEC


_MAX_CMD_CHARS = 2000
_TIMEOUT_SEC = 45
_QUICK_TIMEOUT_SEC = 15
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
    r">\s*/dev/null\s+2>&1\s*;\s*rm\b|"
    r"\bshut\s*down\b|\brestart\b|\breboot\b|\bsleep\b|\blog\s*out\b|"
    r"osascript.+\b(shut down|restart|sleep|log out)\b|"
    r"System Events.+\b(shut down|restart|sleep|log out)\b"
    r")"
)

_EMPTY_TRASH_CMD = (
    "osascript -e 'tell application \"Finder\" to empty the trash'"
)
_SHUTDOWN_CMD = (
    "osascript -e 'tell application \"System Events\" to shut down'"
)
_RESTART_CMD = (
    "osascript -e 'tell application \"System Events\" to restart'"
)
_SLEEP_CMD = (
    "osascript -e 'tell application \"System Events\" to sleep'"
)


def empty_trash_command() -> str:
    return _EMPTY_TRASH_CMD


def shutdown_command() -> str:
    return _SHUTDOWN_CMD


def restart_command() -> str:
    return _RESTART_CMD


def sleep_command() -> str:
    return _SLEEP_CMD


def quote_paths_with_spaces(command: str) -> str:
    """Double-quote unquoted ~/… and absolute paths that contain spaces.

    Fixes LLM output like ``open ~/Downloads/My File.pdf`` which bash
    otherwise splits into multiple arguments.
    """
    cmd = command or ""
    if " " not in cmd:
        return cmd

    out: list[str] = []
    i = 0
    n = len(cmd)
    quote: Optional[str] = None

    def _at_path_start(idx: int) -> bool:
        if idx > 0 and cmd[idx - 1] not in " \t\n;|&(=":
            return False
        rest = cmd[idx:]
        if rest.startswith("~/") or rest.startswith("$HOME/"):
            return True
        if rest.startswith("/"):
            # Absolute path (not a lone slash / flag).
            return bool(
                re.match(
                    r"/(?:Users|Volumes|tmp|private|var|Applications|System|"
                    r"Library|opt|usr)/",
                    rest,
                )
                or re.match(r"/[^/\s][^\s]*\s", rest)
            )
        return False

    def _path_end(idx: int) -> int:
        """Scan from path start; include spaces until a shell metachar boundary."""
        j = idx
        while j < n:
            ch = cmd[j]
            if ch in "|&;<>(){}\n":
                break
            # Redirections / next flag after a space: " 2>" " >/dev" " -"
            if ch in " \t":
                k = j + 1
                while k < n and cmd[k] in " \t":
                    k += 1
                if k >= n:
                    break
                nxt = cmd[k:]
                if nxt.startswith("2>") or nxt.startswith(">&") or nxt[0] in "<>|&;":
                    break
                # Space then a new absolute/tilde path → end current path.
                if _at_path_start(k):
                    break
                # Space then an option flag (e.g. " -l") after the path.
                if nxt.startswith("-") and (k + 1 >= n or not nxt[1].isdigit()):
                    # Allow "file - copy.pdf" style names; only stop for
                    # common short flags after whitespace.
                    if re.match(r"-[a-zA-Z]{1,3}(?:\s|$)", nxt):
                        break
            j += 1
        return j

    while i < n:
        ch = cmd[i]
        if quote:
            out.append(ch)
            if ch == quote and (i == 0 or cmd[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if _at_path_start(i):
            end = _path_end(i)
            path = cmd[i:end]
            if " " in path and not (path.startswith('"') or path.startswith("'")):
                escaped = path.replace("\\", "\\\\").replace('"', '\\"')
                out.append(f'"{escaped}"')
            else:
                out.append(path)
            i = end
            continue
        out.append(ch)
        i += 1

    return "".join(out)


def summarize_command(command: str) -> str:
    cmd = (command or "").strip()
    lower = cmd.lower()
    if "empty the trash" in lower or ".trash" in lower:
        return "Empty the Trash (permanently delete everything in Bin)"
    if "shut down" in lower:
        return "Shut down this Mac"
    if "restart" in lower or "reboot" in lower:
        return "Restart this Mac"
    if re.search(r"(?i)\bsleep\b", lower) and "osascript" in lower:
        return "Put this Mac to sleep"
    if "log out" in lower:
        return "Log out of this Mac"
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
    timeout: float | None = None,
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
    # LLM often emits unquoted paths with spaces — fix before bash splits them.
    cmd = quote_paths_with_spaces(cmd)
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
    effective_timeout = timeout if timeout is not None else _command_timeout(cmd)
    if _SLOW_FIND_RE.search(cmd) and "-maxdepth" not in cmd.lower():
        return {
            "ok": False,
            "error": (
                "command blocked: unbounded find under ~ is too slow — "
                "add -maxdepth (e.g. find ~/Documents -maxdepth 4 -iname '*name*')"
            ),
            "command": cmd[:800],
        }
    if _ROOT_FIND_RE.search(cmd) and "-maxdepth" not in cmd.lower():
        return {
            "ok": False,
            "error": (
                "command blocked: scanning / or /Volumes without -maxdepth is "
                "too slow/empty — search under $HOME with -maxdepth instead"
            ),
            "command": cmd[:800],
        }
    try:
        # pipefail so `find … | sort | head` fails when find dies mid-pipeline.
        proc = subprocess.run(
            ["/bin/bash", "-lc", f"set -o pipefail; {cmd}"],
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            cwd=str(_HOME),
            env=env,
            check=False,
        )
        stdout = (proc.stdout or "")[:_MAX_OUTPUT]
        stderr = (proc.stderr or "")[:_MAX_OUTPUT]
        ok = proc.returncode == 0
        # `… | sort | head -n N` often exits 141 (SIGPIPE) once head closes the pipe.
        # That still means we got the lines we asked for.
        if (not ok) and stdout.strip() and proc.returncode in (141, 128 + 13):
            ok = True
        # Pipelines can exit 0 even when find printed a fatal error to stderr.
        if ok and not stdout.strip() and re.search(
            r"(?i)unknown primary|illegal option|invalid|not found",
            stderr or "",
        ):
            ok = False
        # Discovery with no stdout is not success — callers must not say "Done".
        if ok and not stdout.strip() and _DISCOVERY_RE.search(cmd):
            ok = False
            if not (stderr or "").strip():
                stderr = "no output (nothing matched or scan produced empty results)"
        out: dict[str, Any] = {
            "ok": ok,
            "returncode": proc.returncode,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "command": cmd[:800],
            "confirmed": confirmed,
        }
        if not ok and not stdout.strip():
            err = (stderr or "").strip()
            if err:
                out["error"] = err[:500]
            elif proc.returncode != 0:
                out["error"] = f"exit {proc.returncode}"
        return out
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"timed out after {effective_timeout}s",
            "command": cmd[:800],
        }
    except OSError as exc:
        return {"ok": False, "error": str(exc), "command": cmd[:800]}

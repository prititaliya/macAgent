"""Run short Python snippets for MacAgent (timeout + blocked imports)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CODE_CHARS = 8000
_TIMEOUT_SEC = 8
_MAX_OUTPUT = 4000

# Soft blocklist — not a sandbox; personal agent on local Mac.
_BLOCKED = re.compile(
    r"(?i)\b("
    r"subprocess|os\.system|os\.popen|pty\.|socket\.|urllib|requests|"
    r"http\.client|ftplib|paramiko|ctypes|multiprocessing|"
    r"shutil\.rmtree|"
    r"open\([^)]*['\"]\/etc"
    r")\b"
)


def run_python(code: str, timeout: float = _TIMEOUT_SEC) -> dict[str, Any]:
    """Execute Python code in a temp file; return stdout/stderr."""
    source = (code or "").strip()
    if not source:
        return {"ok": False, "error": "code required"}
    if len(source) > _MAX_CODE_CHARS:
        return {"ok": False, "error": f"code too long (max {_MAX_CODE_CHARS} chars)"}
    if _BLOCKED.search(source):
        return {
            "ok": False,
            "error": "code blocked: uses disallowed network/system APIs",
        }

    # Ensure something prints if the model only left an expression.
    if "print(" not in source and "\n" not in source:
        source = f"print({source})"

    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="macagent_",
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write(source)
            tmp = Path(fh.name)

        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        proc = subprocess.run(
            [sys.executable, str(tmp)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=tempfile.gettempdir(),
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
            "code": source[:1500],
        }
        if not ok and not stdout and stderr:
            out["error"] = stderr.strip()[:500]
        return out
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

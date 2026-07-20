"""High-performance file search via macOS Spotlight (mdfind)."""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

_MAX_RESULTS = 15
_TIMEOUT = 5


class SpotlightFileSearchArgs(BaseModel):
    """Arguments for a native Spotlight query returning absolute file paths."""

    query: str = Field(
        ...,
        description=(
            "Spotlight query passed to mdfind, e.g. 'invoice.pdf', "
            "'kind:pdf last_name', or a full Spotlight expression"
        ),
    )

    @field_validator("query")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("query must be a non-empty string")
        return s


def spotlight_file_search(query: str) -> dict[str, Any]:
    """Run `mdfind {query}` and return up to 15 absolute matching paths."""
    try:
        args = SpotlightFileSearchArgs(query=query)
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    cmd = ["mdfind", args.query]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"mdfind timed out after {_TIMEOUT}s",
            "query": args.query,
            "count": 0,
            "paths": [],
        }
    except OSError as exc:
        logger.warning("mdfind failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "query": args.query,
            "count": 0,
            "paths": [],
        }

    paths: list[str] = []
    for line in (proc.stdout or "").splitlines():
        path = line.strip()
        if not path:
            continue
        # Absolute paths only (mdfind normally returns these).
        if not path.startswith("/"):
            continue
        paths.append(path)
        if len(paths) >= _MAX_RESULTS:
            break

    stderr = (proc.stderr or "").strip()
    # mdfind can return 0 with empty results; treat non-zero as soft failure
    # only when we also got no paths.
    if proc.returncode != 0 and not paths:
        return {
            "ok": False,
            "error": stderr or f"mdfind exited {proc.returncode}",
            "query": args.query,
            "count": 0,
            "paths": [],
            "returncode": proc.returncode,
        }

    return {
        "ok": True,
        "query": args.query,
        "count": len(paths),
        "paths": paths,
        "returncode": proc.returncode,
        "stderr": stderr,
    }


def spotlight_file_search_from_args(payload: dict[str, Any]) -> dict[str, Any]:
    """Registry adapter: accept a free-form args dict."""
    return spotlight_file_search(
        query=str(payload.get("query") or payload.get("q") or payload.get("search") or "")
    )

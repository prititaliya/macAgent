"""In-memory pending destructive actions awaiting user approval."""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Optional

_lock = threading.Lock()
_ids = itertools.count(1)
_pending: dict[str, dict[str, Any]] = {}


def create_pending(
    *,
    utterance: str,
    summary: str,
    command: str,
    tool: str = "run_bash",
) -> dict[str, Any]:
    action_id = str(next(_ids))
    item = {
        "id": action_id,
        "utterance": utterance or "",
        "summary": summary or "Destructive action",
        "command": command or "",
        "tool": tool,
        "created_at": time.time(),
    }
    with _lock:
        # Keep only the latest few.
        if len(_pending) > 20:
            oldest = sorted(_pending.values(), key=lambda x: x["created_at"])[:10]
            for o in oldest:
                _pending.pop(str(o["id"]), None)
        _pending[action_id] = item
    return item


def get_pending(action_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        return dict(_pending[action_id]) if action_id in _pending else None


def take_pending(action_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        return _pending.pop(action_id, None)


def clear_all() -> None:
    with _lock:
        _pending.clear()

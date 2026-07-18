"""In-memory debug traces: raw prompts, intent JSON, model I/O."""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional


class DebugTraceStore:
    def __init__(self, maxlen: int = 50) -> None:
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._current: Dict[int, Dict[str, Any]] = {}

    def start(self, utterance: str, source: str = "ask") -> int:
        tid = next(self._ids)
        trace: Dict[str, Any] = {
            "id": tid,
            "ts": time.time(),
            "utterance": utterance or "",
            "source": source,
            "steps": [],
            "status": "running",
            "result": None,
        }
        with self._lock:
            self._current[tid] = trace
            self._buffer.append(trace)
        return tid

    def step(
        self,
        trace_id: Optional[int],
        name: str,
        **payload: Any,
    ) -> None:
        if trace_id is None:
            return
        entry = {
            "ts": time.time(),
            "name": name,
            **{k: _clip(v) for k, v in payload.items()},
        }
        with self._lock:
            trace = self._current.get(trace_id)
            if not trace:
                for t in self._buffer:
                    if t["id"] == trace_id:
                        trace = t
                        break
            if trace is None:
                return
            trace["steps"].append(entry)

    def finish(
        self,
        trace_id: Optional[int],
        *,
        status: str = "ok",
        result: Any = None,
    ) -> None:
        if trace_id is None:
            return
        with self._lock:
            trace = self._current.pop(trace_id, None)
            if trace is None:
                for t in self._buffer:
                    if t["id"] == trace_id:
                        trace = t
                        break
            if trace is None:
                return
            trace["status"] = status
            trace["result"] = _clip(result)
            trace["finished_ts"] = time.time()

    def latest(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._buffer)
        if limit <= 0:
            return items
        return items[-limit:]

    def get(self, trace_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            for t in self._buffer:
                if t["id"] == trace_id:
                    return t
        return None


def _clip(value: Any, max_chars: int = 12000) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _clip(v, max_chars=max(500, max_chars // 2)) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, max_chars=max(500, max_chars // 2)) for v in value[:40]]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


# Thread-local active trace for nested LLM calls without plumbing ids everywhere.
_tls = threading.local()
debug_traces = DebugTraceStore()


def current_trace_id() -> Optional[int]:
    return getattr(_tls, "trace_id", None)


def set_current_trace_id(trace_id: Optional[int]) -> None:
    _tls.trace_id = trace_id


def trace_step(name: str, **payload: Any) -> None:
    debug_traces.step(current_trace_id(), name, **payload)

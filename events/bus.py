"""In-memory event ring buffer + fan-out for the SwiftUI HUD (SSE)."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from typing import Any, AsyncIterator, Deque, Dict, List, Optional


class EventBus:
    def __init__(self, maxlen: int = 100) -> None:
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._subscribers: List[asyncio.Queue] = []
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    def publish(
        self,
        *,
        utterance: str,
        kind: str,
        text: str,
        detail: str = "",
        sources: Optional[List[Any]] = None,
        step: Optional[str] = None,
        tool: Optional[str] = None,
        tool_input: Any = None,
        tool_output: Any = None,
        backend: Optional[str] = None,
    ) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "id": next(self._ids),
            "ts": time.time(),
            "utterance": utterance or "",
            "kind": kind,
            "text": text or "",
            "detail": detail or "",
        }
        if sources:
            event["sources"] = sources
        if step:
            event["step"] = step
        if tool:
            event["tool"] = tool
        if tool_input is not None:
            event["tool_input"] = tool_input
        if tool_output is not None:
            event["tool_output"] = tool_output
        if backend:
            event["backend"] = backend
        self._buffer.append(event)
        dead: List[asyncio.Queue] = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001
                dead.append(queue)
        for queue in dead:
            if queue in self._subscribers:
                self._subscribers.remove(queue)
        return event

    def latest(self, limit: int = 20) -> List[Dict[str, Any]]:
        items = list(self._buffer)
        if limit <= 0:
            return items
        return items[-limit:]

    def get(self, event_id: int) -> Optional[Dict[str, Any]]:
        for event in self._buffer:
            if event["id"] == event_id:
                return event
        return None

    async def subscribe(self, after_id: int = 0) -> AsyncIterator[Dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.append(queue)
        try:
            for event in list(self._buffer):
                if event["id"] > after_id:
                    yield event
            while True:
                event = await queue.get()
                yield event
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)


event_bus = EventBus()

"""macOS UI control via MacAgent.app bridge (Accessibility TCC on the app).

Python's `osascript` is a *different* binary — enabling MacAgent.app in
Accessibility does NOT unlock daemon-side osascript. We call the local UI
bridge inside MacAgent.app (127.0.0.1:8082) which runs NSAppleScript in-process.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_BRIDGE = "http://127.0.0.1:8082/"
_TIMEOUT = 20.0

_ACCESS_MSG = (
    "MacAgent needs Accessibility to control the screen. "
    "You already may have enabled it — keep MacAgent.app ON in "
    "System Settings → Privacy & Security → Accessibility, "
    "and keep the MacAgent app running (not just the daemon). "
    "AEServer is unrelated; leave it alone."
)

_BRIDGE_DOWN = (
    "UI bridge is offline. Open/restart the MacAgent app (menu bar sparkles) "
    "so screen control can use its Accessibility permission. "
    "Enabling MacAgent.app in Accessibility is correct — AEServer is not needed."
)


def _bridge(op: str, **kwargs: Any) -> dict[str, Any]:
    payload = {"op": op, **kwargs}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _BRIDGE,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if isinstance(data, dict):
                return data
            return {"ok": False, "error": "bad bridge response"}
    except urllib.error.URLError as exc:
        logger.warning("UI bridge unreachable: %s", exc)
        return {"ok": False, "error": _BRIDGE_DOWN, "bridge_down": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("UI bridge error: %s", exc)
        return {"ok": False, "error": str(exc)}


def accessibility_trusted() -> bool:
    ping = _bridge("ping")
    if ping.get("bridge_down"):
        return False
    return bool(ping.get("ok") and ping.get("trusted"))


def ui_snapshot(max_elements: int = 40) -> dict[str, Any]:
    out = _bridge("snapshot", limit=int(max_elements))
    if out.get("bridge_down"):
        return out
    if not out.get("ok") and "Accessibility" in str(out.get("error") or ""):
        out["error"] = _ACCESS_MSG
    return out


def ui_click(name: str = "", role: str = "button", index: int = 1) -> dict[str, Any]:
    return _bridge(
        "click",
        name=name or "",
        role=role or "button",
        index=int(index or 1),
    )


def ui_type(text: str) -> dict[str, Any]:
    return _bridge("type", text=text or "")


def ui_key(key: str = "return", modifiers: str = "") -> dict[str, Any]:
    return _bridge("key", key=key or "return", modifiers=modifiers or "")


def ui_menu(app: str, menu_path: str) -> dict[str, Any]:
    return _bridge("menu", app=app or "", menu_path=menu_path or "")

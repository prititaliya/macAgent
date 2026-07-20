"""Native macOS Notification Center alerts via AppleScript / osascript."""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

_TIMEOUT = 10
_SOUND_NAME = "Glass"


class TriggerNativeNotificationArgs(BaseModel):
    """Arguments for a background Notification Center banner."""

    title: str = Field(..., description="Notification title shown in Notification Center")
    subtitle: str = Field(
        "",
        description="Optional subtitle line under the title",
    )
    message: str = Field(
        ...,
        description="Main notification body text",
        alias="message",
    )
    play_sound: bool = Field(
        False,
        description="If true, play the Glass system sound with the notification",
    )

    model_config = {"populate_by_name": True}

    @field_validator("title", "message")
    @classmethod
    def _required_text(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("must be a non-empty string")
        return s

    @field_validator("subtitle")
    @classmethod
    def _optional_text(cls, v: str) -> str:
        return (v or "").strip()


def _escape_applescript(text: str) -> str:
    """Escape backslashes and double-quotes for an AppleScript string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def trigger_native_notification(
    title: str,
    subtitle: str,
    message: str,
    play_sound: bool,
) -> dict[str, Any]:
    """Fire a Notification Center alert via `osascript` display notification."""
    try:
        args = TriggerNativeNotificationArgs(
            title=title,
            subtitle=subtitle or "",
            message=message,
            play_sound=bool(play_sound),
        )
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    esc_title = _escape_applescript(args.title)
    esc_subtitle = _escape_applescript(args.subtitle)
    esc_message = _escape_applescript(args.message)

    script = (
        f'display notification "{esc_message}" '
        f'with title "{esc_title}"'
    )
    if esc_subtitle:
        script += f' subtitle "{esc_subtitle}"'
    if args.play_sound:
        script += f' sound name "{_SOUND_NAME}"'

    cmd = ["osascript", "-e", script]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("osascript notification failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "title": args.title,
            "subtitle": args.subtitle,
            "message": args.message,
        }

    ok = proc.returncode == 0
    out: dict[str, Any] = {
        "ok": ok,
        "title": args.title,
        "subtitle": args.subtitle,
        "message": args.message,
        "play_sound": args.play_sound,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }
    if not ok:
        out["error"] = out["stderr"] or f"osascript exited {proc.returncode}"
    return out


def trigger_native_notification_from_args(payload: dict[str, Any]) -> dict[str, Any]:
    """Registry adapter: accept a free-form args dict."""
    play = payload.get("play_sound")
    if play is None:
        play = payload.get("sound") or False
    if isinstance(play, str):
        play = play.strip().lower() in {"1", "true", "yes", "on"}
    return trigger_native_notification(
        title=str(payload.get("title") or ""),
        subtitle=str(payload.get("subtitle") or ""),
        message=str(payload.get("message") or payload.get("body") or payload.get("text") or ""),
        play_sound=bool(play),
    )

"""Runtime + user-editable context injected into LLM prompts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONTEXT_PATH = _PROJECT_ROOT / "config" / "user_context.txt"
_MAX_NOTES = 4000


def context_path() -> Path:
    return _CONTEXT_PATH


def load_user_notes() -> str:
    if not _CONTEXT_PATH.exists():
        return ""
    try:
        return _CONTEXT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_user_notes_for_llm() -> str:
    """Notes with # comment lines removed (template hints stay in the file)."""
    lines = []
    for ln in load_user_notes().splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(ln.rstrip())
    return "\n".join(lines).strip()


def save_user_notes(text: str) -> str:
    _CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    cleaned = (text or "").strip()
    if len(cleaned) > _MAX_NOTES:
        cleaned = cleaned[:_MAX_NOTES]
    _CONTEXT_PATH.write_text(cleaned + ("\n" if cleaned else ""), encoding="utf-8")
    return cleaned


def build_runtime_context() -> str:
    """Clock + optional user notes for the small local model."""
    now = datetime.now().astimezone()
    parts = [
        f"Current local datetime: {now.strftime('%A, %B %d, %Y %-I:%M %p %Z')}",
        f"ISO timestamp: {now.isoformat(timespec='minutes')}",
    ]
    notes = load_user_notes_for_llm()
    if notes:
        parts.append("User profile / preferences (from Settings):")
        parts.append(notes[:_MAX_NOTES])
    return "\n".join(parts)


def context_payload() -> dict[str, Any]:
    return {
        "notes": load_user_notes(),
        "runtime_preview": build_runtime_context(),
        "path": str(_CONTEXT_PATH),
        "max_chars": _MAX_NOTES,
    }

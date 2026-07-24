"""Runtime + user-editable context injected into LLM prompts."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONTEXT_PATH = _PROJECT_ROOT / "config" / "user_context.txt"
_MAX_NOTES = 4000
_MAX_TURN_USER = 900
_MAX_TURN_ASSISTANT = 1800
_MAX_PRIOR_TURNS = 6

# Per-request follow-up turns from the overlay (this conversation).
_prior_turns_var: ContextVar[Optional[list[dict[str, str]]]] = ContextVar(
    "macagent_prior_turns", default=None
)


def set_prior_turns(turns: Optional[list[dict[str, str]]]) -> None:
    """Attach prior Q&A turns for the current ask (follow-up mode)."""
    if not turns:
        _prior_turns_var.set(None)
        return
    cleaned: list[dict[str, str]] = []
    for t in turns[-_MAX_PRIOR_TURNS:]:
        if not isinstance(t, dict):
            continue
        user = str(t.get("user") or t.get("utterance") or "").strip()
        assistant = str(t.get("assistant") or t.get("answer") or "").strip()
        if user or assistant:
            cleaned.append({"user": user, "assistant": assistant})
    _prior_turns_var.set(cleaned or None)


def get_prior_turns() -> list[dict[str, str]]:
    return list(_prior_turns_var.get() or [])


def clear_prior_turns() -> None:
    _prior_turns_var.set(None)


# Cloud → local handoff guidance for the current ask.
_cloud_handoff_var: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "macagent_cloud_handoff", default=None
)


def set_cloud_handoff(payload: Optional[dict[str, Any]]) -> None:
    if not payload:
        _cloud_handoff_var.set(None)
        return
    _cloud_handoff_var.set(dict(payload))


def get_cloud_handoff() -> Optional[dict[str, Any]]:
    return _cloud_handoff_var.get()


def clear_cloud_handoff() -> None:
    _cloud_handoff_var.set(None)


def prior_turns_as_messages(
    *,
    max_turns: int = 4,
    max_user: int = 700,
    max_assistant: int = 1400,
) -> list[dict[str, str]]:
    """Prior Q&A as ChatML user/assistant pairs (best for small local models)."""
    messages: list[dict[str, str]] = []
    for t in get_prior_turns()[-max_turns:]:
        user = (t.get("user") or "").strip()
        assistant = (t.get("assistant") or "").strip()
        if user:
            messages.append({"role": "user", "content": user[:max_user]})
        if assistant:
            messages.append({"role": "assistant", "content": assistant[:max_assistant]})
    return messages


def format_followup_block(turns: Optional[list[dict[str, str]]] = None) -> str:
    """Compact text block (for cloud single-string prompts). Prefer chat messages locally."""
    items = turns if turns is not None else get_prior_turns()
    if not items:
        return ""
    # Only the last 3 turns — small models drown in long threads.
    items = items[-3:]
    parts = [
        "FOLLOW-UP THREAD (continue this; resolve it/that/this from the last Assistant reply):"
    ]
    for i, t in enumerate(items, 1):
        user = (t.get("user") or "")[:_MAX_TURN_USER]
        assistant = (t.get("assistant") or "")[:_MAX_TURN_ASSISTANT]
        parts.append(f"User {i}: {user}")
        parts.append(f"Assistant {i}: {assistant}")
    return "\n".join(parts)


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


def build_runtime_context(
    memory: Optional[Any] = None,
    *,
    include_followup_text: bool = True,
) -> str:
    """Clock + notes + optional follow-up text + recent interactions.

    When a live follow-up thread exists, notes/activity are truncated so the
    small local model focuses on the conversation (chat messages carry the turns).
    """
    now = datetime.now().astimezone()
    parts = [
        f"Current local datetime: {now.strftime('%A, %B %d, %Y %-I:%M %p %Z')}",
        f"ISO timestamp: {now.isoformat(timespec='minutes')}",
    ]
    follow = format_followup_block() if include_followup_text else ""
    has_follow = bool(get_prior_turns())

    notes = load_user_notes_for_llm()
    if notes:
        # Follow-ups: keep notes tiny so they don't bury the thread.
        cap = 500 if has_follow else _MAX_NOTES
        parts.append("User profile / preferences (from Settings):")
        parts.append(notes[:cap])

    if follow:
        parts.append(follow)
    elif has_follow:
        parts.append(
            "Follow-up mode: prior User/Assistant turns are in the chat history. "
            "Resolve pronouns from the latest Assistant reply."
        )

    handoff = get_cloud_handoff()
    if handoff:
        parts.append(
            "CLOUD HANDOFF PLAN (execute on this Mac — do not invent unrelated tools):"
        )
        guidance = str(handoff.get("guidance") or "").strip()
        if guidance:
            parts.append(f"Guidance: {guidance[:2500]}")
        cmds = handoff.get("commands") or []
        if cmds:
            parts.append("Suggested commands:")
            for c in cmds[:8]:
                parts.append(f"- {c}")

    try:
        if memory is None:
            from memory.sqlite import ContextMemory

            memory = ContextMemory()
        if not has_follow:
            recent = memory.recent_interactions(limit=5)
            if recent:
                parts.append("Recent interactions (may inform follow-up tasks):")
                for row in recent:
                    u = str(row.get("utterance") or "")[:120]
                    a = str(row.get("answer") or "")[:120]
                    when = str(row.get("created_at") or "")[:16]
                    parts.append(f"- [{when}] User: {u} → Agent: {a}")
    except Exception:
        pass
    return "\n".join(parts)


def context_payload() -> dict[str, Any]:
    return {
        "notes": load_user_notes(),
        "runtime_preview": build_runtime_context(),
        "path": str(_CONTEXT_PATH),
    }

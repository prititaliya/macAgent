"""Natural-language spoken status phrases for agent phases."""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

from tools.tts_kokoro import is_muted, speak_answer, speak_status, tts_config

_last_phrase: dict[str, str] = {}
_last_phase_at: float = 0.0
_phase_lock = threading.Lock()
_MIN_STATUS_GAP_SEC = 1.4

_PHRASES: dict[str, list[str]] = {
    "thinking": [
        "Thinking about your question.",
        "Looking at what you asked.",
        "Taking a moment to understand that.",
        "Considering your request.",
        "Let me think that through.",
    ],
    "planning": [
        "Figuring out the next step.",
        "Planning how to handle this.",
        "Deciding the best approach.",
        "Mapping out what to do.",
        "Working out a plan.",
    ],
    "researching": [
        "Checking sources for you.",
        "Looking that up.",
        "Searching for useful information.",
        "Reading through some results.",
        "Gathering what I can find.",
    ],
    "acting": [
        "Working on that now.",
        "Taking care of that.",
        "Getting that done.",
        "Carrying out the next action.",
        "On it.",
    ],
}


def _pick(phase: str) -> str:
    pool = _PHRASES.get(phase) or _PHRASES["acting"]
    last = _last_phrase.get(phase)
    choices = [p for p in pool if p != last] or pool
    phrase = random.choice(choices)
    _last_phrase[phase] = phrase
    return phrase


def narrate(phase: str, *, force: bool = False) -> Optional[str]:
    """Speak a varied status line for this phase. Returns the phrase used."""
    cfg = tts_config()
    if not cfg.get("enabled") or is_muted() or not cfg.get("speak_status"):
        return None
    global _last_phase_at
    phase_key = (phase or "acting").strip().lower()
    if phase_key not in _PHRASES:
        phase_key = "acting"
    with _phase_lock:
        now = time.monotonic()
        if not force and (now - _last_phase_at) < _MIN_STATUS_GAP_SEC:
            return None
        _last_phase_at = now
        phrase = _pick(phase_key)
    speak_status(phrase)
    return phrase


def narrate_answer(text: str) -> None:
    """Interrupt status speech and read the final answer aloud."""
    if is_muted():
        return
    speak_answer(text or "")

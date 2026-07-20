"""Kokoro-82M text-to-speech — lazy load + interruptible playback."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SETTINGS_PATH = _PROJECT_ROOT / "config" / "settings.json"

_SAMPLE_RATE = 24000
_lock = threading.RLock()
_pipeline = None
_pipeline_lang: Optional[str] = None
_play_thread: Optional[threading.Thread] = None
_afplay_proc: Optional[subprocess.Popen] = None
_cancel = threading.Event()
_dictating = False


def set_dictating(active: bool) -> None:
    """Pause TTS while the overlay mic is recording."""
    global _dictating
    _dictating = bool(active)
    if _dictating:
        interrupt()


def is_dictating() -> bool:
    return _dictating


def _load_settings() -> dict[str, Any]:
    if _SETTINGS_PATH.exists():
        try:
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("tts settings read failed: %s", exc)
    return {}


def tts_config() -> dict[str, Any]:
    s = _load_settings()
    vol = s.get("tts_volume", 0.95)
    try:
        vol = float(vol)
    except (TypeError, ValueError):
        vol = 0.95
    vol = max(0.0, min(1.5, vol))
    speed = s.get("tts_speed", 1.0)
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(2.0, speed))
    return {
        "enabled": bool(s.get("tts_enabled", True)),
        "voice": str(s.get("tts_voice") or "af_heart"),
        "lang": str(s.get("tts_lang") or "a"),
        "speed": speed,
        "volume": vol,
        "speak_status": bool(s.get("tts_speak_status", True)),
        "speak_answer": bool(s.get("tts_speak_answer", True)),
    }


def interrupt() -> None:
    """Stop any in-flight playback as soon as possible."""
    global _afplay_proc
    _cancel.set()
    proc = _afplay_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def _ensure_pipeline(lang: str):
    global _pipeline, _pipeline_lang
    with _lock:
        if _pipeline is not None and _pipeline_lang == lang:
            return _pipeline
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        from kokoro import KPipeline  # lazy: heavy import

        logger.info("Loading Kokoro TTS pipeline lang=%s", lang)
        _pipeline = KPipeline(lang_code=lang)
        _pipeline_lang = lang
        return _pipeline


def _apply_gain(audio: np.ndarray, volume: float) -> np.ndarray:
    out = np.asarray(audio, dtype=np.float32) * float(volume)
    return np.clip(out, -1.0, 1.0)


def _concat_chunks(chunks: list[np.ndarray], volume: float) -> np.ndarray:
    parts = [_apply_gain(c, volume) for c in chunks if c is not None and np.asarray(c).size]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32)


def _play_via_afplay(audio: np.ndarray) -> None:
    """Write a temp WAV and play with macOS afplay (reliable for daemons)."""
    global _afplay_proc
    try:
        import soundfile as sf
    except ImportError:
        logger.warning("soundfile not installed — TTS playback skipped")
        return

    path: Optional[str] = None
    try:
        fd, path = tempfile.mkstemp(prefix="macagent-tts-", suffix=".wav")
        os.close(fd)
        sf.write(path, audio, _SAMPLE_RATE)
        # afplay volume is 0..255 relative scale via -v (float multiplier, typically 0..1+)
        # We already applied gain to PCM; play at unity.
        proc = subprocess.Popen(
            ["/usr/bin/afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _afplay_proc = proc
        while proc.poll() is None:
            if _cancel.is_set():
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                break
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("TTS afplay failed: %s", exc)
    finally:
        _afplay_proc = None
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _play_via_sounddevice(audio: np.ndarray) -> bool:
    try:
        import sounddevice as sd
    except ImportError:
        return False
    try:
        sd.play(audio.reshape(-1, 1), samplerate=_SAMPLE_RATE, blocking=False)
        # Poll so we can interrupt.
        while sd.get_stream().active:  # type: ignore[union-attr]
            if _cancel.is_set():
                sd.stop()
                break
            threading.Event().wait(0.05)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("sounddevice playback unavailable (%s) — using afplay", exc)
        return False


def _play_audio(audio: np.ndarray) -> None:
    if audio.size == 0 or _cancel.is_set():
        return
    # Prefer afplay on macOS — PortAudio often fails for background daemons.
    if sys_platform_is_darwin():
        _play_via_afplay(audio)
        return
    if not _play_via_sounddevice(audio):
        _play_via_afplay(audio)


def sys_platform_is_darwin() -> bool:
    return os.uname().sysname == "Darwin"


def _synthesize(text: str, cfg: dict[str, Any]) -> list[np.ndarray]:
    pipeline = _ensure_pipeline(cfg["lang"])
    chunks: list[np.ndarray] = []
    generator = pipeline(
        text,
        voice=cfg["voice"],
        speed=cfg["speed"],
        split_pattern=r"\n+",
    )
    for _gs, _ps, audio in generator:
        if _cancel.is_set():
            break
        if audio is None:
            continue
        arr = np.asarray(audio, dtype=np.float32)
        if arr.size:
            chunks.append(arr)
    return chunks


def _speak_sync(text: str, cfg: dict[str, Any]) -> None:
    cleaned = (text or "").strip()
    if not cleaned:
        return
    # Soft length cap for answers — keep speech responsive.
    if len(cleaned) > 2500:
        cleaned = cleaned[:2500].rsplit(" ", 1)[0] + "…"
    try:
        logger.info("TTS synthesizing %d chars", len(cleaned))
        chunks = _synthesize(cleaned, cfg)
        if not chunks:
            logger.warning("TTS produced no audio for %r", cleaned[:80])
            return
        if _cancel.is_set():
            return
        audio = _concat_chunks(chunks, cfg["volume"])
        _play_audio(audio)
        logger.info("TTS playback finished (%d samples)", int(audio.size))
    except Exception as exc:  # noqa: BLE001
        logger.warning("TTS speak failed: %s", exc, exc_info=True)


def speak(text: str, *, interrupt_current: bool = True) -> None:
    """Synthesize and play `text` on a background thread."""
    cfg = tts_config()
    if not cfg["enabled"]:
        logger.debug("TTS skipped — disabled in settings")
        return
    if _dictating:
        logger.info("TTS skipped — mic dictation active")
        return
    cleaned = (text or "").strip()
    if not cleaned:
        return

    global _play_thread
    if interrupt_current:
        interrupt()
        prev = _play_thread
        if prev is not None and prev.is_alive() and prev is not threading.current_thread():
            prev.join(timeout=0.4)

    _cancel.clear()

    def _run() -> None:
        _speak_sync(cleaned, cfg)

    t = threading.Thread(target=_run, name="kokoro-tts", daemon=True)
    _play_thread = t
    t.start()


def speak_status(text: str) -> None:
    cfg = tts_config()
    if not cfg["enabled"] or not cfg["speak_status"]:
        return
    speak(text, interrupt_current=True)


def speak_answer(text: str) -> None:
    cfg = tts_config()
    if not cfg["enabled"] or not cfg["speak_answer"]:
        return
    speak(text, interrupt_current=True)

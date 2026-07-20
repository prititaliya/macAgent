import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from events.bus import event_bus
from events.debug_trace import debug_traces, set_current_trace_id, trace_step
from llm.inference import LocalIntentParser, needs_browser
from memory.history_harvest import harvest_chrome_history
from memory.sqlite import ContextMemory
from memory.user_context import context_payload, save_user_notes
from planner.router import CoreRouter
from tools.agent_loop import AgentLoop
from tools.duckduckgo import build_grounded_context
from tools.pending_actions import take_pending
from tools.registry import ToolRegistry
from tools.run_bash import run_bash
from tools.tts_kokoro import set_dictating, tts_config
from tools.tts_narrator import narrate, narrate_answer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("macagent")

_PROJECT_ROOT = Path(__file__).resolve().parent
_SETTINGS_PATH = _PROJECT_ROOT / "config" / "settings.json"

_DEDUPE_WINDOW_SEC = 30.0
_last_executed: tuple[str, float] = ("", 0.0)
_in_flight: set[str] = set()

_RAW_TRANSCRIPTION_RE = re.compile(
    r"<<<RAW_TRANSCRIPTION\s*(.*?)\s*(?:RAW_TRANSCRIPTION|>>>|\Z)",
    re.DOTALL | re.IGNORECASE,
)

_QUESTION_RE = re.compile(
    r"(?i)^\s*(what|why|how|who|when|where|which|whose|whom|is|are|was|were|"
    r"do|does|did|can|could|would|should|will|am|tell me|explain|define|"
    r"is there|are there)\b|[?]\s*$"
)


def _load_settings() -> dict:
    if _SETTINGS_PATH.exists():
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_settings(data: dict[str, Any]) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _model_dir() -> Path:
    raw = settings.get("model_dir") or ""
    if not raw:
        # Default: parent of configured model_path, else ~/Models
        mp = settings.get("model_path") or "~/Models/qwen2.5-3b-instruct-q4_k_m.gguf"
        parent = Path(mp).expanduser().parent
        if parent.is_dir():
            return parent
        return Path("~/Models").expanduser()
    return Path(raw).expanduser()


def _list_gguf_models(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.gguf")):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": size,
                "size_gb": round(size / (1024**3), 2),
            }
        )
    return items


def _models_payload() -> dict[str, Any]:
    model_path = Path(
        settings.get("model_path", "~/Models/qwen2.5-3b-instruct-q4_k_m.gguf")
    ).expanduser()
    directory = _model_dir()
    models = _list_gguf_models(directory)
    # Include current path even if it lives outside model_dir.
    if model_path.exists() and model_path.suffix.lower() == ".gguf":
        if not any(m["path"] == str(model_path) for m in models):
            try:
                size = model_path.stat().st_size
            except OSError:
                size = 0
            models.insert(
                0,
                {
                    "name": model_path.name,
                    "path": str(model_path),
                    "size_bytes": size,
                    "size_gb": round(size / (1024**3), 2),
                },
            )
    return {
        "ok": True,
        "model_dir": str(directory),
        "model_path": str(model_path),
        "model_present": model_path.exists(),
        "model_loaded": _parser is not None
        and getattr(_parser, "_llm", None) is not None,
        "models": models,
    }


settings = _load_settings()
app = FastAPI(title="MacAgent", version="1.0.1")

_parser: Optional[LocalIntentParser] = None
_router: Optional[CoreRouter] = None
_memory: Optional[ContextMemory] = None
_agent: Optional[AgentLoop] = None


def get_parser() -> LocalIntentParser:
    global _parser
    if _parser is None:
        _parser = LocalIntentParser()
    return _parser


def get_router() -> CoreRouter:
    global _router
    if _router is None:
        _router = CoreRouter(parser=get_parser())
    return _router


def get_memory() -> ContextMemory:
    global _memory
    if _memory is None:
        _memory = ContextMemory()
    return _memory


def get_agent() -> AgentLoop:
    global _agent
    if _agent is None:
        registry = ToolRegistry(memory=get_memory(), router=get_router())
        _agent = AgentLoop(parser=get_parser(), registry=registry)
    return _agent


def _looks_like_question(text: str) -> bool:
    return bool(text and _QUESTION_RE.search(text.strip()))


class SiteCreate(BaseModel):
    url: str = Field(..., min_length=1)
    purpose: str = Field(..., min_length=1)


class SiteUpdate(BaseModel):
    url: str = Field(..., min_length=1)
    purpose: str = Field(..., min_length=1)


class ModelSelectBody(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_path: str = Field(..., min_length=1)
    reload: bool = Field(
        True,
        description="If true, unload the current GGUF and load the new one immediately",
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    model_path = Path(
        settings.get("model_path", "~/Models/qwen2.5-3b-instruct-q4_k_m.gguf")
    ).expanduser()
    return {
        "status": "ok",
        "model_present": model_path.exists(),
        "model_path": str(model_path),
        "model_dir": str(_model_dir()),
        "model_loaded": _parser is not None
        and getattr(_parser, "_llm", None) is not None,
        "purpose_sites": len(get_memory().list_purpose_sites()),
    }


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """List GGUF files in model_dir (default ~/Models) and the active model_path."""
    return _models_payload()


class SettingsBody(BaseModel):
    tts_enabled: Optional[bool] = None
    tts_voice: Optional[str] = None
    tts_lang: Optional[str] = None
    tts_speed: Optional[float] = None
    tts_volume: Optional[float] = None
    tts_speak_status: Optional[bool] = None
    tts_speak_answer: Optional[bool] = None
    tts_muted: Optional[bool] = None


class DictationBody(BaseModel):
    active: bool = False


def _settings_payload() -> dict[str, Any]:
    global settings
    settings = _load_settings()
    cfg = tts_config()
    return {
        "ok": True,
        "tts_enabled": cfg["enabled"],
        "tts_voice": cfg["voice"],
        "tts_lang": cfg["lang"],
        "tts_speed": cfg["speed"],
        "tts_volume": cfg["volume"],
        "tts_speak_status": cfg["speak_status"],
        "tts_speak_answer": cfg["speak_answer"],
        "tts_muted": cfg["muted"],
    }


@app.get("/v1/settings")
async def get_settings() -> dict[str, Any]:
    return _settings_payload()


@app.put("/v1/settings")
async def put_settings(body: SettingsBody) -> dict[str, Any]:
    """Persist TTS / agent settings to settings.json."""
    global settings
    settings = _load_settings()
    data = body.model_dump(exclude_none=True)
    if "tts_volume" in data:
        data["tts_volume"] = max(0.0, min(1.5, float(data["tts_volume"])))
    if "tts_speed" in data:
        data["tts_speed"] = max(0.5, min(2.0, float(data["tts_speed"])))
    if "tts_voice" in data and data["tts_voice"]:
        data["tts_voice"] = str(data["tts_voice"]).strip()
    if "tts_lang" in data and data["tts_lang"]:
        data["tts_lang"] = str(data["tts_lang"]).strip()[:8]
    settings.update(data)
    _save_settings(settings)
    if data.get("tts_muted") is True:
        from tools.tts_kokoro import interrupt

        interrupt()
    return _settings_payload()


@app.post("/v1/tts/dictation")
async def tts_dictation(body: DictationBody) -> dict[str, Any]:
    """Pause TTS while the overlay mic is recording."""
    set_dictating(body.active)
    return {"ok": True, "dictating": body.active}


@app.put("/v1/models")
async def select_model(body: ModelSelectBody) -> dict[str, Any]:
    """Persist model_path to settings.json and optionally reload the GGUF in-process."""
    global settings
    path = Path(body.model_path).expanduser()
    if not path.exists() or path.suffix.lower() != ".gguf":
        raise HTTPException(
            status_code=400,
            detail=f"GGUF not found: {path}",
        )

    settings = _load_settings()
    settings["model_path"] = str(path)
    # Keep model_dir pointing at the folder that contains this file when sensible.
    if not settings.get("model_dir"):
        settings["model_dir"] = str(path.parent)
    _save_settings(settings)

    reload_info: dict[str, Any] = {"reloaded": False}
    if body.reload:
        try:
            parser = get_parser()
            reload_info = parser.reload(str(path))
            reload_info["reloaded"] = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Model reload failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    out = _models_payload()
    out.update(reload_info)
    return out


@app.get("/v1/sites")
async def list_sites() -> dict[str, Any]:
    return {"sites": get_memory().list_purpose_sites()}


@app.post("/v1/sites")
async def create_site(body: SiteCreate) -> dict[str, Any]:
    try:
        site = get_memory().add_purpose_site(body.url, body.purpose)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Registered purpose site id=%s url=%s", site["id"], site["url"])
    return site


@app.put("/v1/sites/{site_id}")
async def update_site(site_id: int, body: SiteUpdate) -> dict[str, Any]:
    try:
        site = get_memory().update_purpose_site(site_id, body.url, body.purpose)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if site is None:
        raise HTTPException(status_code=404, detail="site not found")
    logger.info("Updated purpose site id=%s", site_id)
    return site


@app.delete("/v1/sites/{site_id}")
async def delete_site(site_id: int) -> dict[str, Any]:
    ok = get_memory().delete_purpose_site(site_id)
    if not ok:
        raise HTTPException(status_code=404, detail="site not found")
    logger.info("Deleted purpose site id=%s", site_id)
    return {"ok": True, "id": site_id}


class AppAliasCreate(BaseModel):
    alias: str = Field(..., min_length=1)
    app_name: str = Field(..., min_length=1)


class AppAliasUpdate(BaseModel):
    alias: str = Field(..., min_length=1)
    app_name: str = Field(..., min_length=1)


@app.get("/v1/apps")
async def list_apps() -> dict[str, Any]:
    return {"apps": get_memory().list_app_aliases()}


@app.post("/v1/apps")
async def create_app(body: AppAliasCreate) -> dict[str, Any]:
    try:
        row = get_memory().add_app_alias(body.alias, body.app_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Registered app alias %r → %s", row["alias"], row["app_name"])
    return row


@app.put("/v1/apps/{alias_id}")
async def update_app(alias_id: int, body: AppAliasUpdate) -> dict[str, Any]:
    try:
        row = get_memory().update_app_alias(alias_id, body.alias, body.app_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="app alias not found")
    return row


@app.delete("/v1/apps/{alias_id}")
async def delete_app(alias_id: int) -> dict[str, Any]:
    ok = get_memory().delete_app_alias(alias_id)
    if not ok:
        raise HTTPException(status_code=404, detail="app alias not found")
    return {"ok": True, "id": alias_id}


@app.get("/v1/activity")
async def list_activity(limit: int = 100) -> dict[str, Any]:
    return {"activity": get_memory().list_activity(limit=min(limit, 500))}


@app.get("/v1/events/latest")
async def events_latest(limit: int = 20) -> dict[str, Any]:
    return {"events": event_bus.latest(limit=min(limit, 100))}


@app.get("/v1/debug/traces")
async def debug_traces_list(limit: int = 30) -> dict[str, Any]:
    return {"traces": debug_traces.latest(limit=min(limit, 50))}


@app.get("/v1/debug/traces/{trace_id}")
async def debug_trace_one(trace_id: int) -> dict[str, Any]:
    trace = debug_traces.get(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace


@app.get("/v1/events")
async def events_stream(request: Request, after_id: int = 0) -> StreamingResponse:
    async def event_generator():
        async for event in event_bus.subscribe(after_id=after_id):
            if await request.is_disconnected():
                break
            payload = json.dumps(event, ensure_ascii=False)
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/hud/show")
async def hud_show() -> dict[str, Any]:
    """Hint endpoint for clients; HUD activates itself on SSE events."""
    return {"ok": True}


class AskBody(BaseModel):
    text: str = Field(..., min_length=1)


class ConfirmBody(BaseModel):
    id: str = Field(..., min_length=1)
    approve: bool = False


class ContextBody(BaseModel):
    notes: str = ""


@app.get("/v1/context")
async def get_context() -> dict[str, Any]:
    return context_payload()


@app.put("/v1/context")
async def put_context(body: ContextBody) -> dict[str, Any]:
    save_user_notes(body.notes)
    return context_payload()


@app.post("/v1/ask")
async def ask_typed(body: AskBody) -> dict[str, Any]:
    """Typed query from the MacAgent app (same path as FreeFlow speech)."""
    spoken = (body.text or "").strip()
    if not spoken:
        raise HTTPException(status_code=400, detail="empty text")
    accepted = await _dispatch_spoken(spoken)
    return {"ok": accepted, "utterance": spoken}


@app.post("/v1/confirm")
async def confirm_action(body: ConfirmBody) -> dict[str, Any]:
    """Approve or deny a destructive action (empty Trash, rm, etc.)."""
    pending = take_pending(body.id)
    if not pending:
        raise HTTPException(status_code=404, detail="pending action not found or expired")

    utterance = str(pending.get("utterance") or "")
    summary = str(pending.get("summary") or "")
    command = str(pending.get("command") or "")

    if not body.approve:
        event_bus.publish(
            utterance=utterance,
            kind="answer",
            text="Cancelled — nothing was deleted.",
            detail="confirm_denied",
            step="confirm",
            tool_input={"id": body.id, "approve": False},
        )
        try:
            narrate_answer("Cancelled — nothing was deleted.")
        except Exception as exc:  # noqa: BLE001
            logger.debug("tts answer skipped: %s", exc)
        get_memory().log_activity(utterance, "confirm", "denied", summary[:300])
        return {"ok": True, "approved": False, "id": body.id}

    event_bus.publish(
        utterance=utterance,
        kind="action",
        text="Approved — running…",
        detail="pending",
        step="confirm",
    )
    result = run_bash(command, confirmed=True)
    if result.get("ok"):
        msg = (result.get("stdout") or "").strip() or "Done."
        cmd_l = command.lower()
        if "empty the trash" in cmd_l:
            msg = "Trash emptied."
        elif "shut down" in cmd_l:
            msg = "Shutting down…"
        elif "restart" in cmd_l:
            msg = "Restarting…"
        elif re.search(r"\bsleep\b", cmd_l):
            msg = "Sleeping…"
        event_bus.publish(
            utterance=utterance,
            kind="answer",
            text=msg,
            detail="confirm_approved",
            step="confirm",
            tool="run_bash",
            tool_input={"id": body.id, "command": command},
            tool_output=result,
        )
        try:
            narrate_answer(msg)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tts answer skipped: %s", exc)
        get_memory().log_activity(utterance, "confirm", "approved", msg[:300])
        return {"ok": True, "approved": True, "id": body.id, "result": result}

    err = result.get("error") or result.get("stderr") or "command failed"
    fail_msg = f"Could not complete that: {err}"
    event_bus.publish(
        utterance=utterance,
        kind="answer",
        text=fail_msg,
        detail="confirm_failed",
        step="confirm",
        tool_output=result,
    )
    try:
        narrate_answer(fail_msg)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tts answer skipped: %s", exc)
    return {"ok": False, "approved": True, "id": body.id, "error": err}


@app.post("/v1/history/harvest")
async def harvest_history() -> dict[str, Any]:
    try:
        count = harvest_chrome_history()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"upserted": count}


def _message_text(content: Any) -> str:
    if isinstance(content, list):
        parts = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return " ".join(parts).strip()
    return str(content or "").strip()


def _extract_spoken_text(user_text: str) -> str:
    """Pull the real utterance out of FreeFlow cleanup wrappers."""
    match = _RAW_TRANSCRIPTION_RE.search(user_text)
    if match:
        spoken = match.group(1).strip()
        if spoken:
            return spoken
    if "RAW_TRANSCRIPTION" in user_text.upper():
        lines = [ln.strip() for ln in user_text.splitlines() if ln.strip()]
        skip_prefixes = (
            "instructions:",
            "context:",
            "raw_transcription",
            "<<<",
            ">>>",
        )
        candidates = [
            ln
            for ln in lines
            if not ln.lower().startswith(skip_prefixes)
            and ln.upper() != "RAW_TRANSCRIPTION"
        ]
        if candidates:
            return candidates[-1]
    return user_text.strip()


def _is_freeflow_meta_request(messages: list, user_text: str) -> bool:
    """Skip FreeFlow context/activity probes — only route real dictation."""
    lower = user_text.lower()
    if "raw_transcription" in lower:
        return False
    meta_markers = (
        "analyze the context",
        "infer the user's current activity",
        "bundle id:",
        "selected text:",
        "user is working in a desktop application",
    )
    if "bundle id:" in lower and ("app:" in lower or "window:" in lower):
        return True
    if any(marker in lower for marker in meta_markers):
        return True
    for message in messages:
        role = message.get("role")
        text = _message_text(message.get("content")).lower()
        if role == "system" and (
            "current activity" in text
            or "nearby app context" in text
            or "analyze the context" in text
        ):
            return True
        if "bundle id:" in text and "selected text:" in text:
            return True
    return False


_REWRITE_MARKERS = (
    "transform selected_text",
    "transform selected text",
    "voice_command",
    "voice command:",
    "selected_text:",
    "selected text:",
    "return only the replacement text",
    "replacement text",
)

_SELECTED_TEXT_RE = re.compile(
    r"SELECTED_TEXT\s*:\s*(?:'(.*?)'|\"(.*?)\"|(.*?)(?:\n\s*\w+:|\Z))",
    re.DOTALL | re.IGNORECASE,
)


def _is_freeflow_rewrite_request(messages: list, user_text: str) -> bool:
    """FreeFlow selection-rewrite prompts are not MacAgent voice commands."""
    blob = (user_text or "").lower()
    for message in messages:
        blob += "\n" + _message_text(message.get("content")).lower()
    hits = sum(1 for m in _REWRITE_MARKERS if m in blob)
    return hits >= 2 or (
        "transform selected" in blob and "replacement" in blob
    )


def _extract_selected_text_for_noop(user_text: str) -> str:
    """Return SELECTED_TEXT unchanged so FreeFlow does not wipe the selection."""
    match = _SELECTED_TEXT_RE.search(user_text or "")
    if not match:
        return ""
    return (match.group(1) or match.group(2) or match.group(3) or "").strip()


def _should_skip_duplicate(spoken: str) -> bool:
    global _last_executed
    now = time.monotonic()
    key = spoken.strip().lower()
    last_key, last_ts = _last_executed
    if key and key == last_key and (now - last_ts) < _DEDUPE_WINDOW_SEC:
        return True
    _last_executed = (key, now)
    return False


def _completion(content: str) -> JSONResponse:
    return JSONResponse(
        content={
            "id": "chatcmpl-macagent-daemon",
            "object": "chat.completion",
            "created": 1719811200,
            "model": "macagent-local-router",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
    )


_REFUSAL_RE = re.compile(
    r"(?i)(don'?t have access|do not have access|no access to (the )?internet|"
    r"can'?t (access|browse|search)|cannot (access|browse|search)|"
    r"real[- ]?time|NEED_BROWSER|check your local)"
)

# Tiny local-only prompts — no need to hit the web.
_LOCAL_ONLY_RE = re.compile(
    r"(?i)^\s*("
    r"hi|hello|hey|thanks|thank you|who are you|what can you do|"
    # Math / arithmetic (spoken variants: "whats the 2+4", "what is 2 + 4")
    r"(what('?s|s|\s+is)(\s+the)?\s+)?\d[\d\s\+\-\*\/x×÷]+\d\s*\??|"
    r"\d+\s*[\+\-\*\/x×÷]\s*\d+\s*\??"
    r")\s*$"
)

_MATH_EXPR_RE = re.compile(
    r"(?i)(\d+)\s*([\+\-\*\/x×÷])\s*(\d+)"
)


def _try_local_math(text: str) -> Optional[str]:
    """Answer simple a±b / a×b / a÷b questions without tools."""
    m = _MATH_EXPR_RE.search(text or "")
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    if op in {"x", "X", "×", "*"}:
        return str(a * b)
    if op in {"÷", "/"}:
        if b == 0:
            return "undefined (division by zero)"
        return str(a / b if a % b else a // b)
    if op == "+":
        return str(a + b)
    if op == "-":
        return str(a - b)
    return None


def _prefer_web_context(spoken: str) -> bool:
    """Use DuckDuckGo + page read for factual / current questions when possible."""
    text = (spoken or "").strip()
    if not text or _LOCAL_ONLY_RE.match(text):
        return False
    if needs_browser(text) or _looks_like_question(text):
        return True
    return False


def _handle_spoken(spoken: str, trace_id: Optional[int] = None) -> None:
    """Agent tool loop (with a tiny local-only fast path)."""
    set_current_trace_id(trace_id)
    try:
        _handle_spoken_inner(spoken)
    finally:
        set_current_trace_id(None)


def _handle_spoken_inner(spoken: str) -> None:
    """Prefer tool-calling agent; keep a local-only greeting/math fast path."""
    text = (spoken or "").strip()

    math = _try_local_math(text)
    if math is not None and (
        _LOCAL_ONLY_RE.match(text) or _MATH_EXPR_RE.search(text)
    ):
        # Prefer exact arithmetic for clear math questions.
        if re.search(r"(?i)\b(what|whats|what's|is|equals?|compute|calculate)\b", text) or _LOCAL_ONLY_RE.match(text):
            # Avoid treating "open app 2" style as math unless expression is the intent.
            if re.search(r"(?i)[\+\-\*\/x×÷]", text):
                reply = math
                get_memory().log_activity(text, "answer", "local_math", reply)
                event_bus.publish(
                    utterance=text,
                    kind="trace",
                    text="Received input",
                    detail="input",
                    step="input",
                    tool_input={"utterance": text},
                )
                event_bus.publish(
                    utterance=text,
                    kind="trace",
                    text="Local math",
                    detail="local_math",
                    step="tool_result",
                    tool="local_math",
                    tool_input={"expression": text},
                    tool_output={"result": reply},
                )
                event_bus.publish(
                    utterance=text, kind="answer", text=reply, detail="local_math"
                )
                try:
                    narrate_answer(reply)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("tts answer skipped: %s", exc)
                trace_step("final", kind="answer", detail="local_math", text=reply)
                return

    if text and _LOCAL_ONLY_RE.match(text):
        trace_step("route", decision="local_only_fast")
        reply = _try_local_math(text) or get_parser().generate_answer(text)
        get_memory().log_activity(text, "answer", "local_llm", reply)
        event_bus.publish(
            utterance=text,
            kind="answer",
            text=reply,
            detail="local_llm",
        )
        try:
            narrate_answer(reply)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tts answer skipped: %s", exc)
        trace_step("final", kind="answer", detail="local_llm", text=reply)
        return

    trace_step("route", decision="agent_loop")
    logger.info("Agent loop for %r", text)
    result = get_agent().run(text)
    if result.get("action") == "confirm":
        logger.info(
            "Waiting for user confirm id=%s cmd=%r",
            result.get("pending_id"),
            (result.get("command") or "")[:120],
        )
        return
    logger.info(
        "Agent finished steps=%s answer_chars=%s",
        result.get("steps"),
        len(result.get("answer") or ""),
    )


def _answer_with_web_context(spoken: str) -> bool:
    """Search → read page(s) → grounded local answer. Never opens a browser."""
    logger.info("Grounded web answer for %r", spoken)
    event_bus.publish(
        utterance=spoken,
        kind="action",
        text="Searching & reading sources…",
        detail="pending",
    )
    context, sources = build_grounded_context(
        spoken, max_results=4, pages_to_read=2, max_chars_per_page=2800
    )
    trace_step(
        "web_grounded",
        sources=sources,
        context_chars=len(context or ""),
        context_preview=(context or "")[:2000],
    )
    if not context.strip():
        logger.warning("No web context for %r", spoken)
        trace_step("web_grounded", ok=False)
        return False

    reply = get_parser().answer_from_search(spoken, context)
    if sources:
        shown = sources[:3]
        src_lines = "\n".join(f"- {u}" for u in shown)
        reply = f"{reply}\n\nSources:\n{src_lines}"

    get_memory().log_activity(spoken, "answer", "duckduckgo_grounded", reply[:500])
    event_bus.publish(
        utterance=spoken,
        kind="answer",
        text=reply,
        detail="duckduckgo_grounded",
    )
    try:
        narrate_answer(reply)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tts answer skipped: %s", exc)
    trace_step("final", kind="answer", detail="duckduckgo_grounded", text=reply)
    logger.info(
        "Grounded answer (%d chars, %d sources)", len(reply), len(sources)
    )
    return True


def _execute_and_publish(intent: dict, detail: str) -> None:
    spoken = (intent.get("raw_query") or intent.get("target") or "").strip()
    try:
        result = get_router().execute(intent)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Router failed")
        event_bus.publish(
            utterance=spoken,
            kind="error",
            text=str(exc),
            detail="router",
        )
        trace_step("router_error", error=str(exc), intent=intent)
        return

    event_bus.publish(
        utterance=spoken,
        kind="action",
        text=result or "Done",
        detail=detail or intent.get("action") or "action",
    )
    trace_step(
        "final",
        kind="action",
        detail=detail or intent.get("action"),
        text=result,
        intent=intent,
    )


@app.post("/v1/chat/completions")
async def intercept_freeflow_stream(request: Request) -> JSONResponse:
    payload = await request.json()
    messages = payload.get("messages", [])

    user_raw = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            user_raw = _message_text(message.get("content"))
            break

    if _is_freeflow_rewrite_request(messages, user_raw):
        # FreeFlow "edit selection" / transform — not a MacAgent command.
        noop = _extract_selected_text_for_noop(user_raw)
        logger.info(
            "Ignoring FreeFlow rewrite/transform (noop %d chars)",
            len(noop),
        )
        return _completion(noop)

    if _is_freeflow_meta_request(messages, user_raw):
        logger.info("Ignoring FreeFlow context/meta request")
        return _completion("User is working in a desktop application.")

    spoken = _extract_spoken_text(user_raw) if user_raw else ""
    # Extra guard: never treat FreeFlow template blobs as speech.
    if spoken and _is_freeflow_rewrite_request([], spoken):
        logger.info("Skipping rewrite-shaped spoken blob")
        # FreeFlow sentinel: do not paste into the focused app.
        return _completion("EMPTY")
    logger.info("Intercepted FreeFlow transcript spoken=%r", spoken)

    if spoken:
        await _dispatch_spoken(spoken)

    # FreeFlow pastes the model output. "EMPTY" means paste nothing (see FreeFlow sanitizer).
    return _completion("EMPTY")


async def _dispatch_spoken(spoken: str) -> bool:
    """Run answer-or-act for one utterance. Returns False if skipped as duplicate."""
    key = spoken.strip().lower()
    if key in _in_flight or _should_skip_duplicate(spoken):
        logger.info("Skipping duplicate/in-flight command: %r", spoken)
        return False
    _in_flight.add(key)
    tid = debug_traces.start(spoken, source="ask")
    event_bus.publish(
        utterance=spoken,
        kind="action",
        text="Thinking…",
        detail="pending",
    )
    try:
        narrate("thinking", force=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tts narrate thinking skipped: %s", exc)
    try:
        await asyncio.to_thread(_handle_spoken, spoken, tid)
        debug_traces.finish(tid, status="ok")
    except Exception as exc:  # noqa: BLE001
        debug_traces.finish(tid, status="error", result=str(exc))
        raise
    finally:
        _in_flight.discard(key)
    return True


if __name__ == "__main__":
    host = settings.get("host", "127.0.0.1")
    port = int(settings.get("port", 8081))
    uvicorn.run(app, host=host, port=port)

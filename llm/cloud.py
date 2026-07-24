"""Optional OpenAI-compatible cloud chat with local-first privacy gates."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CLOUD: dict[str, Any] = {
    "enabled": False,
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model_name": "gpt-4o-mini",
    "route_general_queries": True,
}

# Gemini OpenAI-compatible root (we append /chat/completions).
_GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

# Built-in OpenAI-compatible providers (Custom = any other base URL).
CLOUD_PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model_name": "gpt-4o-mini",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model_name": "deepseek-chat",
    },
    "google": {
        "label": "Google (Gemini)",
        "base_url": _GEMINI_OPENAI_BASE,
        "model_name": "gemini-2.0-flash",
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model_name": "llama-3.3-70b-versatile",
    },
    "custom": {
        "label": "Custom",
        "base_url": "https://api.openai.com/v1",
        "model_name": "gpt-4o-mini",
    },
}

# Mac / filesystem / UI / live machine state must never leave the device.
# Keep this tight — bare "file(s)" also appears in coding/knowledge Qs
# ("open-source file format"), so only match action/inspect phrasing.
_LOCAL_SYSTEM_RE = re.compile(
    r"(?i)\b("
    r"folder|directory|downl\w*|trash|bin|"
    r"(biggest|largest|recent).{0,40}files?|"
    r"(list|show|find|search|delete|open|move|copy).{0,40}files?|"
    r"files?\s+(in|on|under|from|named|called)\b|"
    r"open|launch|quit|close|bash|shell|terminal|"
    r"delete|remove|rm\b|mv\b|cp\b|mkdir|touch|"
    r"shut\s*down|restart|reboot|sleep|log\s*out|"
    r"screen|desktop|documents|finder|spotlight|"
    r"click|press|empty\s+(the\s+)?(bin|trash)|"
    r"mac|macos|system\s+settings|preference|"
    r"clipboard|screenshot|ui\b|dock|"
    r"application\s+support|"
    r"type\s+(this|that|here|into|in)|"
    r"keystroke|keystrokes|"
    # Live machine state — must use local tools, never cloud invention.
    # Avoid bare "process" (blocks "what is a process in OS").
    r"list\s+.{0,40}?process(?:es)?|"
    r"show\s+.{0,40}?process(?:es)?|"
    r"(all|running)\s+process(?:es)?|"
    r"process(?:es)?\s+(on|for)\s+(my\s+)?(mac|computer|machine)|"
    r"ps\s+aux|activity\s+monitor|"
    r"system\s+resources|resource\s+usage|"
    r"top\s+(cpu|memory|ram|process(?:es)?|apps)|"
    r"(cpu|memory|ram|disk|battery)\s+(usage|hogs?|percent|%)|"
    r"what('?s|\s+is)\s+using\s+(my\s+)?(cpu|memory|ram)|"
    r"running\s+(on\s+)?(my\s+)?(mac|computer|machine)|"
    r"on\s+(the\s+|my\s+)?(mac|computer|machine|laptop)|"
    r"wifi|bluetooth|brightness|volume\s+(up|down|to)|"
    r"\bmute\b|\bunmute\b"
    r")\b"
)

_ABS_USER_PATH_RE = re.compile(
    r"(?P<path>/Users/[A-Za-z0-9_.-]+(?:/[^\s\"'<>]*)?)"
)
_TILDE_PATH_RE = re.compile(r"(?P<path>~(?:/[^\s\"'<>]*)?)")
_IPV4_RE = re.compile(
    r"\b(?P<ip>(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?))\b"
)


def default_cloud_settings() -> dict[str, Any]:
    return dict(_DEFAULT_CLOUD)


def list_cloud_providers() -> list[dict[str, str]]:
    """UI-facing provider catalog."""
    items: list[dict[str, str]] = []
    for pid, meta in CLOUD_PROVIDERS.items():
        items.append(
            {
                "id": pid,
                "label": meta["label"],
                "base_url": meta["base_url"],
                "model_name": meta["model_name"],
            }
        )
    return items


def infer_provider_from_base_url(base_url: str) -> str:
    """Best-effort match of a base URL to a built-in provider id."""
    lower = (base_url or "").strip().lower().rstrip("/")
    if not lower:
        return "openai"
    if "deepseek.com" in lower:
        return "deepseek"
    if "groq.com" in lower:
        return "groq"
    if (
        "googleapis.com" in lower
        or "generativelanguage" in lower
        or "gemini" in lower
    ):
        return "google"
    if "api.openai.com" in lower:
        return "openai"
    # Known preset base match
    for pid, meta in CLOUD_PROVIDERS.items():
        if pid == "custom":
            continue
        if lower == meta["base_url"].lower().rstrip("/"):
            return pid
    return "custom"


def normalize_cloud_base_url(raw: str) -> str:
    """Normalize provider base URL. App always appends /chat/completions.

    Fixes common Gemini mistakes like https://googleapis.com → the real
    generativelanguage …/v1beta/openai root.
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return str(_DEFAULT_CLOUD["base_url"])

    # If the user pasted a full completions URL, strip the leaf.
    lower = url.lower()
    for suffix in ("/chat/completions", "/completions"):
        if lower.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            lower = url.lower()
            break

    # Gemini / Google AI Studio — require the OpenAI-compat path.
    gemini_hints = (
        "googleapis.com",
        "generativelanguage",
        "gemini",
        "google.ai",
    )
    if any(h in lower for h in gemini_hints):
        # Already correct OpenAI-compat root (with or without trailing path junk).
        if "v1beta/openai" in lower:
            # Keep only up through …/v1beta/openai
            idx = lower.find("v1beta/openai")
            return url[: idx + len("v1beta/openai")].rstrip("/")
        return _GEMINI_OPENAI_BASE

    return url


def normalize_cloud_settings(raw: Any) -> dict[str, Any]:
    """Merge a settings `cloud` block with defaults."""
    out = default_cloud_settings()
    if not isinstance(raw, dict):
        return out
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if raw.get("base_url"):
        out["base_url"] = normalize_cloud_base_url(str(raw["base_url"]))
    if "api_key" in raw and raw["api_key"] is not None:
        out["api_key"] = str(raw["api_key"])
    if raw.get("model_name"):
        # Single-line model id (UI paste sometimes inserts newlines).
        name = str(raw["model_name"]).replace("\r", "\n").split("\n", 1)[0].strip()
        if name:
            out["model_name"] = name
    if "route_general_queries" in raw:
        out["route_general_queries"] = bool(raw["route_general_queries"])

    provider = str(raw.get("provider") or "").strip().lower()
    if provider not in CLOUD_PROVIDERS:
        provider = infer_provider_from_base_url(str(out.get("base_url") or ""))
    out["provider"] = provider

    # Switching to a built-in provider (explicit id in payload) applies its base URL.
    # Custom keeps whatever base_url the user set.
    if (
        provider != "custom"
        and str(raw.get("provider") or "").strip().lower() == provider
        and not raw.get("base_url")
    ):
        preset = CLOUD_PROVIDERS[provider]
        out["base_url"] = preset["base_url"]
        if not raw.get("model_name"):
            out["model_name"] = preset["model_name"]

    if provider == "google":
        out["base_url"] = normalize_cloud_base_url(str(out.get("base_url") or ""))

    return out


def mask_api_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 4:
        return "••••"
    return f"••••{key[-4:]}"


def is_local_system_task(text: str) -> bool:
    """True when the utterance looks like a Mac/system/file action."""
    return bool(text and _LOCAL_SYSTEM_RE.search(text))


def cloud_is_configured(cloud: dict[str, Any]) -> bool:
    """True when cloud is enabled and has credentials (ignores routing heuristics)."""
    cfg = normalize_cloud_settings(cloud)
    return bool(cfg.get("enabled") and (cfg.get("api_key") or "").strip())


def cloud_routing_allowed(cloud: dict[str, Any], utterance: str) -> bool:
    """Whether a general-knowledge reply may use the cloud provider."""
    cfg = normalize_cloud_settings(cloud)
    if not cloud_is_configured(cfg):
        return False
    if not cfg.get("route_general_queries", True):
        return False
    if is_local_system_task(utterance):
        return False
    return True


def sanitize_for_cloud(text: str) -> tuple[str, dict[str, str]]:
    """Scrub home paths, local username, and IPs. Returns (scrubbed, restore_map)."""
    restore: dict[str, str] = {}
    if not text:
        return text, restore

    scrubbed = text
    path_i = 0
    ip_i = 0

    def _remember(placeholder: str, original: str) -> str:
        restore[placeholder] = original
        return placeholder

    def _abs_sub(m: re.Match[str]) -> str:
        nonlocal path_i
        original = m.group("path")
        ph = f"<LOCAL_PATH_{path_i}>"
        path_i += 1
        return _remember(ph, original)

    def _tilde_sub(m: re.Match[str]) -> str:
        nonlocal path_i
        original = m.group("path")
        ph = f"<LOCAL_PATH_{path_i}>"
        path_i += 1
        return _remember(ph, original)

    def _ip_sub(m: re.Match[str]) -> str:
        nonlocal ip_i
        original = m.group("ip")
        ph = f"<LOCAL_IP_{ip_i}>"
        ip_i += 1
        return _remember(ph, original)

    scrubbed = _ABS_USER_PATH_RE.sub(_abs_sub, scrubbed)
    scrubbed = _TILDE_PATH_RE.sub(_tilde_sub, scrubbed)
    scrubbed = _IPV4_RE.sub(_ip_sub, scrubbed)

    try:
        username = Path.home().name
    except Exception:  # noqa: BLE001
        username = ""
    if username and len(username) >= 2:
        # Whole-word-ish username redaction (avoid eating short tokens).
        user_re = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(username)}(?![A-Za-z0-9_])")
        if user_re.search(scrubbed):
            scrubbed = user_re.sub("<LOCAL_USER>", scrubbed)
            restore["<LOCAL_USER>"] = username

    return scrubbed, restore


def restore_placeholders(text: str, restore_map: dict[str, str]) -> str:
    if not text or not restore_map:
        return text or ""
    out = text
    # Longer placeholders first so PATH_10 is not partially hit by PATH_1.
    for ph in sorted(restore_map.keys(), key=len, reverse=True):
        out = out.replace(ph, restore_map[ph])
    return out


def parse_cloud_envelope(raw: str) -> dict[str, Any]:
    """Parse cloud JSON envelope; on failure treat whole text as a final answer."""
    text = (raw or "").strip()
    if not text:
        return {
            "final": True,
            "answer": "",
            "guidance": "",
            "commands": [],
        }

    candidate = text
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if fence:
        candidate = fence.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]

    try:
        import json as _json

        data = _json.loads(candidate)
    except Exception:  # noqa: BLE001
        return {
            "final": True,
            "answer": text,
            "guidance": "",
            "commands": [],
        }

    if not isinstance(data, dict):
        return {
            "final": True,
            "answer": text,
            "guidance": "",
            "commands": [],
        }

    final = data.get("final")
    if final is None:
        # Missing flag → final unless guidance/commands clearly ask for local work.
        cmds = data.get("commands") or []
        guidance = str(data.get("guidance") or "").strip()
        final = not (guidance or cmds)
    else:
        final = bool(final)

    answer = str(data.get("answer") or data.get("text") or "").strip()
    guidance = str(data.get("guidance") or data.get("plan") or "").strip()
    raw_cmds = data.get("commands") or data.get("bash") or []
    commands: list[str] = []
    if isinstance(raw_cmds, str) and raw_cmds.strip():
        commands = [raw_cmds.strip()]
    elif isinstance(raw_cmds, list):
        for c in raw_cmds[:8]:
            s = str(c or "").strip()
            if s:
                commands.append(s)

    if final and not answer and guidance:
        answer = guidance
    if not final and not answer and not guidance and commands:
        guidance = "Run the provided shell commands on this Mac and report the results."

    return {
        "final": final,
        "answer": answer,
        "guidance": guidance,
        "commands": commands,
    }


def merge_restore_maps(*maps: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for m in maps:
        if m:
            merged.update(m)
    return merged


def cloud_chat(
    messages: list[dict[str, str]],
    cloud: dict[str, Any],
    *,
    max_tokens: Optional[int] = None,
    temperature: float = 0.3,
    timeout: float = 180.0,
    on_token: Optional[Any] = None,
    json_mode: bool = False,
) -> str:
    """OpenAI-compatible chat.completions (optional SSE stream).

    Uses a minimal JSON body — Gemini's OpenAI adapter 400s on extra fields
    like ``store``. Raises on HTTP / network errors (includes response body).

    When ``max_tokens`` is None, the field is omitted so the provider uses its
    model default output length (no artificial MacAgent cap).

    ``json_mode`` asks OpenAI-compatible providers for a JSON object (skipped for
    Google — use prompt-only JSON there).
    """
    import httpx

    cfg = normalize_cloud_settings(cloud)
    base = (cfg.get("base_url") or _DEFAULT_CLOUD["base_url"]).rstrip("/")
    url = f"{base}/chat/completions"
    api_key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model_name") or _DEFAULT_CLOUD["model_name"]).strip()
    provider = str(cfg.get("provider") or "openai")
    if not api_key:
        raise RuntimeError("Cloud API key is empty")

    # Strict minimal body — Gemini rejects unknown OpenAI-only fields.
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    # Clamp temperature — some Gemini builds reject >1.0 or odd floats.
    temp = float(temperature)
    if provider == "google":
        temp = max(0.0, min(1.0, temp))
    payload["temperature"] = temp
    # JSON object mode — DeepSeek / OpenAI / Groq; Gemini often 400s on this.
    if json_mode and provider in {"openai", "deepseek", "groq", "custom"}:
        payload["response_format"] = {"type": "json_object"}
    use_stream = on_token is not None
    if use_stream:
        payload["stream"] = True

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _http_error(resp: "httpx.Response") -> RuntimeError:
        body = (resp.text or "")[:800]
        return RuntimeError(
            f"Cloud HTTP {resp.status_code} for {url}: {body or resp.reason_phrase}"
        )

    if use_stream:
        return _cloud_chat_stream(
            url, headers, payload, timeout=timeout, on_token=on_token, http_error=_http_error
        )

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise _http_error(resp)
        data = resp.json()
    return _extract_chat_content(data)


def _extract_chat_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("Cloud response had no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        content = "".join(parts)
    text = (content or "").strip()
    if not text:
        raise RuntimeError("Cloud response was empty")
    return text


def _cloud_chat_stream(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    *,
    timeout: float,
    on_token: Any,
    http_error: Any,
) -> str:
    import httpx
    import json as _json

    pieces: list[str] = []
    with httpx.Client(timeout=timeout) as client:
        with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                # Read body for a useful error (stream otherwise buffers it).
                resp.read()
                raise http_error(resp)
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    data_s = line[5:].strip()
                elif line.startswith("{"):
                    data_s = line.strip()
                else:
                    continue
                if data_s == "[DONE]":
                    break
                try:
                    chunk = _json.loads(data_s)
                except _json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece is None:
                    # Some providers send full message on stream chunks.
                    msg = choices[0].get("message") or {}
                    piece = msg.get("content")
                if not piece:
                    continue
                if isinstance(piece, list):
                    piece = "".join(
                        str(p.get("text") or "") if isinstance(p, dict) else str(p)
                        for p in piece
                    )
                piece = str(piece)
                if not piece:
                    continue
                pieces.append(piece)
                try:
                    on_token("".join(pieces))
                except Exception:  # noqa: BLE001
                    pass
    text = "".join(pieces).strip()
    if not text:
        raise RuntimeError("Cloud stream returned empty content")
    return text


class HybridInferenceEngine:
    """Thin facade: routing gate + sanitize + cloud_chat + restore."""

    def __init__(self, cloud: Optional[dict[str, Any]] = None):
        self._cloud = normalize_cloud_settings(cloud)

    def update_settings(self, cloud: Optional[dict[str, Any]]) -> None:
        self._cloud = normalize_cloud_settings(cloud)

    @property
    def settings(self) -> dict[str, Any]:
        return dict(self._cloud)

    def should_use_cloud(self, utterance: str) -> bool:
        return cloud_routing_allowed(self._cloud, utterance)

    def cloud_ready(self) -> bool:
        return cloud_is_configured(self._cloud)

    def complete(
        self,
        system: str,
        user: str,
        utterance: str,
        *,
        max_tokens: Optional[int] = None,
        temperature: float = 0.3,
        on_token: Optional[Any] = None,
        json_mode: bool = False,
        force: bool = False,
    ) -> str:
        """Sanitize, call cloud (optionally streaming), restore placeholders.

        ``max_tokens=None`` omits the cap — provider uses its model default.
        ``json_mode`` requests a JSON object when the provider supports it.
        ``force=True`` uses cloud whenever configured (e.g. scraped web answers),
        even if the utterance looks like a local Mac task.
        """
        if force:
            if not cloud_is_configured(self._cloud):
                raise RuntimeError("Cloud is not configured")
        elif not self.should_use_cloud(utterance):
            raise RuntimeError("Cloud routing not allowed for this request")
        sys_s, sys_map = sanitize_for_cloud(system)
        user_s, user_map = sanitize_for_cloud(user)
        restore = merge_restore_maps(sys_map, user_map)

        def _cb(accumulated: str) -> None:
            if on_token is None:
                return
            on_token(restore_placeholders(accumulated, restore))

        messages = [
            {"role": "system", "content": sys_s},
            {"role": "user", "content": user_s},
        ]
        try:
            raw = cloud_chat(
                messages,
                self._cloud,
                max_tokens=max_tokens,
                temperature=temperature,
                on_token=_cb if on_token else None,
                json_mode=json_mode,
            )
        except Exception:
            # Some gateways reject response_format — retry once without it.
            if json_mode:
                raw = cloud_chat(
                    messages,
                    self._cloud,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    on_token=_cb if on_token else None,
                    json_mode=False,
                )
            else:
                raise
        return restore_placeholders(raw, restore)

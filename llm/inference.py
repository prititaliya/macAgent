import json
import logging
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional

from llama_cpp import Llama, LlamaGrammar

from events.debug_trace import trace_step
from llm.cloud import HybridInferenceEngine, parse_cloud_envelope
from memory.user_context import build_runtime_context, load_user_notes_for_llm

logger = logging.getLogger(__name__)

# Serialize Metal inference — concurrent llama_decode on 8GB unified → -1 / garbage.
_INFER_LOCK = threading.Lock()

_EMPTY_REFUSAL_RE = re.compile(
    r"(?i)\b("
    r"could not find|couldn'?t find|cannot find|can'?t find|"
    r"no information|not (enough )?information|"
    r"does not contain|do(?:es)?n'?t (?:have|contain)|"
    r"unable to find|not (?:mentioned|available|provided) in|"
    r"i don'?t know (?:anything )?about you|"
    r"sources provided|"
    r"not enough context|insufficient (?:context|information|detail)|"
    r"would need to search|need (?:more|to) (?:search|information|context)|"
    r"web context does not|provided web context|"
    r"i('m| am) sorry.? but|"
    r"cannot (?:provide|answer)|can'?t (?:provide|answer) "
    r")\b"
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SETTINGS_PATH = _PROJECT_ROOT / "config" / "settings.json"

_VALID_ACTIONS = frozenset(
    {"open_site", "open_app", "workflow", "search_fallback", "answer", "browse"}
)

_QUESTION_RE = re.compile(
    r"(?i)^\s*(what|why|how|who|when|where|which|whose|whom|is|are|was|were|"
    r"do|does|did|can|could|would|should|will|am|tell me|explain|define|"
    r"is there|are there|what's|who's|how's|where's)\b|[?]\s*$"
)
_ORDER_RE = re.compile(
    r"(?i)\b(open|launch|start|quit|close|go to|navigate|search for|google|browse)\b"
)
# Live / lookup requests must use the browser, not a static LLM answer.
_BROWSER_RE = re.compile(
    r"(?i)\b("
    r"search(\s+for)?|google|look\s*up|find\s+(me|out)|browse|"
    r"next\s+(game|match|fixture)|live\s+(score|scores|game|match)|"
    r"what('?s|\s+is)\s+on|kickoff|fixture|schedule|"
    r"current\s+(score|weather|price|news)|real[- ]?time|"
    r"latest\s+(news|score|scores)|who\s+won|what\s+time\s+is\s+the\s+game"
    r")\b"
)
_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_GEMMA_THINK_RE = re.compile(
    r"<\|channel>thought\n.*?(?:<channel\|>|$)", re.DOTALL | re.IGNORECASE
)
_DEFAULT_MODEL = "~/Models/Qwen3-4B-Q4_K_M.gguf"


def _load_settings() -> dict:
    if _SETTINGS_PATH.exists():
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _build_purpose_grammar(valid_ids: List[int]) -> str:
    """GBNF that emits SITE=null or SITE=<id>.

    Note: this llama-cpp build rejects underscores in rule names.
    """
    id_alts = " | ".join(f'"{i}"' for i in sorted(valid_ids)) if valid_ids else '"0"'
    return (
        "root ::= \"SITE=\" sid\n"
        f"sid ::= \"null\" | {id_alts}\n"
    )


def needs_browser(raw_text: str) -> bool:
    """True when the utterance needs the web, not a static local answer."""
    return bool(raw_text and _BROWSER_RE.search(raw_text))


def _heuristic_intent(raw_text: str) -> Optional[dict[str, Any]]:
    """Fast path — clear questions → answer; browse/search → browser."""
    text = (raw_text or "").strip()
    if not text:
        return None
    if needs_browser(text) or _ORDER_RE.search(text):
        if needs_browser(text):
            return {
                "action": "browse",
                "target": text,
                "raw_query": text,
            }
        return None
    if _QUESTION_RE.search(text):
        return {"action": "answer", "target": "", "raw_query": text}
    return None


def _parse_intent_json(text: str, raw_query: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    candidates = [text.strip()]
    candidates.extend(_JSON_OBJ_RE.findall(text))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        action = str(data.get("action") or "").strip()
        if action not in _VALID_ACTIONS:
            continue
        return {
            "action": action,
            "target": str(data.get("target") or "").strip(),
            "raw_query": str(data.get("raw_query") or raw_query).strip() or raw_query,
        }
    return None


def _is_qwen3(model_path: str) -> bool:
    name = Path(model_path).name.lower()
    return "qwen3" in name or "qwen-3" in name


def _is_gemma(model_path: str) -> bool:
    name = Path(model_path).name.lower()
    return "gemma" in name


def _is_phi(model_path: str) -> bool:
    name = Path(model_path).name.lower()
    return (
        "phi-4" in name
        or "phi4" in name
        or "phi-3.5" in name
        or "phi3.5" in name
        or "phi-3" in name
        or "phi3" in name
        or name.startswith("phi-")
    )


def _is_llama3(model_path: str) -> bool:
    """Llama 3 / 3.1 / 3.2 Instruct — Meta header chat template."""
    name = Path(model_path).name.lower()
    if "llama-3" in name or "llama3" in name:
        return True
    # Bare "Llama-3.2-3B-Instruct…" style without hyphen after llama
    return bool(re.search(r"\bllama[\s._-]*3", name))


def _is_smol(model_path: str) -> bool:
    name = Path(model_path).name.lower()
    return "smollm" in name or "smol-lm" in name or "smolm" in name


def _model_too_heavy_for_mac(model_path: str) -> Optional[str]:
    """Reject GGUFs that reliably OOM / llama_decode -3 on 8GB unified memory."""
    path = Path(model_path).expanduser()
    if not path.exists():
        return None
    gb = path.stat().st_size / (1024**3)
    # ~5.5GB+ weights leave almost no room for Metal KV on 8GB machines.
    if gb >= 5.5:
        return (
            f"{path.name} is {gb:.1f} GB — too large for reliable local use on this Mac. "
            "Pick Qwen3-4B-Q4_K_M (or another ≤~4GB Instruct GGUF) instead."
        )
    return None


def _model_display_name(model_path: str) -> str:
    """Human-readable local model name for system prompts."""
    name = Path(model_path).name
    stem = name.removesuffix(".gguf")
    lower = stem.lower()
    if "phi-4-mini" in lower or "phi4-mini" in lower:
        return "Microsoft Phi-4-mini-instruct (local GGUF)"
    if "phi-3.5" in lower or "phi3.5" in lower:
        return "Microsoft Phi-3.5 Mini (local GGUF)"
    if "phi-3" in lower or "phi3" in lower:
        return "Microsoft Phi-3 (local GGUF)"
    if "gemma-4" in lower or "gemma4" in lower:
        return "Google Gemma 4 (local GGUF)"
    if "smollm" in lower or "smol-lm" in lower:
        return "SmolLM (local GGUF)"
    if _is_llama3(model_path):
        if "3.2" in lower or "3_2" in lower:
            if "1b" in lower:
                return "Llama 3.2 1B (local GGUF)"
            if "3b" in lower:
                return "Llama 3.2 3B (local GGUF)"
            return "Llama 3.2 (local GGUF)"
        return "Llama 3 (local GGUF)"
    if "qwen3" in lower or "qwen-3" in lower:
        if "30b" in lower:
            return "Qwen3-30B (local GGUF)"
        if "4b" in lower:
            return "Qwen3-4B (local GGUF)"
        return "Qwen3 (local GGUF)"
    if "qwen2.5" in lower or "qwen2_5" in lower or "qwen-2.5" in lower:
        if "7b" in lower:
            return "Qwen 2.5 7B (local GGUF)"
        return "Qwen 2.5 (local GGUF)"
    if "qwen" in lower:
        return "Qwen (local GGUF)"
    return f"{stem} (local GGUF)"


def _unified_memory_gb() -> float:
    """Best-effort unified/system memory size in GB (Apple Silicon)."""
    try:
        import subprocess

        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
        return int(out) / (1024**3)
    except Exception:  # noqa: BLE001
        try:
            import os

            pages = int(os.sysconf("SC_PHYS_PAGES"))
            page = int(os.sysconf("SC_PAGE_SIZE"))
            return (pages * page) / (1024**3)
        except Exception:  # noqa: BLE001
            return 8.0


def _safe_n_ctx_cap(requested: int, model_path: str) -> int:
    """Cap context so Qwen3-4B + KV fits in unified memory.

    Architecture target is 32K, but 8GB Macs OOM above ~4K with full Metal layers.
    """
    requested = max(2048, int(requested or 4096))
    mem = _unified_memory_gb()
    if mem < 12:
        cap = 4096
    elif mem < 18:
        cap = 8192
    elif mem < 24:
        cap = 16384
    else:
        cap = 32768
    try:
        gb = Path(model_path).expanduser().stat().st_size / (1024**3)
        if gb >= 4.5 and cap > 8192:
            cap = 8192
    except OSError:
        pass
    final = min(requested, cap)
    if final < requested:
        logger.info(
            "n_ctx capped %s → %s (%.0f GB unified memory)",
            requested,
            final,
            mem,
        )
    return final


def _local_answer_max_tokens(utterance: str, *, has_prior: bool = False) -> int:
    """Budget for on-device answers — longer for homework / worked solutions."""
    text = utterance or ""
    long_form = len(text) > 160 or bool(
        re.search(
            r"(?i)\b("
            r"explain|calculate|compute|prove|show\s+work|step\s+by\s+step|"
            r"datapath|instruction|opcode|binary|beq|mips|register|"
            r"write\s+(a\s+)?(code|function|program)|implement|"
            r"what\s+is\s+the\s+new\s+pc|branch\s+is\s+taken"
            r")\b",
            text,
        )
    )
    if long_form:
        return 1536
    return 1024 if has_prior else 768


def _slim_tool_result(result: dict[str, Any], *, limit: int = 1200) -> dict[str, Any]:
    """Shrink tool payloads for ChatML context."""
    slim: dict[str, Any] = {}
    for k, v in (result or {}).items():
        if k == "context" and isinstance(v, str):
            # Web blobs dominate context — keep a short digest for the planner.
            slim[k] = v[:min(limit, 500)] + ("…" if len(v) > min(limit, 500) else "")
        elif isinstance(v, str) and len(v) > limit:
            slim[k] = v[:limit] + "…"
        else:
            slim[k] = v
    return slim


def _summarize_dropped_step(item: dict[str, Any]) -> str:
    """One-line summary of an older tool turn (for compacted planner context)."""
    call = item.get("call") or {}
    result = item.get("result") or {}
    tool = str(call.get("tool") or "tool")
    args = call.get("args") or {}
    ok = result.get("ok")
    status = "ok" if ok else ("fail" if ok is False else "?")
    detail = ""
    if tool == "run_bash":
        cmd = str(args.get("command") or result.get("command") or "")[:80]
        detail = f" `{cmd}`" if cmd else ""
        if result.get("needs_confirm"):
            status = "needs_confirm"
        elif not ok:
            err = str(result.get("error") or result.get("stderr") or "")[:60]
            if err:
                detail += f" err={err}"
    elif tool == "web_search":
        q = str(args.get("query") or result.get("query") or "")[:60]
        detail = f" q={q!r}" if q else ""
    elif tool == "respond":
        detail = " (advice only — goal may still need Mac action)"
    else:
        # Compact args without dumping huge payloads.
        try:
            detail = " " + json.dumps(args, ensure_ascii=False, default=str)[:60]
        except Exception:  # noqa: BLE001
            detail = ""
    return f"- {tool} [{status}]{detail}"


def _compact_tool_history(
    history: list[dict[str, Any]],
    *,
    keep_recent: int = 2,
    compact_after: int = 3,
) -> list[dict[str, Any]]:
    """Drop early tool turns into a short summary so the planner stays on-goal.

    Keeps the user utterance separate (caller). After ``compact_after`` steps,
    older actions become a single digest: what ran, ok/fail, do-not-repeat.
    """
    if not history or len(history) <= compact_after:
        return list(history)

    keep = max(1, keep_recent)
    if len(history) <= keep:
        return list(history)

    dropped = history[:-keep]
    recent = history[-keep:]
    lines = [_summarize_dropped_step(item) for item in dropped]
    # Commands/queries that already failed — nudge the model not to retry them.
    avoid: list[str] = []
    for item in dropped:
        call = item.get("call") or {}
        result = item.get("result") or {}
        tool = call.get("tool")
        if result.get("ok") and not result.get("needs_confirm"):
            continue
        if tool == "run_bash":
            cmd = str((call.get("args") or {}).get("command") or "")[:120]
            if cmd:
                avoid.append(f"run_bash:{cmd}")
        elif tool == "web_search":
            q = str((call.get("args") or {}).get("query") or "")[:80]
            if q:
                avoid.append(f"web_search:{q}")

    summary = (
        f"Earlier steps ({len(dropped)}) compacted — stay on the USER REQUEST; "
        f"do not repeat failed tools:\n" + "\n".join(lines[:12])
    )
    if avoid:
        summary += "\nDo not repeat: " + "; ".join(avoid[:6])

    digest = {
        "call": {"tool": "_prior_steps", "args": {}},
        "result": {
            "ok": True,
            "summary": summary,
            "dropped_count": len(dropped),
        },
    }
    return [digest] + list(recent)


def _history_to_chat_messages(
    utterance: str,
    history: list[dict[str, Any]],
    *,
    runtime: str = "",
    tool_catalog: str = "",
) -> list[dict[str, str]]:
    """Build multi-turn ChatML: prior follow-ups + user ask + compacted tool turns."""
    from memory.user_context import prior_turns_as_messages

    messages: list[dict[str, str]] = []
    # Once we have several tool steps, prior chat turns steal focus from the goal.
    if len(history or []) < 3:
        messages.extend(prior_turns_as_messages(max_turns=4))
    else:
        messages.extend(prior_turns_as_messages(max_turns=1))

    # Compact early actions; always restate the user goal clearly.
    hist = _compact_tool_history(history or [])
    slim_limit = 600 if len(history or []) >= 3 else 1200

    user_parts = []
    if runtime:
        # After a few steps, clock/notes matter less than the goal + last results.
        rt = runtime if len(history or []) < 3 else "\n".join(
            line
            for line in runtime.splitlines()
            if line.startswith("Current local")
            or line.startswith("ISO ")
            or line.startswith("CLOUD HANDOFF")
            or line.startswith("Guidance:")
            or line.startswith("Suggested")
            or line.startswith("- ")
        )
        if rt.strip():
            user_parts.append(f"CONTEXT:\n{rt.strip()}")
    if tool_catalog:
        user_parts.append(tool_catalog)
    goal_label = (
        "USER REQUEST (still the goal — act on THIS, ignore unrelated earlier advice)"
        if len(history or []) >= 2
        else ("Follow-up request (continue the conversation above)" if messages else "User request")
    )
    if messages and len(history or []) < 2:
        user_parts.append(f"{goal_label}: {utterance}")
    else:
        user_parts.append(f"{goal_label}: {utterance}")
    user_parts.append(
        "Choose the next tool as JSON {\"tool\":\"name\",\"args\":{...}} "
        "or finish with {\"tool\":\"respond\",\"args\":{\"text\":\"...\"}}. "
        "Prefer a corrective Mac action over more web search when the goal is local."
    )
    messages.append({"role": "user", "content": "\n\n".join(user_parts)})
    for item in hist:
        call = item.get("call") or {}
        result = item.get("result") or {}
        tool = call.get("tool") or "unknown"
        args = call.get("args") or {}
        # Digest turns are already short.
        if tool == "_prior_steps":
            messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {"tool": "_prior_steps", "args": {}}, ensure_ascii=False
                    ),
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "ok": True,
                            "summary": str(result.get("summary") or "")[:1500],
                        },
                        ensure_ascii=False,
                    ),
                }
            )
            continue
        assistant_payload = {"tool": tool, "args": args}
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    assistant_payload, ensure_ascii=False, default=str
                )[:800],
            }
        )
        messages.append(
            {
                "role": "tool",
                "content": json.dumps(
                    _slim_tool_result(result, limit=slim_limit),
                    ensure_ascii=False,
                    default=str,
                ),
            }
        )
    return messages


def _chat_prompt(
    system: str,
    user: str,
    *,
    model_path: str = "",
    no_think: bool = True,
) -> str:
    """Build a single-turn chat prompt for the active model family."""
    return _messages_prompt(
        system,
        [{"role": "user", "content": user}],
        model_path=model_path,
        no_think=no_think,
    )


def _messages_prompt(
    system: str,
    messages: list[dict[str, str]],
    *,
    model_path: str = "",
    no_think: bool = True,
) -> str:
    """Build a multi-turn prompt including assistant + tool turns for the active family."""
    if _is_gemma(model_path):
        parts = [f"<|turn>system\n{system}<turn|>\n"]
        for msg in messages:
            role = (msg.get("role") or "user").lower()
            content = (msg.get("content") or "").rstrip()
            if role == "assistant":
                parts.append(f"<|turn>model\n{content}<turn|>\n")
            elif role == "tool":
                parts.append(f"<|turn>user\nTool result:\n{content}<turn|>\n")
            else:
                parts.append(f"<|turn>user\n{content}<turn|>\n")
        parts.append("<|turn>model\n")
        return "".join(parts)

    if _is_phi(model_path):
        parts = [f"<|system|>{system}<|end|>"]
        for msg in messages:
            role = (msg.get("role") or "user").lower()
            content = (msg.get("content") or "").rstrip()
            if role == "assistant":
                parts.append(f"<|assistant|>{content}<|end|>")
            elif role == "tool":
                parts.append(f"<|user|>Tool result:\n{content}<|end|>")
            else:
                parts.append(f"<|user|>{content}<|end|>")
        parts.append("<|assistant|>")
        return "".join(parts)

    if _is_llama3(model_path):
        # Llama 3 / 3.2 Instruct header format
        parts = [
            "<|begin_of_text|>"
            f"<|start_header_id|>system<|end_header_id|>\n\n{system}<|eot_id|>"
        ]
        for msg in messages:
            role = (msg.get("role") or "user").lower()
            content = (msg.get("content") or "").rstrip()
            if role == "assistant":
                parts.append(
                    f"<|start_header_id|>assistant<|end_header_id|>\n\n{content}<|eot_id|>"
                )
            elif role == "tool":
                parts.append(
                    "<|start_header_id|>user<|end_header_id|>\n\n"
                    f"Tool result:\n{content}<|eot_id|>"
                )
            else:
                parts.append(
                    f"<|start_header_id|>user<|end_header_id|>\n\n{content}<|eot_id|>"
                )
        parts.append("<|start_header_id|>assistant<|end_header_id|>\n\n")
        return "".join(parts)

    # ChatML (Qwen 2.5 / Qwen3 / SmolLM / default)
    parts = [f"<|im_start|>system\n{system}<|im_end|>\n"]
    last_user_idx = -1
    for i, msg in enumerate(messages):
        if (msg.get("role") or "").lower() == "user":
            last_user_idx = i
    # /no_think is Qwen3-only — do not append for Qwen 2.5, SmolLM, etc.
    apply_no_think = no_think and _is_qwen3(model_path)
    for i, msg in enumerate(messages):
        role = (msg.get("role") or "user").lower()
        content = (msg.get("content") or "").rstrip()
        if role == "assistant":
            parts.append(f"<|im_start|>assistant\n{content}<|im_end|>\n")
        elif role == "tool":
            parts.append(f"<|im_start|>tool\n{content}<|im_end|>\n")
        else:
            body = content
            if (
                apply_no_think
                and i == last_user_idx
                and "/no_think" not in body
                and "/think" not in body
            ):
                body = f"{body}\n/no_think"
            parts.append(f"<|im_start|>user\n{body}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n")
    return "".join(parts)


def _stop_tokens(model_path: str) -> list[str]:
    if _is_gemma(model_path):
        return ["<turn|>", "<|turn>"]
    if _is_phi(model_path):
        return ["<|end|>", "<|endoftext|>", "<|user|>"]
    if _is_llama3(model_path):
        return ["<|eot_id|>", "<|end_of_text|>", "<|start_header_id|>"]
    return ["<|im_end|>", "<|im_start|>"]


def _strip_thinking(text: str) -> str:
    """Remove model thinking blocks; keep the final answer."""
    t = _THINK_RE.sub("", text or "").strip()
    t = _GEMMA_THINK_RE.sub("", t).strip()
    # Unclosed think (truncated generation)
    if "<think>" in t.lower():
        t = re.sub(r"(?is)<think>.*", "", t).strip()
    if "<|channel>thought" in t.lower():
        t = re.sub(r"(?is)<\|channel>thought.*", "", t).strip()
    return t


def _is_garbage_text(text: str) -> bool:
    """True when Metal decode produced nonsense (e.g. pages of '@')."""
    t = (text or "").strip()
    if len(t) < 12:
        return False
    compact = re.sub(r"\s+", "", t)
    if not compact:
        return True
    ch, n = Counter(compact).most_common(1)[0]
    ratio = n / len(compact)
    if ratio >= 0.55 and (ch in "@#$%*=.~-_|/\\" or not ch.isalnum()):
        return True
    if ratio >= 0.8:
        return True
    # Very low alphanumeric content over a long string.
    alnum = sum(1 for c in compact if c.isalnum())
    if len(compact) >= 40 and alnum / len(compact) < 0.15:
        return True
    return False


def _default_flash_attn(settings: dict) -> bool:
    """Flash-attn on Metal + 8GB unified often yields llama_decode -1 / garbage."""
    if "flash_attn" in settings:
        return bool(settings["flash_attn"])
    return _unified_memory_gb() >= 16.0


class LocalIntentParser:
    """Metal-backed intent parser. Lazy-loads the GGUF on first use."""

    def __init__(self, model_path: Optional[str] = None, grammar_path: Optional[str] = None):
        settings = _load_settings()
        raw_model = model_path or settings.get("model_path", _DEFAULT_MODEL)
        self.model_path = str(Path(raw_model).expanduser())
        self.grammar_path = grammar_path or str(
            Path(__file__).resolve().parent / "grammar.gbnf"
        )
        self._llm: Optional[Llama] = None
        # Reliable local defaults (temp 0.2). flash_attn off on <16GB — Metal OOMs.
        self._n_ctx_requested = int(settings.get("n_ctx") or 32768)
        self._temperature = float(
            settings["temperature"] if settings.get("temperature") is not None else 0.2
        )
        self._min_p = float(settings["min_p"] if settings.get("min_p") is not None else 0.05)
        self._flash_attn = _default_flash_attn(settings)
        self._cloud = HybridInferenceEngine(settings.get("cloud"))
        # "local" | "cloud" — which backend produced the last user-facing answer.
        self.last_answer_backend: str = "local"

    def reload_cloud_settings(self) -> dict[str, Any]:
        """Re-read cloud block from settings.json (no GGUF reload)."""
        settings = _load_settings()
        self._cloud.update_settings(settings.get("cloud"))
        return {"ok": True, "cloud": self._cloud.settings}

    def reload(self, model_path: Optional[str] = None) -> dict[str, Any]:
        """Unload the current GGUF (if any) and optionally switch path, then load."""
        if model_path:
            self.model_path = str(Path(model_path).expanduser())
        settings = _load_settings()
        self._n_ctx_requested = int(settings.get("n_ctx") or self._n_ctx_requested or 32768)
        if settings.get("temperature") is not None:
            self._temperature = float(settings["temperature"])
        if settings.get("min_p") is not None:
            self._min_p = float(settings["min_p"])
        self._flash_attn = _default_flash_attn(settings)
        self._cloud.update_settings(settings.get("cloud"))
        old = self._llm
        self._llm = None
        if old is not None:
            try:
                del old
            except Exception:  # noqa: BLE001
                pass
        self._ensure_loaded()
        return {
            "ok": True,
            "model_path": self.model_path,
            "model_loaded": self._llm is not None,
        }

    def _unload(self) -> None:
        old = self._llm
        self._llm = None
        if old is not None:
            try:
                del old
            except Exception:  # noqa: BLE001
                pass

    def _ensure_loaded(self) -> None:
        if self._llm is not None:
            return
        if not Path(self.model_path).exists():
            raise FileNotFoundError(
                f"Model missing at {self.model_path}. "
                "Download a GGUF (e.g. Qwen3-4B-Q4_K_M) into ~/Models."
            )
        heavy = _model_too_heavy_for_mac(self.model_path)
        if heavy:
            raise RuntimeError(heavy)
        if _is_gemma(self.model_path):
            n_ctx = _safe_n_ctx_cap(min(self._n_ctx_requested, 16384), self.model_path)
            n_gpu_layers = 20
        elif _is_phi(self.model_path):
            n_ctx = _safe_n_ctx_cap(min(self._n_ctx_requested, 4096), self.model_path)
            n_gpu_layers = -1
        elif _is_llama3(self.model_path) or _is_smol(self.model_path):
            # Light models on 8GB: keep KV small like Phi.
            n_ctx = _safe_n_ctx_cap(min(self._n_ctx_requested, 4096), self.model_path)
            n_gpu_layers = -1
        elif _is_qwen3(self.model_path):
            n_ctx = _safe_n_ctx_cap(self._n_ctx_requested, self.model_path)
            n_gpu_layers = -1
        else:
            # Qwen 2.5 and other ChatML models — size gate + _safe_n_ctx_cap handle RAM.
            n_ctx = _safe_n_ctx_cap(self._n_ctx_requested, self.model_path)
            n_gpu_layers = -1
        logger.info(
            "Loading GGUF from %s (n_gpu_layers=%s, n_ctx=%s, flash_attn=%s, temp=%s, min_p=%s)",
            self.model_path,
            n_gpu_layers,
            n_ctx,
            self._flash_attn,
            self._temperature,
            self._min_p,
        )
        kwargs: dict[str, Any] = {
            "model_path": self.model_path,
            "n_ctx": n_ctx,
            "n_gpu_layers": n_gpu_layers,
            "verbose": False,
        }
        if self._flash_attn:
            kwargs["flash_attn"] = True
        try:
            self._llm = Llama(**kwargs)
        except TypeError:
            kwargs.pop("flash_attn", None)
            self._llm = Llama(**kwargs)

    def _complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 160,
        temperature: Optional[float] = None,
        min_p: Optional[float] = None,
        stop_extra: Optional[list[str]] = None,
        no_think: bool = True,
        on_token: Optional[Any] = None,
    ) -> str:
        """Run a single-turn chat completion and strip thinking blocks."""
        return self._complete_messages(
            system,
            [{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
            min_p=min_p,
            stop_extra=stop_extra,
            no_think=no_think,
            on_token=on_token,
        )

    def _complete_messages(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 160,
        temperature: Optional[float] = None,
        min_p: Optional[float] = None,
        stop_extra: Optional[list[str]] = None,
        no_think: bool = True,
        on_token: Optional[Any] = None,
    ) -> str:
        """Run a multi-turn ChatML completion (assistant + tool turns)."""
        with _INFER_LOCK:
            return self._complete_messages_locked(
                system,
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                min_p=min_p,
                stop_extra=stop_extra,
                no_think=no_think,
                on_token=on_token,
            )

    def _complete_messages_locked(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 160,
        temperature: Optional[float] = None,
        min_p: Optional[float] = None,
        stop_extra: Optional[list[str]] = None,
        no_think: bool = True,
        on_token: Optional[Any] = None,
    ) -> str:
        self._ensure_loaded()
        assert self._llm is not None
        prompt = _messages_prompt(
            system, messages, model_path=self.model_path, no_think=no_think
        )
        stop = _stop_tokens(self.model_path)
        if stop_extra:
            stop = stop + list(stop_extra)
        # Planner/tool JSON is more reliable slightly cooler than chat sampling.
        temp = self._temperature if temperature is None else temperature
        mp = self._min_p if min_p is None else min_p
        call_kwargs: dict[str, Any] = {
            "max_tokens": max_tokens,
            "temperature": temp,
            "stop": stop,
        }
        if mp is not None and mp > 0:
            call_kwargs["min_p"] = mp

        def _emit(accumulated: str) -> None:
            if on_token is None:
                return
            try:
                on_token(_strip_thinking(accumulated))
            except Exception:  # noqa: BLE001
                pass

        def _run_stream() -> str:
            assert self._llm is not None
            kwargs = dict(call_kwargs)
            kwargs["stream"] = True
            try:
                stream = self._llm(prompt, **kwargs)
            except TypeError:
                kwargs.pop("min_p", None)
                stream = self._llm(prompt, **kwargs)
            pieces: list[str] = []
            for chunk in stream:
                try:
                    piece = chunk["choices"][0].get("text") or ""
                except (KeyError, IndexError, TypeError):
                    piece = ""
                if not piece:
                    continue
                pieces.append(piece)
                # Emit every token — overlay typewriters char-by-char.
                _emit("".join(pieces))
            text = _strip_thinking("".join(pieces).strip())
            if text:
                _emit(text)
            return text

        def _run_blocking() -> str:
            assert self._llm is not None
            try:
                response = self._llm(prompt, **call_kwargs)
            except TypeError:
                call_kwargs.pop("min_p", None)
                response = self._llm(prompt, **call_kwargs)
            return _strip_thinking((response["choices"][0]["text"] or "").strip())

        def _run() -> str:
            if on_token is not None:
                return _run_stream()
            return _run_blocking()

        try:
            text = _run()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "llama_decode" in msg or "decode" in msg:
                logger.warning("Decode failed (%s) — reloading without flash_attn and retrying", exc)
                self._flash_attn = False
                self._unload()
                self._ensure_loaded()
                text = _run()
            else:
                raise

        if _is_garbage_text(text):
            logger.warning("Rejecting garbage model output (%d chars)", len(text))
            raise RuntimeError("model produced garbage output")
        return text

    def extract_intent(self, raw_text: str) -> dict[str, Any]:
        fallback = {
            "action": "search_fallback",
            "target": "",
            "raw_query": raw_text,
        }
        heuristic = _heuristic_intent(raw_text)
        if heuristic:
            trace_step(
                "intent_heuristic",
                input=raw_text,
                output_json=heuristic,
            )
            return heuristic

        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("%s; using search_fallback", exc)
            trace_step("intent_error", error=str(exc), output_json=fallback)
            return fallback

        system_prompt = (
            "You are a macOS intent parsing engine. Reply with ONLY one JSON object, "
            "no markdown, no extra text. Keys: action, target, raw_query. "
            "action must be one of: answer, browse, open_site, open_app, workflow, search_fallback. "
            "Use browse when the user needs the web or live info "
            "(search for, next game, live scores, latest news, look up). "
            "Use answer only for general knowledge that does not need the internet. "
            "Use open_site for website orders (target=hostname or topic). "
            "Use open_app for native app orders (target=app name). "
            "Use search_fallback as a synonym of browse. "
            "raw_query must be the original spoken text."
        )
        assert self._llm is not None
        try:
            text = self._complete(system_prompt, raw_text, max_tokens=128, temperature=0.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Intent LLM failed (%s); using fallback", exc)
            trace_step("intent_llm_error", system_prompt=system_prompt, error=str(exc))
            return fallback

        parsed = _parse_intent_json(text, raw_text)
        trace_step(
            "intent_llm",
            system_prompt=system_prompt,
            user=raw_text,
            raw_output=text,
            output_json=parsed or fallback,
        )
        if parsed:
            return parsed
        logger.warning(
            "Failed to parse model JSON (%r); using fallback", (text or "")[:120]
        )
        return fallback

    def match_purpose_site(
        self, utterance: str, sites: List[dict[str, Any]]
    ) -> Optional[int]:
        """Pick the best purpose_sites id for an utterance, or None."""
        if not utterance or not sites:
            return None
        # Skip purpose matching for pure knowledge questions — not browse requests.
        if _heuristic_intent(utterance) and not needs_browser(utterance):
            return None
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("Purpose match skipped: %s", exc)
            return None

        catalog_lines = []
        valid_ids: List[int] = []
        for site in sites:
            site_id = int(site["id"])
            valid_ids.append(site_id)
            purpose = (site.get("purpose") or "")[:160]
            catalog_lines.append(f"{site_id}: {purpose}")
        catalog = "\n".join(catalog_lines)

        grammar_src = _build_purpose_grammar(valid_ids)
        try:
            purpose_grammar = LlamaGrammar.from_string(grammar_src)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Purpose grammar failed: %s", exc)
            return None

        system_prompt = (
            "You map a spoken request to the best matching website purpose id. "
            "Reply with SITE=<id> for the closest semantic match, or SITE=null if none fit. "
            "Match meaning, not exact words."
        )
        user_prompt = (
            f"Purposes:\n{catalog}\n\nSpoken request: {utterance}\n\nAnswer:"
        )
        prompt = _chat_prompt(
            system_prompt, user_prompt, model_path=self.model_path, no_think=True
        )

        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=16,
                grammar=purpose_grammar,
                temperature=0.0,
                stop=_stop_tokens(self.model_path),
            )
            text = (response["choices"][0]["text"] or "").strip()
            if not text.startswith("SITE="):
                return None
            value = text[len("SITE=") :].strip()
            if value == "null":
                return None
            site_id = int(value)
            if site_id in set(valid_ids):
                return site_id
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse purpose match output: %s", exc)
            return None

    def generate_cloud_envelope(self, utterance: str) -> dict[str, Any]:
        """Ask cloud for a structured final/handoff envelope (non-streaming JSON).

        Returns dict: final, answer, guidance, commands.
        On cloud failure / empty, returns a soft final refusal (caller may fall back).
        """
        from memory.user_context import format_followup_block, get_prior_turns

        empty = {
            "final": True,
            "answer": "",
            "guidance": "",
            "commands": [],
        }
        if not self._cloud.should_use_cloud(utterance):
            return empty

        runtime = build_runtime_context(include_followup_text=False)
        prior = get_prior_turns()
        system = (
            "You are MacAgent's cloud planner for a macOS app that can run local tools. "
            "Reply with ONLY one JSON object (no markdown fences, no prose outside JSON):\n"
            '{"final": true|false, "answer": "...", "guidance": "...", "commands": ["..."]}\n\n'
            "Rules:\n"
            "- final=true when you can fully answer from knowledge / prior chat "
            "(explanations, code snippets, general advice) with NO need to inspect THIS Mac "
            "and NO need for live web news.\n"
            "- final=false when the user needs live state from THIS Mac "
            "(files, Downloads, disk usage, processes, apps, paths, 'which is that', "
            "open/delete a local file). Then put a clear plan in guidance and "
            "0–5 concrete macOS bash commands in commands.\n"
            "- For live web research (search for announcements/news/releases, citations, "
            "last N hours): set final=false, leave commands EMPTY, and put "
            "'Use web_search then summarize with citations' in guidance. "
            "NEVER emit `open https://…` — opening Safari is not searching.\n"
            "- commands must be BSD/macOS safe (no GNU find -printf). Prefer: "
            "ls -lt ~/Downloads | head, du -sh ~/Downloads/*, ps aux | head, "
            "find ~/Documents -maxdepth 4 …\n"
            "- Never claim you already ran local tools. Never use sudo.\n"
            "- When final=true, put the user-facing reply in answer; guidance/commands empty.\n"
            "- When final=false, answer may be a short status like "
            "\"Checking on your Mac…\"; guidance must tell the local agent what to do."
        )
        user = f"CONTEXT:\n{runtime}\n\n"
        thread = format_followup_block()
        if thread:
            user += f"{thread}\n\n"
        if prior:
            user += (
                "This may be a follow-up — resolve it/that/this from the thread above.\n\n"
            )
        user += f"User question: {utterance}"

        try:
            raw = self._cloud.complete(
                system,
                user,
                utterance,
                max_tokens=1200,
                temperature=0.2,
                on_token=None,
                json_mode=True,
            )
            env = parse_cloud_envelope(raw)
            self.last_answer_backend = "cloud"
            trace_step(
                "generate_cloud_envelope",
                backend="cloud",
                final=env.get("final"),
                answer=(env.get("answer") or "")[:500],
                guidance=(env.get("guidance") or "")[:500],
                commands=env.get("commands") or [],
                raw_output=(raw or "")[:2000],
            )
            return env
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_cloud_envelope failed: %s", exc)
            trace_step("generate_cloud_envelope_error", error=str(exc))
            return empty

    def generate_answer(
        self, utterance: str, *, on_token: Optional[Any] = None
    ) -> str:
        """Chat reply. May use cloud for general knowledge; system tasks stay local."""
        from memory.user_context import (
            format_followup_block,
            get_prior_turns,
            prior_turns_as_messages,
        )

        fallback = "I could not generate an answer right now."
        prior = get_prior_turns()
        # Planner already embeds turns as ChatML; here avoid duplicating huge text in CONTEXT.
        runtime = build_runtime_context(include_followup_text=False)
        model_name = _model_display_name(self.model_path)
        follow_hint = (
            "You are continuing an active conversation. "
            "Resolve pronouns (it/that/this/them/the file) from your earlier replies. "
            if prior
            else ""
        )
        local_system = (
            "You are MacAgent, a local macOS assistant. "
            f"You run on-device using {model_name}. "
            "If asked which model you use, name that model. "
            f"{follow_hint}"
            "Use CONTEXT when relevant. "
            "Be concise for small talk; for homework, worked solutions, or code, "
            "answer fully and finish every sentence. "
            "If the question needs live or web data (scores, schedules, news, prices), "
            "reply with exactly: NEED_BROWSER "
            "Do not invent live sports results. Do not say you lack internet access."
        )
        cloud_system = (
            "You are MacAgent, a capable macOS assistant using a cloud language model. "
            f"{follow_hint}"
            "Use CONTEXT when relevant. "
            "Answer fully and clearly — include code, steps, and detail when useful. "
            "Do not artificially shorten answers. "
            "If the question needs live or web data (scores, schedules, news, prices), "
            "reply with exactly: NEED_BROWSER "
            "Do not invent live sports results. Do not say you lack internet access."
        )

        # Cloud: single string with an explicit thread block (API is system+user).
        cloud_user = f"CONTEXT:\n{runtime}\n\n"
        thread = format_followup_block()
        if thread:
            cloud_user += f"{thread}\n\n"
        cloud_user += f"User question: {utterance}"

        if self._cloud.should_use_cloud(utterance):
            try:
                out = self._cloud.complete(
                    cloud_system,
                    cloud_user,
                    utterance,
                    max_tokens=None,
                    temperature=0.4,
                    on_token=on_token,
                )
                out = out or fallback
                self.last_answer_backend = "cloud"
                trace_step(
                    "generate_answer",
                    backend="cloud",
                    system_prompt=cloud_system,
                    user_prompt=cloud_user,
                    raw_output=out,
                )
                return out
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cloud generate_answer failed; falling back local: %s", exc)
                trace_step("generate_answer_cloud_fallback", error=str(exc))

        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("Answer skipped: %s", exc)
            self.last_answer_backend = "local"
            return fallback

        assert self._llm is not None
        try:
            # Local: real multi-turn ChatML so the small model keeps the thread.
            messages = prior_turns_as_messages(max_turns=4)
            if messages:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"CONTEXT:\n{runtime}\n\n"
                            f"Follow-up: {utterance}"
                        ),
                    }
                )
            else:
                messages = [
                    {
                        "role": "user",
                        "content": f"CONTEXT:\n{runtime}\n\nUser question: {utterance}",
                    }
                ]
            max_tok = _local_answer_max_tokens(utterance, has_prior=bool(prior))
            text = self._complete_messages(
                local_system,
                messages,
                max_tokens=max_tok,
                temperature=0.4,
                on_token=on_token,
            )
            out = text or fallback
            self.last_answer_backend = "local"
            trace_step(
                "generate_answer",
                backend="local",
                max_tokens=max_tok,
                system_chars=len(local_system),
                user_prompt=(messages[-1]["content"] if messages else utterance)[:800],
                prior_turns=len(prior),
                raw_output=out[:4000],
            )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to generate answer: %s", exc)
            trace_step("generate_answer_error", error=str(exc))
            self.last_answer_backend = "local"
            return fallback

    def answer_about_user(self, utterance: str) -> str:
        """Summarize Preferences / user notes for “what do you know about me?”."""
        notes = load_user_notes_for_llm()
        if not notes.strip():
            return (
                "I don't have saved notes about you yet. "
                "Add profile details in Preferences → User context and I'll remember them."
            )
        fallback = _notes_extractive_summary(notes)
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("About-user answer skipped: %s", exc)
            return fallback

        system_prompt = (
            "You are MacAgent. The user asked what you know about them. "
            "Answer ONLY from CONTEXT notes. Speak in second person (you/your). "
            "Give a clear 4–8 sentence summary of the most important facts "
            "(name, school/work, location, interests, recent plans). "
            "If notes have facts, you MUST use them — never say you lack information. "
            "Do not invent facts that are not in the notes."
        )
        user_prompt = (
            f"CONTEXT notes:\n{notes[:5000]}\n\n"
            f"User question: {utterance}\n\n"
            "Summary of what I know about you:"
        )
        assert self._llm is not None
        try:
            text = self._complete(
                system_prompt, user_prompt, max_tokens=320, temperature=0.2
            )
            out = text or fallback
            if _is_empty_refusal(out):
                out = fallback
            trace_step(
                "answer_about_user",
                backend="local",
                system_prompt=system_prompt,
                user_prompt=user_prompt[:2000],
                raw_output=out,
            )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to summarize user notes: %s", exc)
            trace_step("answer_about_user_error", error=str(exc))
            return fallback

    def answer_from_search(
        self,
        utterance: str,
        search_context: str,
        *,
        on_token: Optional[Any] = None,
        force_cloud: Optional[bool] = None,
    ) -> str:
        """Answer using search hits + fetched page text (grounded; less hallucination)."""
        extractive = _search_extractive_summary(search_context)
        fallback = extractive or (
            "I searched the web but could not form a reliable answer from the pages."
        )
        runtime = build_runtime_context()
        # Scraped page bodies are longer / noisier — prefer cloud when configured.
        scraped = bool(
            re.search(r"(?i)Page content from\s+https?://", search_context or "")
        )
        prefer_cloud = (
            force_cloud
            if force_cloud is not None
            else (scraped and self._cloud.cloud_ready())
        )
        shared_rules = (
            "You are MacAgent. Answer the user using the web context "
            "(search hits and page content) plus CONTEXT (time and user notes). "
            "Rules:\n"
            "- Treat search-hit titles and snippets as valid evidence — summarize them.\n"
            "- If the question is about the user (“me”/“about me”) and hits describe "
            "a person matching the name in CONTEXT notes, answer about that person "
            "in second person (you/your).\n"
            "- Prefer concrete facts (school, job, location, projects, prices, model names) "
            "from the hits.\n"
            "- For compare/pricing questions, list every model/price figure present in the "
            "web context. Partial answers are OK — say what is known vs missing.\n"
            "- Only refuse if the web context is empty or clearly unrelated.\n"
            "- Do not invent dates, scores, prices, or names missing from context.\n"
            "- Do NOT tell the user to open a browser or run commands to look this up — "
            "you already have the page content.\n"
        )
        local_system = shared_rules + "- Reply in 2–8 short sentences."
        cloud_system = (
            shared_rules
            + "- Answer fully — include detail, lists, and code when useful. "
            "Do not artificially shorten answers."
        )
        ctx_local = (search_context or "")[:6000]
        ctx_cloud = (search_context or "")[:24000]
        user_local = (
            f"CONTEXT:\n{runtime}\n\n"
            f"User question: {utterance}\n\n"
            f"Web context:\n{ctx_local}\n\n"
            "Write a helpful answer from the web context (and notes if relevant). "
            "Use any prices, model names, or comparison facts present. "
            "Do not claim the sources lack information if they contain relevant facts:"
        )
        user_cloud = (
            f"CONTEXT:\n{runtime}\n\n"
            f"User question: {utterance}\n\n"
            f"Web context:\n{ctx_cloud}\n\n"
            "Write a helpful answer from the web context (and notes if relevant). "
            "Use any prices, model names, or comparison facts present. "
            "Do not claim the sources lack information if they contain relevant facts:"
        )

        use_cloud = prefer_cloud or self._cloud.should_use_cloud(utterance)
        if use_cloud and self._cloud.cloud_ready():
            try:
                out = self._cloud.complete(
                    cloud_system,
                    user_cloud,
                    utterance,
                    max_tokens=None,
                    temperature=0.2,
                    on_token=on_token,
                    force=bool(prefer_cloud),
                )
                out = out or fallback
                if _is_empty_refusal(out) and extractive:
                    out = extractive
                self.last_answer_backend = "cloud"
                trace_step(
                    "answer_from_search",
                    backend="cloud",
                    forced=bool(prefer_cloud),
                    scraped=scraped,
                    system_prompt=cloud_system,
                    user_prompt=user_cloud,
                    web_context_chars=len(ctx_cloud),
                    raw_output=out,
                )
                return out
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Cloud answer_from_search failed; falling back local: %s", exc
                )
                trace_step("answer_from_search_cloud_fallback", error=str(exc))

        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("Search answer skipped: %s", exc)
            self.last_answer_backend = "local"
            return fallback

        assert self._llm is not None
        try:
            text = self._complete(
                local_system,
                user_local,
                max_tokens=256,
                temperature=0.2,
                on_token=on_token,
            )
            out = text or fallback
            # Small models often refuse “about me” even when LinkedIn/snippets are rich.
            if _is_empty_refusal(out) and extractive:
                logger.info("answer_from_search: replacing empty refusal with extractive")
                out = extractive
            self.last_answer_backend = "local"
            trace_step(
                "answer_from_search",
                backend="local",
                scraped=scraped,
                system_prompt=local_system,
                user_prompt=user_local,
                web_context_chars=len(ctx_local),
                raw_output=out,
            )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to summarize search: %s", exc)
            trace_step("answer_from_search_error", error=str(exc))
            self.last_answer_backend = "local"
            return fallback

    def answer_from_command(
        self, utterance: str, command: str, stdout: str
    ) -> str:
        """Turn raw shell/python stdout into a user-facing answer for the question."""
        fallback = (stdout or "").strip()[:2500] or "Done."
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("Command answer skipped: %s", exc)
            return fallback

        system_prompt = (
            "You are MacAgent. The user asked a question; a shell/python command already ran. "
            "Rewrite the command output into a clear answer for THAT question. "
            "Rules: do not dump raw ls columns (permissions, owner, size numbers) unless asked; "
            "for file lists, use a short numbered list of names with dates when useful; "
            "do not invent files that are not in the output; keep it concise (under ~12 lines). "
            "NEVER tell the user to run a command themselves — MacAgent already ran it "
            "(or the output shows it failed). If output is an error, say what failed plainly."
        )
        out = (stdout or "")[:4500]
        user_prompt = (
            f"User question: {utterance}\n\n"
            f"Command: {command}\n\n"
            f"Command output:\n{out}\n\n"
            "Formatted answer:"
        )
        assert self._llm is not None
        try:
            text = self._complete(
                system_prompt, user_prompt, max_tokens=280, temperature=0.1
            )
            trace_step(
                "answer_from_command",
                user=utterance,
                command=command[:300],
                raw_output=text[:1000],
            )
            return text or fallback
        except Exception as exc:  # noqa: BLE001
            logger.warning("answer_from_command failed: %s", exc)
            return fallback

    def plan_tool_call(
        self,
        utterance: str,
        history: list[dict[str, Any]],
        tool_catalog: str,
    ) -> str:
        """Ask the model for the next tool call as a single JSON object."""
        soft = {
            "tool": "respond",
            "args": {
                "text": (
                    "Hey — I'm MacAgent. Ask me anything, or say “what can you do?”"
                )
            },
        }
        hard = {
            "tool": "respond",
            "args": {
                "text": (
                    "I couldn't plan that step (model decode failed). "
                    "Stay on Qwen3-4B, or try again with a shorter ask."
                )
            },
        }
        # Casual chat should never surface a planner failure.
        utt = (utterance or "").strip()
        if re.match(
            r"(?i)^\s*("
            r"yo+|hey+|hi+|hello+|sup+|howdy|thanks|thank\s+you|thx|"
            r"ok|okay|cool|nice|great|awesome|lol|haha|bro+|dude|"
            r"good\s+(morning|afternoon|evening|night)|"
            r"how\s+are\s+(you|ya|u)|how('?s|\s+is)\s+it\s+going|"
            r"how('?s|\s+is)\s+going|hows\s+going|how\s+goes\s+it|"
            r"what'?s\s+up|wassup|whats\s+going\s+on|what'?s\s+going\s+on"
            r")[\s!.?]*$",
            utt,
        ):
            fallback = json.dumps(soft)
        else:
            fallback = json.dumps(hard)
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("Tool plan skipped: %s", exc)
            return json.dumps(
                {
                    "tool": "respond",
                    "args": {
                        "text": f"Model isn't ready: {exc}",
                        "goal_done": True,
                    },
                }
            )

        runtime = build_runtime_context(include_followup_text=False)
        messages = _history_to_chat_messages(
            utterance,
            history,
            runtime=runtime,
            tool_catalog=tool_catalog,
        )

        # Keep this short — every token here steals budget from tool JSON / answers.
        system_prompt = (
            "MacAgent macOS planner. Model: "
            f"{_model_display_name(self.model_path)}. "
            "Reply with ONLY one JSON tool call: {\"tool\":\"name\",\"args\":{...}}. "
            "USER REQUEST is the goal — older tool turns may be compacted; "
            "do not repeat failed tools; take the next useful Mac step. "
            "Match the user intent; never copy unrelated catalog examples. "
            "Follow-ups: resolve it/that/this from the latest Assistant path/reply. "
            "If CONTEXT has CLOUD HANDOFF PLAN, run those bash commands. "
            "Knowledge/advice Qs (incl. 'open source') → web_search then respond — "
            "not open_app. Only open/launch when the user explicitly asks. "
            "Wifi/bt/mute/dark→control_mac. Quit app→manage_system_resources kill. "
            "pmset ONLY for sleep/display/battery. "
            "One tool per step; compound asks need separate steps. "
            "After find/ls, if they asked to open/delete/move, do that next. "
            "Live facts/prices/news/latest versions → web_search first, then "
            "run_bash for THIS Mac (e.g. python3 --version). Never curl websites in bash. "
            "Local file cleanup on Desktop/Downloads → run_bash, not web how-tos. "
            "After open_app, if user also asked to type/click/read the screen → "
            "ui_type / ui_click / ui_key then ui_snapshot — do not stop at launch. "
            "If you researched online but the user also asked to check THIS Mac "
            "(installed version, terminal), run_bash next — never finish with "
            "'you can run `python3 --version`' advice. "
            "Greetings → respond. Never shut down/restart/empty trash/rm/ui_* unless asked. "
            "Read prior tool results before the next step."
        )
        assert self._llm is not None
        try:
            text = self._complete_messages(
                system_prompt,
                messages,
                max_tokens=160,
                temperature=0.1,
            )
            # Never dump the full system prompt into Debug — it crashes the prefs UI.
            trace_step(
                "plan_tool_call",
                system_chars=len(system_prompt),
                messages=len(messages),
                history_len=len(history),
                raw_output=(text or "")[:1500],
            )
            return text or fallback
        except Exception as exc:  # noqa: BLE001
            logger.warning("plan_tool_call failed: %s", exc)
            trace_step("plan_tool_call_error", error=str(exc))
            return fallback

    def generate_python(self, utterance: str) -> str:
        """Write a short Python script that prints the answer to utterance."""
        fallback = "print('unable to generate code')"
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("generate_python skipped: %s", exc)
            return fallback

        system_prompt = (
            "You write short Python 3 scripts. "
            "Reply with ONLY executable Python code — no markdown fences, no explanation. "
            "The script must print the final answer with print(...). "
            "Use only the standard library. No network, no files outside /tmp, no subprocess."
        )
        user_prompt = (
            f"Write a Python script that solves this and prints the result:\n{utterance}"
        )
        assert self._llm is not None
        try:
            text = self._complete(
                system_prompt, user_prompt, max_tokens=320, temperature=0.1
            )
            text = _strip_code_fences(text)
            trace_step("generate_python", user=utterance, raw_output=text[:1000])
            return text or fallback
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_python failed: %s", exc)
            return fallback

    def generate_bash(self, utterance: str) -> str:
        """Write a short bash command for a local macOS file/shell task.

        Prefers cloud (when configured) for correct quoting / multi-step shell,
        but only sends a scrubbed ask — no absolute home paths, username, or IPs.
        The returned command uses ``~`` / ``$HOME``; we localize before run.
        """
        text_in = (utterance or "").strip()
        fallback = _bash_fallback_for_ask(text_in)

        # Local version checks — never curl the web from bash.
        if re.search(r"(?i)\bpython\b", text_in) and re.search(
            r"(?i)\b(version|installed|out\s+of\s+date|up\s+to\s+date)\b",
            text_in,
        ):
            return "python3 --version"

        # Deterministic create-folder asks — don't risk Downloads / path hallucination.
        deterministic = _deterministic_create_command(text_in)
        if not deterministic:
            deterministic = _deterministic_zip_command(text_in)
        if not deterministic:
            deterministic = _deterministic_reachability_command(text_in)
        if deterministic:
            trace_step(
                "generate_bash",
                user=utterance,
                backend="deterministic",
                raw_output=deterministic[:1000],
            )
            return deterministic

        system_prompt = (
            "You write ONE short bash command for macOS (bash/zsh). "
            "Reply with ONLY the command — no markdown, no explanation, no leading $. "
            "Privacy: the ask is generic. Use ONLY ~ or $HOME for home paths — "
            "NEVER /Users/<name>, never real usernames, never absolute machine paths. "
            "Match the user's folder/file names AND parent paths exactly "
            "(if they said ~/Documents/…, the command MUST use ~/Documents/… — "
            "never drop Documents/Desktop/Downloads and create under ~/). "
            "CRITICAL quoting: names with spaces, &, (), [], or quotes must be wrapped "
            "in SINGLE quotes so the shell does not split them "
            "(e.g. mkdir -p ~/Documents/'Project Alpha & Beta (2026)'). "
            "Never break a path across mismatched quotes. "
            "Prefer: ls, find -maxdepth (BSD find — never -printf), zip, mkdir -p, "
            "printf/echo, touch, mv, cp, rm, open, open -R (reveal in Finder), "
            "shasum -a 256, md5 -r. "
            "Do the FULL ask in one command when possible. "
            "No curl/wget, no sudo, no rm -rf /, no disk erase. "
            "Only list ~/Downloads when the user asked about downloads."
        )
        # Scrub local identity before the model sees the ask (cloud or local).
        from llm.cloud import sanitize_for_cloud, restore_placeholders
        from pathlib import Path as _Path

        generic_ask, restore_map = sanitize_for_cloud(text_in)
        try:
            home = str(_Path.home())
            if home and home in generic_ask:
                generic_ask = generic_ask.replace(home, "~")
        except Exception:  # noqa: BLE001
            pass
        user_prompt = (
            "Write one portable macOS bash command (paths with ~ only) "
            "that prints useful stdout for this ask:\n"
            f"{generic_ask}"
        )

        text = ""
        used_cloud = False
        # Local-first for shell: cloud often returns empty on scrubbed Mac path asks,
        # which only adds a scary Debug error before we fall back anyway.
        try:
            self._ensure_loaded()
            assert self._llm is not None
            text = self._complete(
                system_prompt, user_prompt, max_tokens=320, temperature=0.1
            )
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("generate_bash local unavailable: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_bash local failed: %s", exc)

        text = _strip_code_fences(text or "")
        for line in (text or "").splitlines():
            line = line.strip().lstrip("$").strip()
            if line and not line.lower().startswith("bash"):
                text = line
                break
        if text:
            text = restore_placeholders(text, restore_map)
            text = _localize_bash_command(text)
            text = _repair_dropped_home_folder(text, text_in)

        need_cloud = (not (text or "").strip()) or _bash_looks_broken_cmd(text or "")
        if need_cloud and self._cloud.cloud_ready():
            try:
                cloud_text = self._cloud.complete(
                    system_prompt,
                    user_prompt,
                    utterance,
                    max_tokens=400,
                    temperature=0.1,
                    force=True,
                )
                cloud_text = _strip_code_fences(cloud_text or "")
                for line in cloud_text.splitlines():
                    line = line.strip().lstrip("$").strip()
                    if line and not line.lower().startswith("bash"):
                        cloud_text = line
                        break
                cloud_text = restore_placeholders(cloud_text, restore_map)
                cloud_text = _localize_bash_command(cloud_text)
                cloud_text = _repair_dropped_home_folder(cloud_text, text_in)
                if (cloud_text or "").strip() and not _bash_looks_broken_cmd(cloud_text):
                    text = cloud_text
                    used_cloud = True
                else:
                    # Empty/broken cloud — quiet skip (not a Debug error).
                    logger.info("generate_bash: cloud returned empty/broken; keeping local")
                    trace_step("generate_bash_cloud_skip", reason="empty_or_broken")
            except Exception as exc:  # noqa: BLE001
                # Soft-skip empty responses; only surface real HTTP/config failures.
                msg = str(exc)
                if re.search(r"(?i)empty|no choices", msg):
                    logger.info("generate_bash: cloud empty; keeping local")
                    trace_step("generate_bash_cloud_skip", reason="empty")
                else:
                    logger.warning("cloud generate_bash failed; keeping local: %s", exc)
                    trace_step("generate_bash_cloud_fallback", error=msg[:300])

        if not (text or "").strip():
            return fallback

        if re.search(r"(?i)invoice", text or "") and not re.search(
            r"(?i)invoice", text_in
        ):
            return fallback
        text = re.sub(r"(?i)\bsha256sum\b", "shasum -a 256", text or "")
        text = re.sub(r"(?i)\bmd5sum\b", "md5 -r", text)
        if re.search(r"(?i)\b(curl|wget)\b", text or ""):
            if re.search(r"(?i)\bpython\b", text_in):
                return "python3 --version"
            # Allow curl only for explicit reachability / latency checks.
            reach = _deterministic_reachability_command(text_in)
            if reach:
                return reach
            return fallback
        if _bash_looks_broken_cmd(text):
            logger.warning("generate_bash produced broken cmd; using fallback")
            return fallback
        try:
            from tools.run_bash import quote_paths_with_spaces

            text = quote_paths_with_spaces(text)
        except Exception:  # noqa: BLE001
            pass
        # Never ship a Downloads listing for an unrelated ask.
        if (
            text
            and re.search(r"(?i)~/Downloads", text)
            and not re.search(r"(?i)\bdownl", text_in)
        ):
            logger.warning("generate_bash ignored unrelated Downloads cmd")
            return fallback
        trace_step(
            "generate_bash",
            user=utterance,
            backend="cloud" if used_cloud else "local",
            raw_output=(text or "")[:1000],
        )
        self.last_answer_backend = "cloud" if used_cloud else "local"
        return text or fallback


    def check_goal_done(
        self,
        utterance: str,
        history_summary: str,
        candidate: str,
        *,
        is_action_request: bool = False,
    ) -> dict[str, Any]:
        """Critic: did the candidate answer / tool history finish the user's goal?"""
        fallback = (
            {"done": False, "reason": "action may be incomplete", "next_hint": "run_bash or next tool"}
            if is_action_request
            else {"done": True, "reason": "assumed done", "next_hint": ""}
        )
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("check_goal_done skipped: %s", exc)
            return fallback

        system_prompt = (
            "You verify whether a Mac assistant finished the user's request. "
            "Reply with ONLY JSON: "
            '{"done":true|false,"reason":"…","next_hint":"tool or action to try next"}. '
            "done=true if the user asked an informational question and the candidate answers it "
            "(even when words like open-source appear — that is NOT an open-file request). "
            "done=false when the user asked MacAgent to check/do something on THIS Mac "
            "(installed version, terminal, files, open+type, delete) and that local step "
            "has not run yet — telling them 'you can run `cmd`' is NOT done; next_hint should "
            "be run_bash with that command. "
            "done=false only when the user clearly asked for a Mac side-effect "
            "(delete/remove/open-app/launch/move/empty trash/change setting) that has not happened. "
            "done=false if tools ran but the candidate answer does not address the question "
            "(quality fail — suggest a corrective next_hint). "
            "done=false if a folder/file was created under the wrong parent "
            "(e.g. user asked for ~/Documents/… but tools used ~/… without Documents). "
            "done=true if the Mac action is complete or a clarifying question is appropriate. "
            "When done=false and the fix is obvious, next_hint must be the exact run_bash "
            "command to run next (not vague advice)."
        )
        user_prompt = (
            f"User request: {utterance}\n\n"
            f"Tool history:\n{history_summary or '(none)'}\n\n"
            f"Candidate answer:\n{(candidate or '')[:800]}\n\n"
            "JSON:"
        )
        assert self._llm is not None
        try:
            text = self._complete(
                system_prompt, user_prompt, max_tokens=120, temperature=0.0
            )
            data = None
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        data = None
            if not isinstance(data, dict):
                trace_step("check_goal_done_parse_fail", raw_output=text[:500])
                return fallback
            done = bool(data.get("done"))
            out = {
                "done": done,
                "reason": str(data.get("reason") or ""),
                "next_hint": str(data.get("next_hint") or ""),
            }
            trace_step("check_goal_done", raw_output=text[:500], parsed=out)
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("check_goal_done failed: %s", exc)
            trace_step("check_goal_done_error", error=str(exc))
            return fallback

def _localize_bash_command(command: str) -> str:
    """Map foreign /Users/… paths from cloud advice onto this Mac's home."""
    cmd = command or ""
    if not cmd:
        return cmd
    from pathlib import Path as _Path

    home = str(_Path.home())
    # /Users/someOtherName/... → ~/...
    cmd = re.sub(r"/Users/[^/\s\"']+", "~", cmd)
    cmd = re.sub(r"/home/[^/\s\"']+", "~", cmd)
    if home:
        cmd = cmd.replace(home, "~")
    return cmd


def _bash_looks_broken_cmd(command: str) -> bool:
    """Local copy of broken-cmd checks for generate_bash (no agent_loop import)."""
    cmd = (command or "").strip()
    if not cmd or len(cmd) > 700:
        return True
    if re.search(r"[|\\&;]\s*$", cmd) or cmd.endswith("\\"):
        return True
    if cmd.count("'") % 2 == 1 or cmd.count('"') % 2 == 1:
        return True
    # curl with URL on the next line (planner newline inside -w string / after it).
    if re.search(r"(?i)\bcurl\b", cmd) and re.search(
        r"(?i)\n\s*https?://", cmd
    ):
        return True
    if re.search(r"(?i)\bcurl\b", cmd) and not re.search(
        r"(?i)https?://|\s+[a-z0-9.-]+\.[a-z]{2,}(?:/|\s|$)", cmd
    ):
        return True
    # Classic broken join: "~/Documents/"Name with spaces"
    # (not valid adjacent segments like "Folder"/"file.txt")
    if _has_broken_path_quote_join(cmd):
        return True
    # Unquoted bare & (not &&) — almost always a botched name like "A & B".
    if re.search(r"(?<!&)&(?!&)", cmd) and not re.search(
        r"(?i)>\s*&|<&|2>&1", cmd
    ):
        if not _ampersand_only_inside_quotes(cmd):
            return True
    if len(re.findall(r"(?i)\bfind\b", cmd)) >= 3:
        return True
    if len(re.findall(r"(?i)\bxargs\b", cmd)) >= 2 and re.search(
        r"(?i)\bfind\b", cmd
    ):
        return True
    return False


def _has_broken_path_quote_join(command: str) -> bool:
    """True for ``\"~/Documents/\"Name with spaces`` mid-path quote closes.

    Quote-state aware so ``\"Folder\"/\"file.txt\"`` is allowed.
    """
    quote: str | None = None
    i = 0
    n = len(command or "")
    while i < n:
        ch = command[i]
        if quote:
            if ch == quote and (i == 0 or command[i - 1] != "\\"):
                prev = command[i - 1] if i > 0 else ""
                nxt = command[i + 1] if i + 1 < n else ""
                if prev == "/" and nxt and nxt not in " \t\n|;&=<>)$'\"":
                    return True
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        i += 1
    return False


_HOME_FOLDER_NAMES = (
    "Documents",
    "Desktop",
    "Downloads",
    "Movies",
    "Music",
    "Pictures",
)


def _home_folder_from_ask(utterance: str) -> str | None:
    """Return Documents/Desktop/… when the ask targets that home folder."""
    text = utterance or ""
    # Prefer explicit ~/Documents (or "in Documents named …").
    m = re.search(
        r"(?i)(?:in|under|into|on)\s+(?:~/|\$HOME/)?("
        + "|".join(_HOME_FOLDER_NAMES)
        + r")\b",
        text,
    )
    if m:
        for name in _HOME_FOLDER_NAMES:
            if name.lower() == m.group(1).lower():
                return name
    m = re.search(
        r"(?i)(?:~/|\$HOME/)(" + "|".join(_HOME_FOLDER_NAMES) + r")\b",
        text,
    )
    if m:
        for name in _HOME_FOLDER_NAMES:
            if name.lower() == m.group(1).lower():
                return name
    return None


def _repair_dropped_home_folder(command: str, utterance: str) -> str:
    """If the ask required ~/Documents but the cmd used ~/Name, inject Documents."""
    cmd = command or ""
    folder = _home_folder_from_ask(utterance)
    if not cmd or not folder:
        return cmd
    if re.search(rf"(?i)(?:~/|\$HOME/){re.escape(folder)}\b", cmd):
        return cmd
    # Only rewrite creates / opens that landed directly under ~.
    if not re.search(r"(?i)\b(mkdir|touch|echo|printf|open)\b", cmd):
        return cmd

    known = "|".join(re.escape(n) for n in _HOME_FOLDER_NAMES)

    def _inject_quoted(m: re.Match[str]) -> str:
        q, name = m.group(1), m.group(2)
        if re.match(rf"(?i)^({known})(/|$)", name):
            return m.group(0)
        return f"{q}~/{folder}/{name}{q}"

    # '~/Foo bar' or "~/Foo bar"
    repaired = re.sub(r"(['\"])~/([^'\"]+)\1", _inject_quoted, cmd)

    def _inject_bare(m: re.Match[str]) -> str:
        name = m.group(1)
        if re.match(rf"(?i)^({known})(/|$)", name):
            return m.group(0)
        return f"~/{folder}/{name}"

    # Unquoted ~/Name (single path component)
    repaired = re.sub(
        rf"(?<![\w/])~/({known}|[^\s/'\";|&]+)",
        _inject_bare,
        repaired,
    )
    return repaired


def _deterministic_create_command(utterance: str) -> str:
    """Build mkdir/echo/open -R when the ask is a clear create-folder request."""
    text = (utterance or "").strip()
    if not text:
        return ""
    if not re.search(r"(?i)\b(create|make|new)\b", text):
        return ""
    if not re.search(r"(?i)\b(folder|director(?:y|ies)|file)\b", text):
        return ""

    folder = _home_folder_from_ask(text) or "Documents"
    # folder … named 'X'
    name_m = re.search(
        r"(?i)(?:folder|directory)\s+(?:in\s+\S+\s+)?(?:named|called)\s+"
        r"['\"]([^'\"]+)['\"]",
        text,
    )
    if not name_m:
        name_m = re.search(
            r"(?i)(?:named|called)\s+['\"]([^'\"]+)['\"]",
            text,
        )
    if not name_m:
        return ""
    dirname = name_m.group(1).strip()
    if not dirname:
        return ""

    # Optional file inside: named 'Y' containing 'Z' / with 'Z'
    file_m = re.search(
        r"(?i)(?:text\s+)?file\s+(?:inside\s+(?:it\s+)?)?(?:named|called)\s+"
        r"['\"]([^'\"]+)['\"]",
        text,
    )
    content_m = re.search(
        r"(?i)(?:containing|with(?:\s+contents?)?|that\s+says)\s+['\"]([^'\"]+)['\"]",
        text,
    )
    reveal = bool(re.search(r"(?i)\b(reveal|show|open|finder)\b", text))

    import shlex

    base = f"~/{folder}/{dirname}"
    parts: list[str] = [f"mkdir -p {shlex.quote(base)}"]
    if file_m:
        fname = file_m.group(1).strip()
        fpath = f"{base}/{fname}"
        if content_m:
            body = content_m.group(1)
            parts.append(f"printf '%s\\n' {shlex.quote(body)} > {shlex.quote(fpath)}")
        else:
            parts.append(f"touch {shlex.quote(fpath)}")
    if reveal:
        parts.append(f"open -R {shlex.quote(base)}")
    return " && ".join(parts)


def _deterministic_zip_command(utterance: str) -> str:
    """Zip matching files from Downloads/Desktop/Documents to a Desktop archive."""
    text = (utterance or "").strip()
    if not text:
        return ""
    if not re.search(r"(?i)\b(zip|compress|archive)\b", text):
        return ""

    src = "Downloads"
    if re.search(r"(?i)\bin\s+(?:my\s+)?documents\b|\bdocuments\s+folder\b", text):
        src = "Documents"
    elif re.search(r"(?i)\bin\s+(?:my\s+)?desktop\b|\bdesktop\s+folder\b", text):
        src = "Desktop"
    elif re.search(r"(?i)\bdownl", text):
        src = "Downloads"
    else:
        return ""

    ext = "pdf"
    m_ext = re.search(
        r"(?i)\b(pdf|png|jpe?g|gif|txt|csv|docx?|xlsx?|pptx?)\b",
        text,
    )
    if m_ext:
        ext = m_ext.group(1).lower()
        if ext == "jpeg":
            ext = "jpg"
        elif ext == "doc":
            ext = "doc"
        elif ext == "docx":
            ext = "docx"

    zip_m = re.search(
        r"(?i)(?:named|called)\s+['\"]([^'\"]+\.zip)['\"]",
        text,
    )
    if not zip_m:
        zip_m = re.search(r"(?i)['\"]([^'\"]+\.zip)['\"]", text)
    zip_name = (zip_m.group(1) if zip_m else f"{ext.upper()}_Archive.zip").strip()

    dest = "Desktop"
    if re.search(r"(?i)\b(?:on|to|onto)\s+(?:my\s+)?documents\b", text):
        dest = "Documents"

    reveal = bool(re.search(r"(?i)\b(reveal|show|finder|open)\b", text))

    import shlex

    archive = f"~/{dest}/{zip_name}"
    pattern = f"*.{ext}"
    # -print0 + xargs -0 keeps spaces in filenames; -j stores basenames only.
    cmd = (
        f"find ~/{src} -type f -name {shlex.quote(pattern)} -print0 | "
        f"xargs -0 zip -j {shlex.quote(archive)}"
    )
    if reveal:
        cmd += f" && open -R {shlex.quote(archive)}"
    return cmd


def _host_from_reachability_ask(utterance: str) -> str:
    """Extract a hostname/URL host from a reachability / latency ask."""
    text = utterance or ""
    m = re.search(r"(?i)https?://([a-z0-9.-]+\.[a-z]{2,})", text)
    if m:
        return m.group(1).lower().rstrip(".")
    # Prefer explicit "github.com is reachable" / "ping example.com"
    m = re.search(
        r"(?i)\b((?:[a-z0-9-]+\.)+[a-z]{2,})\b",
        text,
    )
    if not m:
        return ""
    host = m.group(1).lower().rstrip(".")
    # Skip junk like "response.time" false positives — require a real TLD-ish host.
    if host.count(".") < 1:
        return ""
    if re.search(
        r"(?i)^(www\.)?(average|response|terminal|internet)\.",
        host,
    ):
        return ""
    return host


def _deterministic_reachability_command(utterance: str) -> str:
    """curl latency probe when the user asked if a site is reachable / response time."""
    text = (utterance or "").strip()
    if not text:
        return ""
    wants = bool(
        re.search(
            r"(?i)\b("
            r"reachable|reachability|ping|latency|response\s+time|"
            r"how\s+fast|average\s+(response|latency)|"
            r"check\s+if.{0,60}\b(up|down|online|reachable)"
            r")\b",
            text,
        )
    )
    if not wants:
        return ""

    host = _host_from_reachability_ask(text)
    if not host:
        return ""
    url = f"https://{host}"
    # 3 probes; awk averages (no bc dependency).
    return (
        f"{{ "
        f"for i in 1 2 3; do "
        f"curl -sS -o /dev/null -w '%{{time_total}}\\n' "
        f"--connect-timeout 5 --max-time 15 {url} || echo FAIL; "
        f"done; "
        f"}} | awk -v host={host} '"
        r'{if($1=="FAIL") next; s+=$1; n++} '
        r'END{if(n) printf "reachable=yes host=%s probes=%d avg_total=%.3fs\n", host, n, s/n; '
        r'else printf "reachable=no host=%s\n", host}'
        "'"
    )


def _bash_fallback_for_ask(utterance: str) -> str:
    """Ask-aware fallback — never ls Downloads for unrelated tasks."""
    text = (utterance or "").strip()
    created = _deterministic_create_command(text)
    if created:
        return created
    zipped = _deterministic_zip_command(text)
    if zipped:
        return zipped
    reach = _deterministic_reachability_command(text)
    if reach:
        return reach
    if re.search(r"(?i)\bdownl", text):
        return "ls -lt ~/Downloads | head -20"
    return ""


def _ampersand_only_inside_quotes(command: str) -> bool:
    quote: str | None = None
    i = 0
    n = len(command or "")
    while i < n:
        ch = command[i]
        if quote:
            if ch == quote and (i == 0 or command[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "&":
            nxt = command[i + 1] if i + 1 < n else ""
            prev = command[i - 1] if i > 0 else ""
            if nxt == "&" or prev == "&":
                i += 1
                continue
            if re.match(r"2>&1|>&|<&", command[max(0, i - 1) : i + 3] or ""):
                i += 1
                continue
            return False
        i += 1
    return True


def _strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _is_empty_refusal(text: str) -> bool:
    """True when the model admitted it lacks an answer (any length)."""
    t = (text or "").strip()
    if not t:
        return True
    return bool(_EMPTY_REFUSAL_RE.search(t))


def _notes_extractive_summary(notes: str, limit: int = 8) -> str:
    """Fallback: pull bullet facts from user notes without the LLM."""
    facts: list[str] = []
    for ln in (notes or "").splitlines():
        s = ln.strip().lstrip("*•-").strip()
        if not s or s.startswith("---") or s.lower().startswith("evidence:"):
            continue
        # Prefer the main claim lines (usually “The user …”).
        if re.match(r"(?i)^the user\b", s) or len(facts) < 3:
            # Drop trailing evidence clauses if jammed onto the same line.
            s = re.split(r"(?i)\bevidence\s*:", s, maxsplit=1)[0].strip().rstrip(".")
            if s:
                s = _to_second_person(s)
                facts.append(s)
        if len(facts) >= limit:
            break
    if not facts:
        return notes.strip()[:900]
    return "Here's what I know about you:\n• " + "\n• ".join(facts)


def _to_second_person(sentence: str) -> str:
    s = sentence.strip()
    s = re.sub(r"(?i)^the user'?s\b", "Your", s)
    s = re.sub(r"(?i)^the user\b", "You", s)
    s = re.sub(r"(?i)\bthe user'?s\b", "your", s)
    s = re.sub(r"(?i)\bthe user\b", "you", s)
    # Fix common leftover agreement after “The user is/has/…” → “You is/has…”
    s = re.sub(r"(?i)^You is\b", "You are", s)
    s = re.sub(r"(?i)^You has\b", "You have", s)
    s = re.sub(r"(?i)^You was\b", "You were", s)
    s = re.sub(r"(?i)^You resides\b", "You reside", s)
    s = re.sub(r"(?i)^You plays\b", "You play", s)
    s = re.sub(r"(?i)^You maintains\b", "You maintain", s)
    s = re.sub(r"(?i)^You collaborates\b", "You collaborate", s)
    s = re.sub(r"(?i)^You shares\b", "You share", s)
    s = re.sub(r"(?i)^You concluded\b", "You concluded", s)
    return s


def _search_extractive_summary(context: str, max_hits: int = 3) -> str:
    """Fallback: turn search-hit snippets into a short answer when the LLM refuses."""
    if not (context or "").strip():
        return ""
    hits: list[tuple[str, str]] = []
    # Match: "1. Title\n   body\n   URL: ..."
    for m in re.finditer(
        r"(?m)^\d+\.\s+(.+?)\n\s+(.+?)(?:\n\s+URL:|\n\n|\Z)",
        context,
        re.DOTALL,
    ):
        title = " ".join(m.group(1).split())
        body = " ".join(m.group(2).split())
        # Skip generic listicle junk when better hits exist.
        if re.search(r"(?i)resume examples|cv compiler", title) and hits:
            continue
        if body and len(body) > 40:
            hits.append((title, body))
        if len(hits) >= max_hits:
            break
    if not hits:
        # Last resort: first non-empty paragraph after "Search hits:".
        chunk = context.split("Page content from", 1)[0]
        plain = " ".join(chunk.split())
        return plain[:700] if len(plain) > 80 else ""

    parts = []
    for title, body in hits:
        snippet = body if len(body) <= 320 else body[:317].rstrip() + "…"
        parts.append(f"• {title}: {snippet}")
    return (
        "From the sources I found:\n"
        + "\n".join(parts)
        + "\n\n(Ask if you want me to go deeper on any of these.)"
    )

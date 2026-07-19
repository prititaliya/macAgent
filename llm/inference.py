import json
import logging
import re
from pathlib import Path
from typing import Any, List, Optional

from llama_cpp import Llama, LlamaGrammar

from events.debug_trace import trace_step
from memory.user_context import build_runtime_context, load_user_notes_for_llm

logger = logging.getLogger(__name__)

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


class LocalIntentParser:
    """Metal-backed intent parser. Lazy-loads the GGUF on first use."""

    def __init__(self, model_path: Optional[str] = None, grammar_path: Optional[str] = None):
        settings = _load_settings()
        raw_model = model_path or settings.get(
            "model_path", "~/Models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
        )
        self.model_path = str(Path(raw_model).expanduser())
        self.grammar_path = grammar_path or str(
            Path(__file__).resolve().parent / "grammar.gbnf"
        )
        self._llm: Optional[Llama] = None

    def _ensure_loaded(self) -> None:
        if self._llm is not None:
            return
        if not Path(self.model_path).exists():
            raise FileNotFoundError(
                f"Model missing at {self.model_path}. "
                "Download the Qwen2.5-1.5B-Instruct Q4_K_M GGUF first."
            )
        logger.info("Loading GGUF from %s (Metal n_gpu_layers=-1)", self.model_path)
        self._llm = Llama(
            model_path=self.model_path,
            n_ctx=4096,
            n_gpu_layers=-1,
            verbose=False,
        )

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
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{raw_text}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )

        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=128,
                temperature=0.0,
                stop=["<|im_end|>", "<|im_start|>", "\n\n"],
            )
            text = response["choices"][0]["text"]
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
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )

        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=16,
                grammar=purpose_grammar,
                temperature=0.0,
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

    def generate_answer(self, utterance: str) -> str:
        """Short local chat reply (no GBNF). Used for answer intents."""
        fallback = "I could not generate an answer right now."
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("Answer skipped: %s", exc)
            return fallback

        runtime = build_runtime_context()
        system_prompt = (
            "You are MacAgent, a concise local macOS assistant. "
            "Use the CONTEXT block (current time and user notes) when relevant. "
            "Answer in a few short sentences. "
            "If the question needs live or web data (scores, schedules, news, prices), "
            "reply with exactly: NEED_BROWSER "
            "Do not invent live sports results. Do not say you lack internet access."
        )
        user_prompt = (
            f"CONTEXT:\n{runtime}\n\n"
            f"User question: {utterance}"
        )
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=192,
                temperature=0.4,
                stop=["<|im_end|>", "<|im_start|>"],
            )
            text = (response["choices"][0]["text"] or "").strip()
            out = text or fallback
            trace_step(
                "generate_answer",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                raw_output=out,
            )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to generate answer: %s", exc)
            trace_step("generate_answer_error", error=str(exc))
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
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=320,
                temperature=0.2,
                stop=["<|im_end|>", "<|im_start|>"],
            )
            text = (response["choices"][0]["text"] or "").strip()
            out = text or fallback
            if _is_empty_refusal(out):
                out = fallback
            trace_step(
                "answer_about_user",
                system_prompt=system_prompt,
                user_prompt=user_prompt[:2000],
                raw_output=out,
            )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to summarize user notes: %s", exc)
            trace_step("answer_about_user_error", error=str(exc))
            return fallback

    def answer_from_search(self, utterance: str, search_context: str) -> str:
        """Answer using search hits + fetched page text (grounded; less hallucination)."""
        extractive = _search_extractive_summary(search_context)
        fallback = extractive or (
            "I searched the web but could not form a reliable answer from the pages."
        )
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("Search answer skipped: %s", exc)
            return fallback

        runtime = build_runtime_context()
        system_prompt = (
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
            "- Reply in 2–8 short sentences."
        )
        # Keep prompt bounded for the small model.
        ctx = (search_context or "")[:6000]
        user_prompt = (
            f"CONTEXT:\n{runtime}\n\n"
            f"User question: {utterance}\n\n"
            f"Web context:\n{ctx}\n\n"
            "Write a helpful answer from the web context (and notes if relevant). "
            "Use any prices, model names, or comparison facts present. "
            "Do not claim the sources lack information if they contain relevant facts:"
        )
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=256,
                temperature=0.2,
                stop=["<|im_end|>", "<|im_start|>"],
            )
            text = (response["choices"][0]["text"] or "").strip()
            out = text or fallback
            # Small models often refuse “about me” even when LinkedIn/snippets are rich.
            if _is_empty_refusal(out) and extractive:
                logger.info("answer_from_search: replacing empty refusal with extractive")
                out = extractive
            trace_step(
                "answer_from_search",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                web_context_chars=len(ctx),
                raw_output=out,
            )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to summarize search: %s", exc)
            trace_step("answer_from_search_error", error=str(exc))
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
            "do not invent files that are not in the output; keep it concise (under ~12 lines)."
        )
        out = (stdout or "")[:4500]
        user_prompt = (
            f"User question: {utterance}\n\n"
            f"Command: {command}\n\n"
            f"Command output:\n{out}\n\n"
            "Formatted answer:"
        )
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=280,
                temperature=0.1,
                stop=["<|im_end|>", "<|im_start|>"],
            )
            text = (response["choices"][0]["text"] or "").strip()
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
        fallback = json.dumps(
            {"tool": "respond", "args": {"text": "I could not plan the next step."}}
        )
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("Tool plan skipped: %s", exc)
            return fallback

        runtime = build_runtime_context()
        hist_lines: list[str] = []
        for i, item in enumerate(history[-3:]):
            call = item.get("call") or {}
            result = item.get("result") or {}
            slim = {
                k: (v[:500] + "…" if isinstance(v, str) and len(v) > 500 else v)
                for k, v in result.items()
                if k != "context" or True
            }
            if isinstance(slim.get("context"), str) and len(slim["context"]) > 600:
                slim["context"] = slim["context"][:600] + "…"
            hist_lines.append(
                f"{i+1}. called {call.get('tool')} args={json.dumps(call.get('args') or {}, ensure_ascii=False)[:200]} "
                f"→ {json.dumps(slim, ensure_ascii=False, default=str)[:700]}"
            )
        history_block = "\n".join(hist_lines) if hist_lines else "(none yet)"

        system_prompt = (
            "You are MacAgent's planner on macOS. "
            "Choose exactly ONE next tool call as JSON: {\"tool\":\"name\",\"args\":{...}}. "
            "No markdown. No explanation. "
            "CRITICAL: Match the user's words. "
            "Multi-step is expected: one tool per step, keep going until the GOAL is done. "
            "After listing/search, if the user asked to delete/move/open/act on that result, "
            "call the next tool — do NOT respond with only the listing. "
            "If a prior web_search answer said context was insufficient / missing prices, "
            "call web_search again with a sharper query (include model names + pricing + year). "
            "Use tool=respond only when the goal is finished or you must ask a clarifying question. "
            "For greetings or small talk (hi, yo, hey, thanks) use tool=respond with a short friendly reply. "
            "NEVER call shut down, restart, empty trash, rm, or ui_* unless the user explicitly asked. "
            "Do not invent destructive actions from the catalog examples. "
            "For factual questions use web_search then respond. "
            "For explicit open/launch use open_app or open_url. "
            "For explicit file tasks use run_bash. "
            "Only when the user wants something done on the Mac should you use action tools."
        )
        user_prompt = (
            f"CONTEXT:\n{runtime}\n\n"
            f"{tool_catalog}\n\n"
            f"User request: {utterance}\n\n"
            f"Prior tool results:\n{history_block}\n\n"
            "Next tool JSON:"
        )
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=160,
                temperature=0.0,
                stop=["<|im_end|>", "<|im_start|>", "\n\n"],
            )
            text = (response["choices"][0]["text"] or "").strip()
            trace_step(
                "plan_tool_call",
                system_prompt=system_prompt,
                user_prompt=user_prompt[:2500],
                raw_output=text,
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
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=320,
                temperature=0.1,
                stop=["<|im_end|>", "<|im_start|>"],
            )
            text = (response["choices"][0]["text"] or "").strip()
            text = _strip_code_fences(text)
            trace_step("generate_python", user=utterance, raw_output=text[:1000])
            return text or fallback
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_python failed: %s", exc)
            return fallback

    def generate_bash(self, utterance: str) -> str:
        """Write a short bash command for a local macOS file/shell task."""
        fallback = "ls -lt ~/Downloads | head -20"
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("generate_bash skipped: %s", exc)
            return fallback

        system_prompt = (
            "You write ONE short bash command for macOS (zsh/bash). "
            "Reply with ONLY the command — no markdown, no explanation, no leading $. "
            "Prefer: ls, find, mdfind, open, head, sort, stat, du, pwd, echo. "
            "If the user asked to DELETE/REMOVE the latest download, emit a command that "
            "resolves the newest file in ~/Downloads and rm's it (print Deleted: path). "
            "Home is ~. Print useful stdout. Do not use sudo, rm -rf /, curl|sh, or disk erase. "
            "To empty the macOS Trash/Bin use: "
            "osascript -e 'tell application \"Finder\" to empty the trash' "
            "— never rm /bin or touch system paths."
        )
        user_prompt = (
            f"Write one bash command that accomplishes this and prints useful output:\n"
            f"{utterance}"
        )
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=160,
                temperature=0.1,
                stop=["<|im_end|>", "<|im_start|>", "\n\n"],
            )
            text = (response["choices"][0]["text"] or "").strip()
            text = _strip_code_fences(text)
            # Take first non-empty line; strip prompt junk.
            for line in text.splitlines():
                line = line.strip().lstrip("$").strip()
                if line and not line.lower().startswith("bash"):
                    text = line
                    break
            trace_step("generate_bash", user=utterance, raw_output=text[:1000])
            return text or fallback
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_bash failed: %s", exc)
            return fallback

    def check_goal_done(
        self, utterance: str, history_summary: str, candidate: str
    ) -> dict[str, Any]:
        """Critic: did the candidate answer / tool history finish the user's goal?"""
        fallback = {"done": True, "reason": "assumed done", "next_hint": ""}
        try:
            self._ensure_loaded()
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("check_goal_done skipped: %s", exc)
            return fallback

        system_prompt = (
            "You verify whether a Mac assistant finished the user's request. "
            "Reply with ONLY JSON: "
            '{"done":true|false,"reason":"…","next_hint":"tool or action to try next"}. '
            "done=false if they only listed/found something but the user asked to "
            "delete/remove/open/move/empty/change it. "
            "done=true if the action is complete or the user only asked a question."
        )
        user_prompt = (
            f"User request: {utterance}\n\n"
            f"Tool history:\n{history_summary or '(none)'}\n\n"
            f"Candidate answer:\n{(candidate or '')[:800]}\n\n"
            "JSON:"
        )
        prompt = (
            f"<|im_start|>system\n{system_prompt}<|im_end|>\n"
            f"<|im_start|>user\n{user_prompt}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        assert self._llm is not None
        try:
            response = self._llm(
                prompt,
                max_tokens=120,
                temperature=0.0,
                stop=["<|im_end|>", "<|im_start|>"],
            )
            text = (response["choices"][0]["text"] or "").strip()
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

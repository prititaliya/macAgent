"""Multi-step tool-calling agent loop (capped iterations for 1.5B / 8GB)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from events.bus import event_bus
from events.debug_trace import trace_step
from tools.registry import TOOL_CATALOG, ToolRegistry

logger = logging.getLogger(__name__)

_MAX_ITERS = 4
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_tool_call(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    candidates = [text.strip()]
    # Prefer fenced json
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                candidates.insert(0, part)
    candidates.extend(_JSON_RE.findall(text))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        tool = str(data.get("tool") or data.get("name") or "").strip()
        if not tool:
            continue
        args = data.get("args") or data.get("arguments") or data.get("parameters") or {}
        if not isinstance(args, dict):
            args = {}
        return {"tool": tool, "args": args}
    return None


class AgentLoop:
    def __init__(self, parser, registry: Optional[ToolRegistry] = None):
        self.parser = parser
        self.registry = registry or ToolRegistry()

    def run(self, utterance: str) -> dict[str, Any]:
        """Execute tool loop; returns final payload for SSE / activity."""
        history: list[dict[str, Any]] = []
        sources: list[Any] = []
        last_text = ""

        event_bus.publish(
            utterance=utterance,
            kind="trace",
            text="Received input",
            detail="input",
            step="input",
            tool_input={"utterance": utterance},
        )
        event_bus.publish(
            utterance=utterance,
            kind="action",
            text="Planning…",
            detail="pending",
        )

        for step in range(_MAX_ITERS):
            call = self._next_call(utterance, history)
            if call is None:
                trace_step("agent_parse_fail", step=step, history_len=len(history))
                break

            tool = call["tool"]
            args = call.get("args") or {}

            # Special: generate Python then run it (1.5B-friendly path).
            if tool == "__write_and_run_python__":
                event_bus.publish(
                    utterance=utterance,
                    kind="action",
                    text="Writing & running code…",
                    detail="pending",
                    step="codegen",
                )
                code = self.parser.generate_python(utterance)
                call = {"tool": "run_python", "args": {"code": code}}
                tool = "run_python"
                args = {"code": code}
                trace_step("agent_generated_code", code=code[:1000])
                event_bus.publish(
                    utterance=utterance,
                    kind="trace",
                    text="Generated Python code",
                    detail="codegen",
                    step="codegen",
                    tool="run_python",
                    tool_input={"code": code},
                )

            trace_step("agent_tool_call", step=step, tool=tool, args=_trim(args))
            event_bus.publish(
                utterance=utterance,
                kind="trace",
                text=f"Calling {tool}",
                detail="tool_call",
                step="tool_call",
                tool=tool,
                tool_input=_trim(args),
            )
            event_bus.publish(
                utterance=utterance,
                kind="action",
                text=f"Tool: {tool}",
                detail="pending",
            )

            if tool == "respond":
                text = str(args.get("text") or args.get("message") or "").strip()
                if not text and history:
                    text = self._synthesize_from_history(utterance, history)
                last_text = text or "Done."
                result = {"ok": True, "text": last_text, "final": True}
                history.append({"call": call, "result": result})
                event_bus.publish(
                    utterance=utterance,
                    kind="trace",
                    text="Final respond",
                    detail="respond",
                    step="respond",
                    tool="respond",
                    tool_input={"text": last_text},
                    tool_output={"text": last_text},
                )
                return self._finish(utterance, last_text, sources, history)

            result = self.registry.run(tool, args)
            history.append({"call": call, "result": result})
            trace_step("agent_tool_result", step=step, tool=tool, result=_trim(result))
            event_bus.publish(
                utterance=utterance,
                kind="trace",
                text=f"{tool} → output",
                detail="tool_result",
                step="tool_result",
                tool=tool,
                tool_input=_trim(args),
                tool_output=_trim(result),
            )

            if tool == "web_search" and isinstance(result.get("sources"), list):
                sources = result["sources"]

            # Failed / empty web search → answer locally, never dump JSON.
            if tool == "web_search" and not result.get("ok"):
                reply = self._local_answer(utterance)
                return self._finish(utterance, reply, sources, history)

            if tool == "web_search" and result.get("ok") and result.get("context"):
                reply = self.parser.answer_from_search(
                    utterance, str(result["context"])
                )
                return self._finish(utterance, reply, sources, history)

            if tool == "run_python":
                if result.get("ok") and (result.get("stdout") or "").strip():
                    out = str(result["stdout"]).strip()
                    # Show result; include tiny code preview when helpful.
                    code_preview = (result.get("code") or "").strip()
                    if code_preview and len(code_preview) < 200:
                        text = f"{out}"
                    else:
                        text = out
                    return self._finish(utterance, text, sources, history)
                err = result.get("error") or result.get("stderr") or "code failed"
                # One retry via regenerated code is left to the planner; fall back locally.
                reply = self._local_answer(utterance)
                if reply:
                    return self._finish(
                        utterance,
                        f"{reply}\n\n(code error: {err})" if False else reply,
                        sources,
                        history,
                    )
                return self._finish(utterance, f"Code failed: {err}", sources, history)

            if result.get("final") and result.get("text"):
                last_text = str(result["text"])
                return self._finish(utterance, last_text, sources, history)

            # Action tools that already did the work — wrap up without another LLM hop.
            if tool in {
                "open_app",
                "open_url",
                "open_system_settings",
                "update_user_context",
            } and result.get("ok"):
                msg = (
                    result.get("message")
                    or result.get("text")
                    or f"Done ({tool})."
                )
                if tool == "update_user_context":
                    msg = "Saved your notes."
                elif tool == "open_system_settings":
                    msg = f"Opened System Settings ({result.get('pane') or 'general'})."
                return self._finish(utterance, str(msg), sources, history)

            if tool == "find_files" and result.get("ok") and step >= 0:
                # One-shot file results are usually enough.
                paths = result.get("paths") or []
                if paths:
                    lines = "\n".join(f"- {p}" for p in paths[:12])
                    return self._finish(
                        utterance,
                        f"Found {len(paths)} file(s):\n{lines}",
                        sources,
                        history,
                    )

        # Force a respond if the model never did.
        last_text = self._synthesize_from_history(utterance, history) or last_text
        if not last_text:
            last_text = "I could not complete that request."
        return self._finish(utterance, last_text, sources, history)

    def _next_call(
        self, utterance: str, history: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        # Heuristic shortcuts for clear patterns (saves latency on 1.5B).
        if not history:
            quick = _heuristic_tool(utterance)
            if quick:
                return quick

        raw = self.parser.plan_tool_call(utterance, history, TOOL_CATALOG)
        parsed = _parse_tool_call(raw)
        if parsed:
            return parsed
        # Fallback: if we have web context already, respond; else web_search or respond.
        if history:
            return {
                "tool": "respond",
                "args": {"text": self._synthesize_from_history(utterance, history)},
            }
        return {"tool": "web_search", "args": {"query": utterance}}

    def _synthesize_from_history(
        self, utterance: str, history: list[dict[str, Any]]
    ) -> str:
        if not history:
            return self._local_answer(utterance)
        for item in reversed(history):
            result = item.get("result") or {}
            call = item.get("call") or {}
            tool = call.get("tool")
            if tool == "web_search" and result.get("context"):
                try:
                    return self.parser.answer_from_search(
                        utterance, str(result["context"])
                    )
                except Exception:  # noqa: BLE001
                    pass
            if tool == "find_files" and result.get("paths"):
                paths = result["paths"]
                lines = "\n".join(f"- {p}" for p in paths[:12])
                return f"Found {len(paths)} file(s):\n{lines}"
            if result.get("message"):
                return str(result["message"])
            if result.get("text") and result.get("ok", True):
                return str(result["text"])
            if tool == "get_user_context" and result.get("notes") is not None:
                notes = (result.get("notes") or "").strip() or "(empty)"
                return f"Current notes:\n{notes}"
            if tool == "run_python" and result.get("stdout"):
                return str(result["stdout"]).strip()
        # Never surface raw tool JSON to the user.
        return self._local_answer(utterance)

    def _local_answer(self, utterance: str) -> str:
        math = _try_simple_math(utterance)
        if math is not None:
            return math
        try:
            return self.parser.generate_answer(utterance)
        except Exception:  # noqa: BLE001
            return "I could not answer that right now."

    def _finish(
        self,
        utterance: str,
        text: str,
        sources: list[Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Normalize sources for the overlay (tappable links only — no text footer).
        normalized: list[dict[str, str]] = []
        for s in sources[:5]:
            if isinstance(s, str) and s.startswith("http"):
                normalized.append({"title": s, "url": s})
            elif isinstance(s, dict):
                url = str(s.get("url") or "").strip()
                if url:
                    normalized.append(
                        {
                            "title": str(s.get("title") or url),
                            "url": url,
                        }
                    )

        try:
            self.registry.memory.log_activity(
                utterance, "agent", f"steps={len(history)}", text[:500]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("activity log failed: %s", exc)

        event_bus.publish(
            utterance=utterance,
            kind="answer",
            text=text,
            detail="agent",
            sources=normalized or None,
        )
        trace_step(
            "final",
            kind="answer",
            detail="agent",
            text=text,
            tools=[h.get("call", {}).get("tool") for h in history],
        )
        return {
            "action": "answer",
            "answer": text,
            "sources": normalized,
            "steps": len(history),
        }


def _trim(obj: Any, limit: int = 1200) -> Any:
    raw = json.dumps(obj, ensure_ascii=False, default=str)
    if len(raw) <= limit:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "context" and isinstance(v, str):
                out[k] = v[:800] + "…"
            else:
                out[k] = v
        return out
    return raw[:limit]


def _heuristic_tool(utterance: str) -> Optional[dict[str, Any]]:
    text = (utterance or "").strip()
    lower = text.lower()
    if not text:
        return None

    # Simple arithmetic — answer locally, skip web search.
    math = _try_simple_math(text)
    if math is not None and re.search(r"(?i)[\+\-\*\/x×÷]", text):
        return {"tool": "respond", "args": {"text": math}}

    # Explicit code / compute — model will write Python via generate path.
    if re.search(
        r"(?i)\b(write|run|execute)\b.+\b(code|script|python|program)\b|"
        r"\b(calculate|compute|evaluate|factorial|fibonacci|prime|"
        r"sort this|parse json|regex)\b",
        text,
    ):
        return {"tool": "__write_and_run_python__", "args": {}}

    # File search
    if re.search(r"(?i)\b(find|locate|search for)\b.+\b(file|pdf|doc|folder|invoice)\b", text) or re.search(
        r"(?i)\b(find|locate)\s+(my\s+)?[\w.\- ]+\.(pdf|docx?|xlsx?|png|jpg)\b", text
    ):
        q = re.sub(r"(?i)^(please\s+)?(find|locate|search for)\s+", "", text).strip()
        return {"tool": "find_files", "args": {"query": q or text, "limit": 10}}

    if re.search(r"(?i)\b(open|launch|start)\s+(system\s+)?(settings|preferences)\b", lower):
        pane = "general"
        for key in (
            "wifi",
            "bluetooth",
            "accessibility",
            "privacy",
            "network",
            "keyboard",
            "displays",
            "sound",
            "battery",
            "notifications",
            "spotlight",
        ):
            if key in lower or key.replace("i", "i-") in lower:
                pane = key
                break
        if "wi-fi" in lower or "wi fi" in lower:
            pane = "wifi"
        return {"tool": "open_system_settings", "args": {"pane": pane}}

    m = re.match(r"(?i)^\s*(open|launch|start)\s+(.+)$", text)
    if m:
        target = m.group(2).strip().rstrip(".")
        if target.startswith("http") or "." in target.split()[0]:
            return {"tool": "open_url", "args": {"url": target}}
        if "setting" in target.lower() or "preference" in target.lower():
            return {"tool": "open_system_settings", "args": {"pane": "general"}}
        return {"tool": "open_app", "args": {"name": target}}

    if re.search(r"(?i)\b(what('?s| is) in my (notes|context)|show (my )?notes)\b", text):
        return {"tool": "get_user_context", "args": {}}

    if re.search(r"(?i)\b(remember that|note that|save (this )?note)\b", text):
        note = re.sub(
            r"(?i)^(please\s+)?(remember that|note that|save (this )?note:?)\s*",
            "",
            text,
        ).strip()
        existing = load_notes_safe()
        merged = (existing + "\n" + note).strip() if existing else note
        return {"tool": "update_user_context", "args": {"notes": merged}}

    return None


def load_notes_safe() -> str:
    try:
        from memory.user_context import load_user_notes

        return load_user_notes()
    except Exception:  # noqa: BLE001
        return ""


_MATH_EXPR_RE = re.compile(r"(?i)(\d+)\s*([\+\-\*\/x×÷])\s*(\d+)")


def _try_simple_math(text: str) -> Optional[str]:
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

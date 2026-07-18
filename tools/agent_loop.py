"""Multi-step tool-calling agent loop (capped iterations for 1.5B / 8GB)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from events.bus import event_bus
from events.debug_trace import trace_step
from tools.pending_actions import create_pending
from tools.registry import TOOL_CATALOG, ToolRegistry
from tools.run_bash import empty_trash_command

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

            # Special: generate bash then run it.
            if tool == "__write_and_run_bash__":
                event_bus.publish(
                    utterance=utterance,
                    kind="action",
                    text="Writing & running shell…",
                    detail="pending",
                    step="shellgen",
                )
                command = self.parser.generate_bash(utterance)
                call = {"tool": "run_bash", "args": {"command": command}}
                tool = "run_bash"
                args = {"command": command}
                trace_step("agent_generated_bash", command=command[:1000])
                event_bus.publish(
                    utterance=utterance,
                    kind="trace",
                    text="Generated bash command",
                    detail="shellgen",
                    step="shellgen",
                    tool="run_bash",
                    tool_input={"command": command},
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
                    # Math / one-liners stay as-is; otherwise format for the question.
                    if len(out) < 80 and "\n" not in out:
                        return self._finish(utterance, out, sources, history)
                    text = self._format_command_answer(
                        utterance,
                        result.get("code") or "python",
                        out,
                    )
                    return self._finish(utterance, text, sources, history)
                err = result.get("error") or result.get("stderr") or "code failed"
                reply = self._local_answer(utterance)
                if reply:
                    return self._finish(utterance, reply, sources, history)
                return self._finish(utterance, f"Code failed: {err}", sources, history)

            if tool == "run_bash":
                if result.get("needs_confirm"):
                    return self._request_confirm(
                        utterance,
                        command=str(result.get("command") or args.get("command") or ""),
                        summary=str(result.get("summary") or ""),
                        sources=sources,
                        history=history,
                    )
                out = (result.get("stdout") or "").strip()
                err = (result.get("error") or result.get("stderr") or "").strip()
                cmd = (result.get("command") or args.get("command") or "").strip()
                if result.get("ok") and out:
                    text = self._format_command_answer(utterance, cmd, out)
                    return self._finish(utterance, text, sources, history)
                if result.get("ok") and not out:
                    return self._finish(
                        utterance,
                        "Done." if not cmd else f"Done (`{cmd}`).",
                        sources,
                        history,
                    )
                return self._finish(
                    utterance,
                    f"Shell error: {err or 'command failed'}",
                    sources,
                    history,
                )

            if result.get("final") and result.get("text"):
                last_text = str(result["text"])
                return self._finish(utterance, last_text, sources, history)

            # Action tools that already did the work — wrap up without another LLM hop.
            if tool in {
                "open_app",
                "open_url",
                "open_folder",
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
                elif tool == "open_folder":
                    msg = result.get("message") or f"Opened folder {result.get('path')}"
                return self._finish(utterance, str(msg), sources, history)

            if tool == "find_files" and result.get("ok") and step >= 0:
                paths = result.get("paths") or []
                items = result.get("items") or []
                if paths or items:
                    lines = []
                    if items:
                        for i, it in enumerate(items[:12], 1):
                            kind = it.get("kind") or "file"
                            name = it.get("name") or Path(str(it.get("path") or "")).name
                            path = it.get("path") or ""
                            lines.append(f"{i}. [{kind}] {name}\n   {path}")
                        header = (
                            f"Most recent in Downloads ({len(items)}):"
                            if result.get("scope") == "Downloads"
                            else f"Found {len(items)} item(s):"
                        )
                    else:
                        for i, p in enumerate(paths[:12], 1):
                            lines.append(f"{i}. {p}")
                        header = f"Found {len(paths)} file(s):"
                    return self._finish(
                        utterance,
                        header + "\n" + "\n".join(lines),
                        sources,
                        history,
                    )
                return self._finish(
                    utterance,
                    "No matching files found.",
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
            if tool == "run_bash" and result.get("stdout"):
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

    def _format_command_answer(
        self, utterance: str, command: str, stdout: str
    ) -> str:
        """Never dump raw terminal noise; format for the user's question."""
        friendly = _friendly_ls_answer(utterance, stdout)
        if friendly:
            return friendly
        # Short action confirmations ("Opened: …") are already good.
        if re.match(r"(?i)^(opened|done|saved|no .+ found)\b", stdout.strip()):
            return stdout.strip()
        try:
            return self.parser.answer_from_command(utterance, command, stdout)
        except Exception:  # noqa: BLE001
            return friendly or stdout.strip()[:2000]

    def _request_confirm(
        self,
        utterance: str,
        *,
        command: str,
        summary: str,
        sources: list[Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        cmd = (command or "").strip()
        summary = (summary or "").strip() or f"Run:\n{cmd}"
        pending = create_pending(
            utterance=utterance,
            summary=summary,
            command=cmd,
            tool="run_bash",
        )
        event_bus.publish(
            utterance=utterance,
            kind="confirm",
            text=summary,
            detail="needs_permission",
            step="confirm",
            tool="run_bash",
            tool_input={
                "id": pending["id"],
                "summary": summary,
                "command": cmd,
            },
        )
        trace_step(
            "confirm_requested",
            id=pending["id"],
            command=cmd[:500],
            summary=summary[:500],
        )
        history.append(
            {
                "call": {"tool": "run_bash", "args": {"command": cmd}},
                "result": {"ok": False, "needs_confirm": True, "id": pending["id"]},
            }
        )
        return {
            "action": "confirm",
            "pending_id": pending["id"],
            "summary": summary,
            "command": cmd,
            "answer": summary,
            "sources": sources,
            "steps": len(history),
        }

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

    # Empty Trash / Bin — never /bin; always goes through confirm gate.
    if re.search(
        r"(?i)\b("
        r"empty\s+(the\s+)?(bin|trash|rubbish)|"
        r"clear\s+(the\s+)?(bin|trash)|"
        r"delete\s+(everything|all)\s+in\s+(the\s+)?(bin|trash)|"
        r"take\s+out\s+(the\s+)?(bin|trash)"
        r")\b",
        text,
    ):
        return {
            "tool": "run_bash",
            "args": {"command": empty_trash_command()},
        }

    # Recently downloaded / Downloads — bash, not a special-case tool.
    if re.search(
        r"(?i)\b("
        r"recent(ly)?\s+download|download(ed)?\s+(item|file|folder|stuff)?|"
        r"last\s+download|newest\s+in\s+downloads|"
        r"what('?s|\s+is|\s+are)?\s+(the\s+)?most\s+recent(ly)?|"
        r"what('?s| did i)\s+download|"
        r"find( me)?\s+(my\s+)?recent(ly)?\s+download|"
        r"show( me)?\s+(my\s+)?downloads"
        r")\b",
        text,
    ) and re.search(r"(?i)download", text):
        singular = bool(
            re.search(r"(?i)\b(most\s+recent(ly)?|last|newest|latest)\b", text)
            and not re.search(r"(?i)\b(list|show\s+all|all\s+my)\b", text)
        )
        if re.search(r"(?i)\b(open|reveal)\b", text):
            return {
                "tool": "run_bash",
                "args": {
                    "command": (
                        'f=$(ls -t ~/Downloads/* 2>/dev/null | head -1); '
                        'if [ -n "$f" ]; then open "$f"; echo "Opened: $f"; '
                        'else echo "Downloads is empty"; fi'
                    )
                },
            }
        if singular:
            return {
                "tool": "run_bash",
                "args": {
                    "command": "ls -lt ~/Downloads | head -6",
                },
            }
        return {
            "tool": "run_bash",
            "args": {
                "command": "ls -lt ~/Downloads | head -20",
            },
        }

    # Open folder / directory via bash (before open_app).
    folder_m = re.search(
        r"(?i)^\s*(open|show|reveal)\s+(the\s+)?(folder|directory|dir)\s+(.+)$",
        text,
    )
    if folder_m:
        name = folder_m.group(4).strip().rstrip(".")
        name = re.sub(r"(?i)^(named|called)\s+", "", name).strip()
        return {"tool": "run_bash", "args": {"command": _bash_open_folder(name)}}
    folder_m2 = re.search(
        r"(?i)^\s*(open|show|reveal)\s+(.+?)\s+(folder|directory|dir)\s*$",
        text,
    )
    if folder_m2:
        name = folder_m2.group(2).strip()
        name = re.sub(r"(?i)^(the|a|my)\s+", "", name).strip()
        return {"tool": "run_bash", "args": {"command": _bash_open_folder(name)}}

    # File / folder search — let the model invent a bash command.
    if re.search(
        r"(?i)\b(find|locate|search for|show me)\b.+\b(file|pdf|doc|folder|invoice|item|download)\b",
        text,
    ) or re.search(
        r"(?i)\b(find|locate)\s+(my\s+)?[\w.\- ]+\.(pdf|docx?|xlsx?|png|jpg)\b",
        text,
    ) or re.search(r"(?i)\b(run|execute)\b.+\b(bash|shell|command|terminal)\b", text):
        return {"tool": "__write_and_run_bash__", "args": {}}

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
        # Strip leading "the folder/directory"
        target = re.sub(r"(?i)^(the\s+)?(folder|directory|dir)\s+", "", target).strip()
        if re.search(r"(?i)\b(folder|directory|dir)\b", target):
            target = re.sub(r"(?i)\b(folder|directory|dir)\b", "", target).strip()
            return {"tool": "run_bash", "args": {"command": _bash_open_folder(target)}}
        if target.startswith("http") or (
            "." in target.split()[0] and not target.lower().endswith(".app")
        ):
            # hostname-looking → url; course codes like comp3370 stay folders/apps
            if "/" in target or target.startswith("www.") or re.search(r"\.[a-z]{2,}$", target.lower()):
                return {"tool": "open_url", "args": {"url": target}}
        if "setting" in target.lower() or "preference" in target.lower():
            return {"tool": "open_system_settings", "args": {"pane": "general"}}
        # Course / project style names → try folder first (comp3370, my-notes, etc.)
        if re.match(r"(?i)^[a-z]{2,}\d{3,}[a-z0-9_\-]*$", target.replace(" ", "")) or (
            "folder" in lower or "directory" in lower
        ):
            return {"tool": "run_bash", "args": {"command": _bash_open_folder(target)}}
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


def _bash_open_folder(name: str) -> str:
    """Safe-ish bash to find a folder under ~ and open it in Finder."""
    import shlex

    q = shlex.quote((name or "").strip())
    return (
        f"name={q}; "
        'p=$(mdfind -onlyin "$HOME" "kind:folder $name" 2>/dev/null | head -1); '
        'if [ -z "$p" ]; then p=$(find "$HOME" -type d -iname "*$name*" 2>/dev/null | head -1); fi; '
        'if [ -n "$p" ]; then open "$p"; echo "Opened: $p"; else echo "No folder found for: $name"; fi'
    )


def _friendly_ls_answer(utterance: str, stdout: str) -> Optional[str]:
    """Turn `ls -l` style stdout into a clean answer for file questions."""
    items: list[tuple[str, str, str]] = []
    for line in (stdout or "").splitlines():
        line = line.rstrip()
        if not line or line.lower().startswith("total "):
            continue
        if line[0] not in "-dlbcps":
            continue
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        kind = "folder" if parts[0].startswith("d") else "file"
        when = f"{parts[5]} {parts[6]} {parts[7]}"
        name = parts[8]
        items.append((kind, name, when))
    if not items:
        return None

    lower = (utterance or "").lower()
    singular = bool(
        re.search(r"(?i)\b(most\s+recent(ly)?|last|newest|latest)\b", lower)
        and not re.search(r"(?i)\b(list|show\s+all|all\s+my|recent\s+ones)\b", lower)
    )
    if singular:
        kind, name, when = items[0]
        kind_word = "folder" if kind == "folder" else "file"
        if "download" in lower:
            return f"Your most recent download is {name} ({when})."
        return f"The most recent {kind_word} is {name} ({when})."

    if re.search(r"download", lower):
        header = "Most recent in Downloads:"
    elif re.search(r"folder|director", lower):
        header = "Found:"
    else:
        header = "Here is what I found:"

    lines = [header]
    for i, (kind, name, when) in enumerate(items[:15], 1):
        prefix = "[folder] " if kind == "folder" else ""
        lines.append(f"{i}. {prefix}{name}  ·  {when}")
    return "\n".join(lines)


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

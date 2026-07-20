"""Multi-step tool-calling agent loop (capped iterations for 1.5B / 8GB)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from events.bus import event_bus
from events.debug_trace import trace_step
from tools.pending_actions import create_pending, clear_all as clear_pending_actions
from tools.registry import TOOL_CATALOG, ToolRegistry
from tools.run_bash import (
    empty_trash_command,
    restart_command,
    shutdown_command,
    sleep_command,
)
from tools.tts_narrator import narrate, narrate_answer

logger = logging.getLogger(__name__)

_MAX_ITERS = 10
_MAX_WEB_SEARCHES = 3
_MAX_IDENTICAL_FAILURES = 2
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_ACTION_RE = re.compile(
    r"(?i)\b("
    r"shut\s*down|turn\s+off|power\s+off|restart|reboot|sleep|log\s*out|"
    r"empty\s+(the\s+)?(bin|trash)|delete|remove|install|quit|close|"
    # bare "open" is handled via _is_open_action (avoids open-source / open to …)
    r"click|press|type|launch|start|enable|disable|toggle|"
    r"set|change|move|copy|create|make|do\s+it|perform|run\s+this"
    r")\b"
)

_POWER_UTTERANCE_RE = re.compile(
    r"(?i)\b("
    r"pmset|displaysleep|display\s*sleep|disk\s*sleep|"
    r"sleep\s+(timeout|timer|after|in)|"
    r"hibernate|low\s*power|power\s*nap|energy\s*saver|"
    r"battery\s+(settings?|timeout)|"
    r"screen\s+(sleep|timeout)|"
    r"put\s+.+\s+to\s+sleep"
    r")\b"
)

_PREF_UTTERANCE_RE = re.compile(
    r"(?i)\b("
    r"dark\s*mode|light\s*mode|defaults\s+write|"
    r"system\s+preference|change\s+(the\s+)?(dock|appearance)|"
    r"set\s+(the\s+)?(dock|appearance|theme)"
    r")\b"
)

# Common app aliases → process / app name for manage_system_resources kill.
_APP_ALIASES: dict[str, str] = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "chromium": "Chromium",
    "safari": "Safari",
    "firefox": "Firefox",
    "edge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
    "slack": "Slack",
    "spotify": "Spotify",
    "code": "Code",
    "vscode": "Code",
    "visual studio code": "Code",
    "terminal": "Terminal",
    "iterm": "iTerm2",
    "iterm2": "iTerm2",
    "finder": "Finder",
    "mail": "Mail",
    "messages": "Messages",
    "notes": "Notes",
    "music": "Music",
    "zoom": "zoom.us",
    "discord": "Discord",
    "notion": "Notion",
    "cursor": "Cursor",
}

_CHAT_RE = re.compile(
    r"(?i)^\s*("
    r"yo+|hey+|hi+|hello+|sup+|howdy|thanks|thank\s+you|thx|"
    r"ok|okay|cool|nice|great|awesome|lol|haha|"
    r"good\s+(morning|afternoon|evening|night)|"
    r"how\s+are\s+you|what'?s\s+up|wassup"
    r")[\s!.?]*$"
)

_META_RE = re.compile(
    r"(?i)\b("
    r"what\s+can\s+(you|u|ya)\s+do|"
    r"what\s+are\s+(you|u)\s+(able|capable)|"
    r"your\s+capabilities|what\s+capabilities|"
    r"help\s+me\s+understand|"
    r"what\s+things\s+can\s+(you|u)|"
    r"by\s+yourself|who\s+are\s+(you|u)|"
    r"what\s+are\s+(you|u)|introduce\s+yourself"
    r")\b"
)

_ABOUT_ME_RE = re.compile(
    r"(?i)\b("
    r"what\s+do\s+(you|u)\s+know\s+about\s+me|"
    r"what\s+do\s+(you|u)\s+remember\s+about\s+me|"
    r"tell\s+me\s+about\s+(myself|me)\b|"
    r"who\s+am\s+i\b|"
    r"what('?s|\s+is)\s+my\s+(profile|bio|background)\b|"
    r"do\s+(you|u)\s+know\s+(who\s+i\s+am|me)\b"
    r")"
)

_DESTRUCTIVE_UTTERANCE_RE = re.compile(
    r"(?i)\b("
    r"shut\s*down|turn\s+off|power\s+off|restart|reboot|"
    r"empty\s+(the\s+)?(bin|trash)|clear\s+(the\s+)?(bin|trash)|"
    r"\brm\b|\bdelete\b|\bremove\b|delete\s+all|wipe|"
    r"put\s+.+\s+to\s+sleep|sleep\s+(my\s+)?(mac|pc|computer)|go\s+to\s+sleep|"
    r"log\s*out"
    r")\b"
)

_DISCOVERY_BASH_RE = re.compile(
    r"(?i)(^|[;&|]\s*|\n\s*)(ls|find|mdfind|locate|stat|du|head|tail|file|wc|dirname|basename)\b"
)

_DELETE_RE = re.compile(r"(?i)\b(delete|remove|rm|trash)\b")
_MOVE_RE = re.compile(r"(?i)\b(move|relocate|transfer)\b")
_COPY_RE = re.compile(r"(?i)\b(copy|duplicate)\b")
_PAST_QUERY_RE = re.compile(
    r"(?i)\b("
    r"what\s+did\s+i\s+(ask|say|tell\s+you)|"
    r"what\s+was\s+my\s+(last|previous)\s+(ask|question|request)|"
    r"last\s+time|"
    r"like\s+before|"
    r"continue\s+(from|where)|"
    r"earlier\s+(today|when)|"
    r"remember\s+what\s+i"
    r")\b"
)
_KNOWN_SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "yt": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "linkedin": "https://www.linkedin.com",
    "netflix": "https://www.netflix.com",
}
# Imperative open/launch — do NOT use bare \bopen\b (matches "open-source").
_OPEN_RE = re.compile(
    r"(?i)(?:^|\b)(?:can\s+you\s+|could\s+you\s+|please\s+|would\s+you\s+)?"
    r"(open|launch|start|reveal)\s+(?!source\b|sourced\b|to\b|minded\b|question\b)"
)
_OPEN_COMPOUND_RE = re.compile(
    r"(?i)\bopen[- ]source(d)?\b|"
    r"\bopen[- ]minded\b|"
    r"\bopenness\b|"
    r"\bopen to\b|"
    r"\bopen question\b|"
    r"\bin the open\b|"
    r"\b(any|some|an?)\s+open[- ]?(source(d)?|model|models|standard|protocol|api|license|software)\b"
)
_EMPTY_TRASH_RE = re.compile(
    r"(?i)\b(empty\s+(the\s+)?(bin|trash)|clear\s+(the\s+)?(bin|trash))\b"
)

_CAPABILITIES_TEXT = """Here's what I can do on your Mac:

• Answer questions (local model + web search when needed)
• Open apps and Chrome URLs
• Find files via Spotlight (fast) or shell
• Change system prefs (defaults) and power settings (pmset) without opening GUI
• List / kill top CPU & memory processes
• Send Notification Center alerts (even when the overlay is hidden)
• Empty Trash, shut down / restart / sleep — with your Approve first
• Control the UI (click, type, menus) when Accessibility is enabled
• Run short bash/Python for local tasks
• Remember notes you save in Preferences

Ask me like: “open Slack”, “spotlight invoice.pdf”, “notify me when done”, or “top CPU processes”."""


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
        subtasks = _decompose_compound_request(utterance)
        compound_state = {
            "subtasks": subtasks,
            "idx": 0,
            "results": [],
            "full_utterance": utterance,
        }
        self._compound_state = compound_state
        # Drop stale Approve cards from a previous (possibly hallucinated) action.
        clear_pending_actions()

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
        narrate("planning")

        for step in range(_MAX_ITERS):
            active = _active_subtask_utterance(compound_state)
            call = self._next_call(active, history, compound_state)
            if call is None:
                trace_step("agent_parse_fail", step=step, history_len=len(history))
                break

            tool = call["tool"]
            args = call.get("args") or {}

            # Hard stop: model invented a destructive command the user did not ask for.
            if tool == "run_bash":
                cmd = str(args.get("command") or "")
                if _command_is_destructive(cmd) and not _DESTRUCTIVE_UTTERANCE_RE.search(
                    utterance or ""
                ):
                    return self._finish(
                        utterance,
                        "I won't run a destructive action unless you ask for it "
                        "(e.g. “shut down my Mac” or “empty the bin”). "
                        "What did you want instead?",
                        sources,
                        history,
                    )

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

            # Special: answer “what do you know about me?” from Preferences notes.
            if tool == "__answer_about_user__":
                event_bus.publish(
                    utterance=utterance,
                    kind="action",
                    text="Reading your notes…",
                    detail="pending",
                    step="about_user",
                )
                reply = self.parser.answer_about_user(utterance)
                history.append(
                    {
                        "call": {"tool": "respond", "args": {"text": reply}},
                        "result": {"ok": True, "text": reply, "final": True},
                    }
                )
                event_bus.publish(
                    utterance=utterance,
                    kind="trace",
                    text="Answered from user notes",
                    detail="about_user",
                    step="about_user",
                    tool="respond",
                    tool_input={"utterance": utterance},
                    tool_output={"text": reply[:500]},
                )
                return self._finish(utterance, reply, sources, history)

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
            if tool == "web_search":
                narrate("researching")
            elif tool not in {"respond"}:
                narrate("acting")

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
                finished = self._attempt_finish(
                    utterance, last_text, sources, history
                )
                if finished is not None:
                    return finished
                continue

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

            # Stop thrashing: same failed tool+args repeated → surface error.
            if not result.get("ok") and _identical_failure_count(history) >= _MAX_IDENTICAL_FAILURES:
                err = str(result.get("error") or "tool failed")
                msg = (
                    f"That action failed repeatedly ({tool}): {err}. "
                    "Stopped retrying — try a different request."
                )
                return self._finish(utterance, msg, sources, history)

            if tool == "web_search" and isinstance(result.get("sources"), list):
                for s in result["sources"]:
                    if s not in sources:
                        sources.append(s)

            # Failed / empty web search
            if tool == "web_search" and not result.get("ok"):
                if _is_action_request(utterance):
                    # Keep going — try local tools without web context.
                    continue
                reply = self._local_answer(utterance)
                finished = self._attempt_finish(utterance, reply, sources, history)
                if finished is not None:
                    return finished
                continue

            if tool == "web_search" and result.get("ok") and result.get("context"):
                if _is_action_request(utterance):
                    # Research informs the next tool call — do NOT answer-only.
                    event_bus.publish(
                        utterance=utterance,
                        kind="action",
                        text="Research done — acting…",
                        detail="pending",
                    )
                    narrate("acting")
                    continue
                # Merge contexts from earlier searches so retries compound evidence.
                combined = _combined_search_context(history)
                reply = self.parser.answer_from_search(utterance, combined)
                searches_done = _web_search_count(history)
                if _answer_needs_more_search(reply) and searches_done < _MAX_WEB_SEARCHES:
                    history.append(
                        {
                            "call": {"tool": "_search_retry", "args": {}},
                            "result": {
                                "ok": False,
                                "incomplete": True,
                                "needs_more_search": True,
                                "reason": "web answer lacked enough evidence",
                                "candidate": (reply or "")[:400],
                            },
                        }
                    )
                    event_bus.publish(
                        utterance=utterance,
                        kind="action",
                        text=f"Need better sources — searching again ({searches_done}/{_MAX_WEB_SEARCHES})…",
                        detail="pending",
                    )
                    narrate("researching")
                    event_bus.publish(
                        utterance=utterance,
                        kind="trace",
                        text="Search answer insufficient — retrying",
                        detail="search_retry",
                        step="search_retry",
                        tool_output={"reason": "insufficient", "attempt": searches_done},
                    )
                    continue
                finished = self._attempt_finish(utterance, reply, sources, history)
                if finished is not None:
                    return finished
                continue

            if tool == "run_python":
                if result.get("ok") and (result.get("stdout") or "").strip():
                    out = str(result["stdout"]).strip()
                    # Math / one-liners stay as-is; otherwise format for the question.
                    if len(out) < 80 and "\n" not in out:
                        finished = self._attempt_finish(
                            utterance, out, sources, history
                        )
                    else:
                        text = self._format_command_answer(
                            utterance,
                            result.get("code") or "python",
                            out,
                        )
                        finished = self._attempt_finish(
                            utterance, text, sources, history
                        )
                    if finished is not None:
                        return finished
                    continue
                err = result.get("error") or result.get("stderr") or "code failed"
                reply = self._local_answer(utterance)
                finished = self._attempt_finish(
                    utterance,
                    reply or f"Code failed: {err}",
                    sources,
                    history,
                )
                if finished is not None:
                    return finished
                continue

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
                    # Discovery-only bash on an action ask → keep looping.
                    if _is_discovery_bash(cmd) and _goal_status(
                        active, history, text
                    ) == "incomplete":
                        event_bus.publish(
                            utterance=utterance,
                            kind="action",
                            text="Acting on results…",
                            detail="pending",
                        )
                        continue
                    finished = self._attempt_finish(
                        active, text, sources, history
                    )
                    if finished is not None:
                        return finished
                    continue
                if result.get("ok") and not out:
                    text = "Done." if not cmd else f"Done (`{cmd}`)."
                    finished = self._attempt_finish(
                        active, text, sources, history
                    )
                    if finished is not None:
                        return finished
                    continue
                finished = self._attempt_finish(
                    active,
                    f"Shell error: {err or 'command failed'}",
                    sources,
                    history,
                    force=True,
                )
                if finished is not None:
                    return finished
                continue

            if result.get("final") and result.get("text"):
                last_text = str(result["text"])
                finished = self._attempt_finish(
                    utterance, last_text, sources, history
                )
                if finished is not None:
                    return finished
                continue

            # Action tools that already did the work — wrap up without another LLM hop.
            if tool == "open_app":
                if result.get("not_found"):
                    msg = result.get("message") or (
                        f"I couldn't find that app. Which app did you mean?"
                    )
                    return self._finish(utterance, str(msg), sources, history)
                if result.get("ok"):
                    msg = result.get("message") or "Launched app."
                    finished = self._attempt_finish(
                        active, str(msg), sources, history
                    )
                    if finished is not None:
                        return finished
                    continue
                return self._finish(
                    utterance,
                    result.get("error") or result.get("message") or "Could not open app.",
                    sources,
                    history,
                )

            if tool == "open_url":
                if result.get("ok"):
                    msg = result.get("message") or f"Opened {result.get('url')}"
                    finished = self._attempt_finish(
                        active, str(msg), sources, history
                    )
                    if finished is not None:
                        return finished
                    continue
                return self._finish(
                    utterance,
                    result.get("error")
                    or "Couldn't open that URL in Chrome. Is Google Chrome installed?",
                    sources,
                    history,
                )

            if tool == "search_past_interactions":
                items = result.get("items") or []
                if not items:
                    msg = "I don't have any matching past interactions stored yet."
                else:
                    lines = []
                    for it in items[:8]:
                        u = str(it.get("utterance") or "")[:160]
                        a = str(it.get("answer") or it.get("result") or "")[:160]
                        when = str(it.get("created_at") or "")[:16]
                        lines.append(f"• [{when}] You asked: {u}\n  I answered: {a}")
                    msg = "Here's what I remember from earlier:\n\n" + "\n\n".join(lines)
                return self._finish(utterance, msg, sources, history)

            if tool in {
                "open_folder",
                "open_system_settings",
                "update_user_context",
                "modify_system_setting",
                "control_power_management",
                "trigger_native_notification",
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
                elif tool == "modify_system_setting":
                    msg = (
                        f"Updated {result.get('domain')}.{result.get('key')} "
                        f"= {result.get('value')}"
                    )
                elif tool == "control_power_management":
                    msg = (
                        f"Set pmset {result.get('setting')} "
                        f"to {result.get('value')}"
                    )
                elif tool == "trigger_native_notification":
                    msg = f"Notification sent: {result.get('title') or 'MacAgent'}"
                finished = self._attempt_finish(
                    utterance, str(msg), sources, history
                )
                if finished is not None:
                    return finished
                continue

            # UI tools: keep looping so the agent can multi-step; only stop on hard fail.
            if tool in {"ui_snapshot", "ui_click", "ui_type", "ui_key", "ui_menu"}:
                if not result.get("ok"):
                    err = _friendly_ui_error(result.get("error") or "UI action failed")
                    return self._finish(utterance, err, sources, history)
                # Successful UI step — continue toward respond / more clicks.
                continue

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
                    listing = header + "\n" + "\n".join(lines)
                    if _goal_status(utterance, history, listing) == "incomplete":
                        event_bus.publish(
                            utterance=utterance,
                            kind="action",
                            text="Acting on results…",
                            detail="pending",
                        )
                        continue
                    finished = self._attempt_finish(
                        utterance, listing, sources, history
                    )
                    if finished is not None:
                        return finished
                    continue
                finished = self._attempt_finish(
                    utterance,
                    "No matching files found.",
                    sources,
                    history,
                    force=True,

                )
                if finished is not None:
                    return finished
                continue

            if tool == "spotlight_file_search" and result.get("ok") and step >= 0:
                paths = result.get("paths") or []
                if paths:
                    lines = [f"{i}. {p}" for i, p in enumerate(paths[:15], 1)]
                    listing = (
                        f"Spotlight found {len(paths)} file(s):\n"
                        + "\n".join(lines)
                    )
                    if _goal_status(utterance, history, listing) == "incomplete":
                        event_bus.publish(
                            utterance=utterance,
                            kind="action",
                            text="Acting on results…",
                            detail="pending",
                        )
                        continue
                    finished = self._attempt_finish(
                        utterance, listing, sources, history
                    )
                    if finished is not None:
                        return finished
                    continue
                finished = self._attempt_finish(
                    utterance,
                    "No Spotlight matches found.",
                    sources,
                    history,
                    force=True,
                )
                if finished is not None:
                    return finished
                continue

            if tool == "manage_system_resources" and result.get("ok") and step >= 0:
                action = str(result.get("action") or "")
                if action == "list":
                    procs = result.get("processes") or []
                    if procs:
                        lines = [
                            f"{i}. {p.get('name')} (pid {p.get('pid')}) — "
                            f"CPU {p.get('cpu_percent')}% / "
                            f"{p.get('memory_mb')} MB"
                            for i, p in enumerate(procs, 1)
                        ]
                        listing = "Top processes:\n" + "\n".join(lines)
                    else:
                        listing = "No processes available."
                    finished = self._attempt_finish(
                        utterance, listing, sources, history, force=True
                    )
                    if finished is not None:
                        return finished
                    continue
                if action == "kill":
                    terminated = result.get("terminated") or []
                    names = ", ".join(
                        f"{t.get('name')}({t.get('pid')})" for t in terminated[:8]
                    )
                    msg = (
                        f"Terminated: {names}"
                        if names
                        else "Process termination requested."
                    )
                    finished = self._attempt_finish(
                        utterance, msg, sources, history, force=True
                    )
                    if finished is not None:
                        return finished
                    continue

        # Force a respond if the model never did.
        last_text = self._synthesize_from_history(utterance, history) or last_text
        if not last_text:
            last_text = "I could not complete that request."
        if _is_action_request(utterance) and _goal_status(
            utterance, history, last_text
        ) == "incomplete":
            last_text = (
                f"{last_text}\n\n"
                "I found information but could not finish the full action "
                "(permission may be required, or the next step failed)."
            )
        return self._finish(utterance, last_text, sources, history)

    def _next_call(
        self,
        utterance: str,
        history: list[dict[str, Any]],
        compound_state: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        # Heuristic shortcuts for clear patterns (saves latency on 1.5B).
        scoped = _history_since_last_subtask_boundary(history)
        if not scoped:
            quick = _heuristic_tool(utterance)
            if quick:
                return quick

        # After discovery, force the mutating follow-up when the goal is clear.
        follow = _forced_followup(utterance, scoped or history)
        if follow:
            return follow

        raw = self.parser.plan_tool_call(utterance, history, TOOL_CATALOG)
        parsed = _parse_tool_call(raw)
        if parsed:
            return _sanitize_planned_call(utterance, parsed)
        # Fallback: if we have web context already, respond; else chat/local — not web for "yo".
        if history:
            return {
                "tool": "respond",
                "args": {"text": self._synthesize_from_history(utterance, history)},
            }
        if _CHAT_RE.match((utterance or "").strip()) or len((utterance or "").split()) <= 2:
            return {
                "tool": "respond",
                "args": {
                    "text": "Hey — I'm here. What do you want to do?"
                },
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
            if tool == "spotlight_file_search" and result.get("paths"):
                paths = result["paths"]
                lines = "\n".join(f"- {p}" for p in paths[:15])
                return f"Spotlight found {len(paths)} file(s):\n{lines}"
            if tool == "manage_system_resources" and result.get("processes"):
                lines = [
                    f"- {p.get('name')} (pid {p.get('pid')}): "
                    f"CPU {p.get('cpu_percent')}% / {p.get('memory_mb')} MB"
                    for p in (result.get("processes") or [])[:5]
                ]
                return "Top processes:\n" + "\n".join(lines)
            if tool == "manage_system_resources" and result.get("terminated"):
                names = ", ".join(
                    f"{t.get('name')}({t.get('pid')})"
                    for t in (result.get("terminated") or [])[:8]
                )
                return f"Terminated: {names}" if names else "Termination done."
            if tool == "trigger_native_notification" and result.get("ok"):
                return f"Notification sent: {result.get('title') or 'MacAgent'}"
            if tool == "modify_system_setting" and result.get("ok"):
                return (
                    f"Updated {result.get('domain')}.{result.get('key')} "
                    f"= {result.get('value')}"
                )
            if tool == "control_power_management" and result.get("ok"):
                return f"Set pmset {result.get('setting')} to {result.get('value')}"
            if result.get("message"):
                return str(result["message"])
            if result.get("text") and result.get("ok", True):
                return str(result["text"])
            if tool == "get_user_context" and result.get("notes") is not None:
                notes = (result.get("notes") or "").strip() or "(empty)"
                return f"Current notes:\n{notes}"
            if tool == "search_past_interactions" and result.get("ok"):
                items = result.get("items") or []
                if not items:
                    return "No matching past interactions found."
                lines = []
                for it in items[:8]:
                    u = str(it.get("utterance") or "")[:120]
                    r = str(it.get("result") or it.get("answer") or "")[:120]
                    when = str(it.get("created_at") or "")[:16]
                    lines.append(f"- [{when}] You: {u}\n  Agent: {r}")
                return "Past interactions:\n" + "\n".join(lines)
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

    def _attempt_finish(
        self,
        utterance: str,
        text: str,
        sources: list[Any],
        history: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Finish if the goal is done; otherwise return None so the loop continues."""
        compound = getattr(self, "_compound_state", None)
        full_utterance = (
            str(compound.get("full_utterance") or utterance) if compound else utterance
        )
        goal_utterance = utterance
        if compound and len(compound.get("subtasks") or []) > 1:
            goal_utterance = _active_subtask_utterance(compound)

        if force or not _is_action_request(goal_utterance):
            return self._maybe_finish_compound(
                full_utterance, text, sources, history, compound, force=True
            )

        status = _goal_status(goal_utterance, history, text)
        reason: Optional[str] = None
        next_hint = ""
        if status == "incomplete":
            reason = _goal_incomplete_reason(goal_utterance, history, text)
        elif status == "unknown":
            # Secondary LLM critic when rules are inconclusive.
            try:
                check = self.parser.check_goal_done(
                    goal_utterance,
                    _history_summary_for_critic(history),
                    text,
                    is_action_request=_is_action_request(goal_utterance),
                )
            except Exception:  # noqa: BLE001
                check = None
            if isinstance(check, dict) and check.get("done") is False:
                reason = str(check.get("reason") or "goal not completed")
                next_hint = str(check.get("next_hint") or "")

        if reason:
            # Avoid infinite incomplete loops on the same candidate.
            recent_incomplete = sum(
                1
                for h in history
                if (h.get("result") or {}).get("incomplete")
            )
            if recent_incomplete >= 2:
                return self._maybe_finish_compound(
                    full_utterance,
                    (
                        f"{text}\n\n"
                        f"I couldn't complete the full request ({reason})."
                    ),
                    sources,
                    history,
                    compound,
                    force=True,
                )
            history.append(
                {
                    "call": {"tool": "_goal_check", "args": {}},
                    "result": {
                        "ok": False,
                        "incomplete": True,
                        "reason": reason,
                        "next_hint": next_hint,
                        "candidate": (text or "")[:500],
                    },
                }
            )
            event_bus.publish(
                utterance=full_utterance,
                kind="trace",
                text=f"Goal incomplete — {reason}",
                detail="goal_check",
                step="goal_check",
                tool_output={"reason": reason, "next_hint": next_hint},
            )
            event_bus.publish(
                utterance=full_utterance,
                kind="action",
                text="Acting on results…",
                detail="pending",
            )
            narrate("acting")
            return None
        return self._maybe_finish_compound(
            full_utterance, text, sources, history, compound
        )

    def _maybe_finish_compound(
        self,
        full_utterance: str,
        text: str,
        sources: list[Any],
        history: list[dict[str, Any]],
        compound: Optional[dict[str, Any]],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Finish, or advance to the next compound subtask."""
        if not compound or len(compound.get("subtasks") or []) <= 1:
            return self._finish(full_utterance, text, sources, history)

        idx = int(compound.get("idx") or 0)
        subtasks: list[str] = compound["subtasks"]
        active = subtasks[idx] if idx < len(subtasks) else full_utterance

        if force:
            return self._finish(full_utterance, text, sources, history)

        if not force and _is_action_request(active):
            scoped = _history_since_last_subtask_boundary(history)
            if _goal_status(active, scoped or history, text) == "incomplete":
                return None  # type: ignore[return-value]

        results: list[str] = compound.setdefault("results", [])
        results.append(text)
        history.append(
            {
                "call": {"tool": "_subtask_done", "args": {"index": idx}},
                "result": {"ok": True, "text": text[:500]},
            }
        )
        compound["idx"] = idx + 1
        if compound["idx"] >= len(subtasks):
            combined = "\n".join(r for r in results if r)
            return self._finish(full_utterance, combined, sources, history)

        event_bus.publish(
            utterance=full_utterance,
            kind="action",
            text=f"Next step ({compound['idx'] + 1}/{len(subtasks)})…",
            detail="pending",
        )
        narrate("acting")
        return None  # type: ignore[return-value]

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
        try:
            narrate_answer(text or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug("tts answer skipped: %s", exc)
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


def _resolve_app_name(raw: str) -> str:
    """Map user phrasing (chrome, browser) to a process name."""
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    key = re.sub(r"^(the|a|an|my)\s+", "", key).strip()
    key = re.sub(r"\s+(app|application|browser|process)$", "", key).strip()
    if key in {"browser", "web browser"}:
        return "Google Chrome"
    if key in _APP_ALIASES:
        return _APP_ALIASES[key]
    # Substring alias: "google chrome browser" → chrome
    for alias, resolved in sorted(_APP_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in key:
            return resolved
    return (raw or "").strip()


def _close_app_heuristic(text: str) -> Optional[dict[str, Any]]:
    """Deterministic close/quit/kill for apps — 1.5B often invents pmset instead."""
    if not re.search(
        r"(?i)\b(close|quit|kill|force[\s-]?quit|terminate|exit|shut)\b",
        text,
    ):
        return None
    if re.search(r"(?i)\b(window|tab|file|folder|document)\b", text) and not re.search(
        r"(?i)\b(chrome|safari|firefox|browser|slack|spotify|app|application)\b",
        text,
    ):
        return None

    # Prefer known app names anywhere in the utterance.
    known = re.search(
        r"(?i)\b(?P<name>google\s*chrome|microsoft\s*edge|visual\s*studio\s*code|"
        r"chrome|safari|firefox|edge|slack|spotify|discord|zoom|notion|"
        r"cursor|vscode|iterm2?|finder|mail|messages|notes|music|browser)\b",
        text,
    )
    if known:
        return {
            "tool": "manage_system_resources",
            "args": {
                "action": "kill",
                "target_process": _resolve_app_name(known.group("name")),
            },
        }

    # Generic “close X” / “quit the X app”
    m = re.search(
        r"(?i)\b(?:close|quit|kill|force[\s-]?quit|terminate|exit)\b"
        r"(?:\s+(?:the|my|a))?"
        r"(?:\s+(?:app|application|process|browser))?"
        r"\s+(?P<name>[\w.\-]+(?:\s+[\w.\-]+){0,3})"
        r"(?:\s+(?:app|application|browser|process|for\s+me))?$",
        text.strip().rstrip(".?!"),
    )
    if not m:
        return None
    name = m.group("name").strip().rstrip(".?!")
    name = re.sub(r"(?i)\s+(for\s+me|please)$", "", name).strip()
    if not name or len(name) > 64:
        return None
    # Avoid treating “close enough” / “close the loop” as apps.
    if name.lower() in {"enough", "the loop", "loop", "me", "it"}:
        return None
    return {
        "tool": "manage_system_resources",
        "args": {
            "action": "kill",
            "target_process": _resolve_app_name(name),
        },
    }


def _identical_failure_count(history: list[dict[str, Any]]) -> int:
    """How many times the latest failed tool+args appears consecutively at the end."""
    if not history:
        return 0
    last = history[-1]
    last_call = last.get("call") or {}
    last_result = last.get("result") or {}
    if last_result.get("ok", True):
        return 0
    tool = last_call.get("tool")
    args = last_call.get("args") or {}
    count = 0
    for item in reversed(history):
        call = item.get("call") or {}
        result = item.get("result") or {}
        if call.get("tool") != tool or (call.get("args") or {}) != args:
            break
        if result.get("ok", True):
            break
        count += 1
    return count


def _heuristic_tool(utterance: str) -> Optional[dict[str, Any]]:
    text = (utterance or "").strip()
    lower = text.lower()
    if not text:
        return None

    # Capability / help questions — never run UI automation for these.
    if _META_RE.search(text):
        return {"tool": "respond", "args": {"text": _CAPABILITIES_TEXT}}

    # “What do you know about me?” — use Preferences notes, not a flaky web refuse.
    if _ABOUT_ME_RE.search(text):
        return {"tool": "__answer_about_user__", "args": {}}

    # Greetings / small talk — never invent tools.
    if _CHAT_RE.match(text):
        return {
            "tool": "respond",
            "args": {
                "text": "Hey — I'm MacAgent. Ask me anything, or say “what can you do?” for a quick list."
            },
        }

    # Simple arithmetic — answer locally, skip web search.
    math = _try_simple_math(text)
    if math is not None and re.search(r"(?i)[\+\-\*\/x×÷]", text):
        return {"tool": "respond", "args": {"text": math}}

    # Close / quit / kill an app or browser (before planner can invent pmset).
    close_call = _close_app_heuristic(text)
    if close_call:
        return close_call

    # Native Notification Center — “notify me …”, “send a notification …”
    notify_m = re.search(
        r"(?i)\b("
        r"notify\s+me|"
        r"send\s+(me\s+)?(a\s+)?notification|"
        r"show\s+(me\s+)?(a\s+)?notification|"
        r"alert\s+me|"
        r"desktop\s+notification"
        r")\b",
        text,
    )
    if notify_m:
        msg = text
        for pat in (
            r"(?i)^(please\s+)?(can\s+you\s+|could\s+you\s+)?",
            r"(?i)\b(notify\s+me(\s+(that|about|when|with))?|"
            r"send\s+(me\s+)?(a\s+)?notification(\s+(that|about|saying|with))?|"
            r"show\s+(me\s+)?(a\s+)?notification(\s+(that|about|saying|with))?|"
            r"alert\s+me(\s+(that|about|when|with))?|"
            r"desktop\s+notification(\s+(that|about|saying|with))?)\s*",
        ):
            msg = re.sub(pat, "", msg).strip()
        msg = msg.strip(" .,:;\"'") or "Done"
        play = bool(re.search(r"(?i)\b(sound|beep|ping|chime)\b", text))
        return {
            "tool": "trigger_native_notification",
            "args": {
                "title": "MacAgent",
                "subtitle": "",
                "message": msg[:200],
                "play_sound": play,
            },
        }

    # Process monitor — top CPU / memory / kill process
    if re.search(
        r"(?i)\b("
        r"top\s+(cpu|memory|ram|processes|apps)|"
        r"(cpu|memory|ram)\s+(hogs?|usage|processes)|"
        r"what('?s|\s+is)\s+using\s+(my\s+)?(cpu|memory|ram)|"
        r"list\s+(running\s+)?processes|"
        r"system\s+resources|"
        r"resource\s+usage"
        r")\b",
        text,
    ):
        return {"tool": "manage_system_resources", "args": {"action": "list"}}

    kill_m = re.search(
        r"(?i)\b(kill|quit|force[\s-]?quit|terminate|close)\s+"
        r"(the\s+)?(process\s+)?(?P<name>[\w.\- ]+?)(?:\s+process)?\s*$",
        text,
    )
    if kill_m and not re.search(r"(?i)\b(kill\s+me|quit\s+asking|close\s+enough)\b", text):
        name = kill_m.group("name").strip().rstrip(".?!")
        name = re.sub(r"(?i)^(called|named|the)\s+", "", name).strip()
        name = re.sub(r"(?i)\s+(browser|app|application)$", "", name).strip()
        if name and len(name) < 64:
            resolved = _resolve_app_name(name)
            return {
                "tool": "manage_system_resources",
                "args": {"action": "kill", "target_process": resolved},
            }

    # Spotlight file search — prefer mdfind tool over bash find
    spotlight_m = re.search(
        r"(?i)\b("
        r"spotlight(\s+search)?|"
        r"mdfind|"
        r"search\s+(my\s+)?(mac|computer|disk|system)\s+for|"
        r"find\s+(the\s+)?file\b"
        r")\b",
        text,
    )
    if spotlight_m or (
        re.search(r"(?i)\b(find|locate|search\s+for)\b", text)
        and re.search(
            r"(?i)\b(file|pdf|doc|docx|xlsx?|png|jpg|jpeg|csv|txt|folder)\b",
            text,
        )
        and not re.search(r"(?i)\b(download|downloads)\b", text)
    ):
        q = text
        q = re.sub(
            r"(?i)^(please\s+)?(can\s+you\s+|could\s+you\s+)?",
            "",
            q,
        ).strip()
        q = re.sub(
            r"(?i)\b("
            r"spotlight(\s+search)?|"
            r"mdfind|"
            r"search\s+(my\s+)?(mac|computer|disk|system)\s+for|"
            r"find\s+(the\s+)?(file|folder)|"
            r"find|locate|search\s+for|show\s+me"
            r")\b",
            " ",
            q,
        )
        q = re.sub(
            r"(?i)\b(file|pdf|doc|folder|on\s+my\s+mac|please)\b",
            " ",
            q,
        )
        q = re.sub(r"\s+", " ", q).strip(" .,:;\"'")
        if q:
            return {"tool": "spotlight_file_search", "args": {"query": q}}

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

    # Power actions — only when clearly requested (never invent from "yo").
    if re.search(
        r"(?i)("
        r"\b(shut\s*down|turn\s+off|power\s+off)\b.+\b(mac|pc|computer|machine|system)\b|"
        r"\b(shut\s*down|turn\s+off|power\s+off)\s+(my\s+)?(mac|pc|computer|machine|system)\b|"
        r"^(please\s+)?(can\s+you\s+)?(shut\s*down|turn\s+off|power\s+off)(\s+now)?\s*[.!]?\s*$"
        r")",
        text,
    ):
        return {"tool": "run_bash", "args": {"command": shutdown_command()}}
    if re.search(
        r"(?i)("
        r"\b(restart|reboot)\b.+\b(mac|pc|computer|machine|system)\b|"
        r"\b(restart|reboot)\s+(my\s+)?(mac|pc|computer|machine|system)\b"
        r")",
        text,
    ):
        return {"tool": "run_bash", "args": {"command": restart_command()}}
    if re.search(
        r"(?i)\b("
        r"put\s+(the\s+|my\s+)?(mac|pc|computer|machine)\s+to\s+sleep|"
        r"sleep\s+(my\s+)?(mac|pc|computer|machine)|"
        r"go\s+to\s+sleep"
        r")\b",
        text,
    ):
        return {"tool": "run_bash", "args": {"command": sleep_command()}}

    # Recently downloaded / Downloads — bash, not a special-case tool.
    if re.search(
        r"(?i)\b("
        r"recent(ly)?\s+download|download(ed)?\s+(item|file|folder|stuff)?|"
        r"last\s+download|latest\s+download|newest\s+in\s+downloads|"
        r"what('?s|\s+is|\s+are)?\s+(the\s+)?(most\s+recent(ly)?|latest)\b|"
        r"what('?s| did i)\s+download|"
        r"find( me)?\s+(my\s+)?recent(ly)?\s+download|"
        r"show( me)?\s+(my\s+)?downloads|"
        r"file\s+that\s+i\s+download"
        r")\b",
        text,
    ) and re.search(r"(?i)download", text):
        singular = bool(
            re.search(r"(?i)\b(most\s+recent(ly)?|last|newest|latest)\b", text)
            and not re.search(r"(?i)\b(list|show\s+all|all\s+my)\b", text)
        )
        if _DELETE_RE.search(text):
            return {
                "tool": "run_bash",
                "args": {"command": _delete_latest_download_command()},
            }
        if _MOVE_RE.search(text) or _COPY_RE.search(text):
            verb = "cp" if _COPY_RE.search(text) else "mv"
            dest = _resolve_move_destination(text)
            return {
                "tool": "run_bash",
                "args": {"command": _move_latest_download_command(dest, verb=verb)},
            }
        if _is_open_action(text):
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
    if folder_m and (
        folder_m.group(1).lower() in {"show", "reveal"} or _is_open_action(text)
    ):
        name = folder_m.group(4).strip().rstrip(".")
        name = re.sub(r"(?i)^(named|called)\s+", "", name).strip()
        return {"tool": "open_folder", "args": {"query": name}}
    folder_m2 = re.search(
        r"(?i)^\s*(open|show|reveal)\s+(.+?)\s+(folder|directory|dir)\s*$",
        text,
    )
    if folder_m2 and (
        folder_m2.group(1).lower() in {"show", "reveal"} or _is_open_action(text)
    ):
        name = folder_m2.group(2).strip()
        name = re.sub(r"(?i)^(the|a|my)\s+", "", name).strip()
        return {"tool": "open_folder", "args": {"query": name}}

    # File / folder search — let the model invent a bash command.
    if re.search(
        r"(?i)\b(find|locate|search for|show me)\b.+\b(file|pdf|doc|folder|invoice|item|download)\b",
        text,
    ) or re.search(
        r"(?i)\b(find|locate)\s+(my\s+)?[\w.\- ]+\.(pdf|docx?|xlsx?|png|jpg)\b",
        text,
    ) or re.search(r"(?i)\b(run|execute)\b.+\b(bash|shell|command|terminal)\b", text):
        return {"tool": "__write_and_run_bash__", "args": {}}

    if _is_open_action(text) and re.search(
        r"(?i)\b(open|launch|start)\s+(system\s+)?(settings|preferences)\b", lower
    ):
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

    # Past interaction lookup — "what did I ask last time?"
    if _PAST_QUERY_RE.search(text):
        q = text
        q = re.sub(r"(?i)^(what\s+did\s+i\s+(ask|say|tell\s+you)\??\s*)", "", q).strip()
        return {"tool": "search_past_interactions", "args": {"query": q or text, "limit": 8}}

    # Compound open: "open YouTube and comp3370 folder" — before single-target open.
    compound_open = _heuristic_compound_open(text)
    if compound_open:
        return compound_open

    # "can you open …" / "please open …" / "open …" — not "open-source" questions
    if _is_open_action(text):
        m = re.search(
            r"(?i)(?:^|\b)(?:can\s+you\s+|could\s+you\s+|please\s+)?(open|launch|start)\s+(.+)$",
            _scrub_open_compounds(text),
        )
        if m:
            target = m.group(2).strip().rstrip(".?!")
            if target and not re.match(r"(?i)^source(d)?\b", target):
                if "setting" in target.lower() or "preference" in target.lower():
                    return {
                        "tool": "open_system_settings",
                        "args": {"pane": "general"},
                    }
                opened = _heuristic_open_target(target)
                if opened:
                    return opened

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


def _command_is_destructive(command: str) -> bool:
    from tools.run_bash import classify_command

    return classify_command(command) in {"needs_confirm", "hard_block"}


def _sanitize_planned_call(
    utterance: str, call: dict[str, Any]
) -> dict[str, Any]:
    """Block hallucinated destructive / UI tools on casual chat."""
    tool = (call.get("tool") or "").strip()
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    text = (utterance or "").strip()

    if _CHAT_RE.match(text) or _META_RE.search(text):
        if tool != "respond":
            if _META_RE.search(text):
                return {"tool": "respond", "args": {"text": _CAPABILITIES_TEXT}}
            return {
                "tool": "respond",
                "args": {
                    "text": "Hey — I'm MacAgent. Ask me anything, or say “what can you do?”"
                },
            }

    if _ABOUT_ME_RE.search(text) and tool in {
        "web_search",
        "ui_snapshot",
        "ui_click",
        "run_bash",
        "open_url",
    }:
        return {"tool": "__answer_about_user__", "args": {}}

    if tool == "run_bash":
        cmd = str(args.get("command") or "")
        if _command_is_destructive(cmd) and not _DESTRUCTIVE_UTTERANCE_RE.search(text):
            return {
                "tool": "respond",
                "args": {
                    "text": (
                        "I almost ran a system action you didn't ask for — skipped it. "
                        "What would you like me to do?"
                    )
                },
            }

    if tool in {"ui_snapshot", "ui_click", "ui_type", "ui_key", "ui_menu"}:
        if not _is_action_request(text) or _CHAT_RE.match(text):
            return {
                "tool": "respond",
                "args": {
                    "text": (
                        "I don't need screen control for that. "
                        "Ask a question or tell me what to open/do."
                    )
                },
            }

    # Don't invent system mutations / kills on casual chat.
    if tool in {
        "modify_system_setting",
        "control_power_management",
        "manage_system_resources",
        "trigger_native_notification",
    }:
        if _CHAT_RE.match(text) or (
            tool == "manage_system_resources"
            and str(args.get("action") or "").lower() == "kill"
            and not re.search(r"(?i)\b(kill|quit|force[\s-]?quit|terminate|close)\b", text)
        ):
            return {
                "tool": "respond",
                "args": {
                    "text": (
                        "I almost ran a system action you didn't ask for — skipped it. "
                        "What would you like me to do?"
                    )
                },
            }

    # Catalog-example hijack: pmset / defaults only when the user asked for that.
    if tool == "control_power_management" and not _POWER_UTTERANCE_RE.search(text):
        close = _close_app_heuristic(text)
        if close:
            return close
        return {
            "tool": "respond",
            "args": {
                "text": (
                    "I won't change power/sleep settings unless you ask for that. "
                    "If you meant to close an app, say e.g. “close Chrome”."
                )
            },
        }

    if tool == "modify_system_setting" and not _PREF_UTTERANCE_RE.search(text):
        close = _close_app_heuristic(text)
        if close:
            return close
        if re.search(r"(?i)\b(close|quit|kill)\b", text):
            return {
                "tool": "respond",
                "args": {
                    "text": (
                        "To close an app, ask me to quit it by name "
                        "(e.g. “close Chrome”) — I won't change system prefs for that."
                    )
                },
            }

    return call


def _friendly_ui_error(raw: str) -> str:
    lower = (raw or "").lower()
    if (
        "assistive" in lower
        or "not allowed" in lower
        or "accessibility" in lower
        or "osascript is not allowed" in lower
    ):
        return (
            "I need Accessibility permission to control the screen "
            "(click/type). Open Preferences → Permissions → "
            "Open Accessibility Settings, enable MacAgent once, then try again.\n\n"
            "For a list of what I can do without that, ask: “what can you do?”"
        )
    return (raw or "UI action failed").strip()


def _is_discovery_bash(command: str) -> bool:
    cmd = (command or "").strip()
    if not cmd:
        return False
    # Mutating commands are never "discovery-only".
    if re.search(r"(?i)\b(rm|rmdir|mv|cp|mkdir|touch|kill|killall|osascript)\b", cmd):
        return False
    return bool(_DISCOVERY_BASH_RE.search(cmd))


def _history_has_successful_mutation(history: list[dict[str, Any]]) -> bool:
    for item in history:
        call = item.get("call") or {}
        result = item.get("result") or {}
        tool = call.get("tool")
        if tool in {
            "open_app",
            "open_url",
            "open_folder",
            "open_system_settings",
            "modify_system_setting",
            "control_power_management",
            "trigger_native_notification",
        }:
            if result.get("ok"):
                return True
        if tool == "manage_system_resources" and result.get("ok"):
            action = str(
                result.get("action")
                or (call.get("args") or {}).get("action")
                or ""
            ).lower()
            if action == "kill":
                return True
        if tool == "run_bash":
            cmd = str((call.get("args") or {}).get("command") or result.get("command") or "")
            if result.get("needs_confirm"):
                return True  # confirm gate is the correct terminal for delete
            if result.get("ok") and not _is_discovery_bash(cmd):
                if re.search(
                    r"(?i)\b(rm|rmdir|mv|empty|trash|shut\s*down|restart|sleep)\b",
                    cmd,
                ):
                    return True
            out = str(result.get("stdout") or "")
            if result.get("ok") and re.search(
                r"(?i)^(deleted|removed|emptied|moved|opened):", out
            ):
                return True
    return False


def _history_has_command_containing(history: list[dict[str, Any]], needle: str) -> bool:
    n = needle.lower()
    for item in history:
        call = item.get("call") or {}
        if call.get("tool") != "run_bash":
            continue
        cmd = str((call.get("args") or {}).get("command") or "")
        if n in cmd.lower():
            return True
    return False


def _goal_status(
    utterance: str, history: list[dict[str, Any]], candidate: str
) -> str:
    """Return 'done' | 'incomplete' | 'unknown'."""
    text = (utterance or "").strip()
    if not text or not _is_action_request(text):
        return "done"

    cand = (candidate or "").strip()

    if _EMPTY_TRASH_RE.search(text):
        if _history_has_command_containing(history, "empty the trash") or any(
            (h.get("result") or {}).get("needs_confirm") for h in history
        ):
            return "done"
        return "incomplete"

    if _DELETE_RE.search(text):
        if _history_has_successful_mutation(history):
            return "done"
        if re.search(
            r"(?i)your most recent download is|most recent (file|folder) is|"
            r"^found \d+|most recent in downloads",
            cand,
        ):
            return "incomplete"
        # Only discovery so far.
        only_discovery = True
        saw_tool = False
        for item in history:
            call = item.get("call") or {}
            tool = call.get("tool")
            if tool in {"_goal_check"}:
                continue
            if tool == "respond":
                continue
            saw_tool = True
            if tool == "find_files":
                continue
            if tool == "spotlight_file_search":
                continue
            if tool == "run_bash":
                cmd = str((call.get("args") or {}).get("command") or "")
                if _is_discovery_bash(cmd):
                    continue
                only_discovery = False
                break
            only_discovery = False
            break
        if saw_tool and only_discovery:
            return "incomplete"
        if not saw_tool and cand:
            return "incomplete"
        return "unknown"

    if _MOVE_RE.search(text) or _COPY_RE.search(text):
        if _history_has_successful_mutation(history):
            return "done"
        if re.search(
            r"(?i)your most recent download is|most recent (file|folder) is|"
            r"^found \d+|most recent in downloads",
            cand,
        ):
            return "incomplete"
        only_discovery = True
        saw_tool = False
        for item in history:
            call = item.get("call") or {}
            tool = call.get("tool")
            if tool in {"_goal_check", "_subtask_done"}:
                continue
            if tool == "respond":
                continue
            saw_tool = True
            if tool in {"find_files", "spotlight_file_search"}:
                continue
            if tool == "run_bash":
                cmd = str((call.get("args") or {}).get("command") or "")
                if _is_discovery_bash(cmd):
                    continue
                if re.search(r"(?i)\b(mv|cp)\b", cmd):
                    return "done"
                only_discovery = False
                break
            only_discovery = False
            break
        if saw_tool and only_discovery:
            return "incomplete"
        if not saw_tool and cand:
            return "incomplete"
        return "unknown"

    if _is_open_action(text) and not _DELETE_RE.search(text):
        # "open my latest download" etc.
        if any(
            (h.get("call") or {}).get("tool")
            in {"open_app", "open_url", "open_folder", "open_system_settings"}
            and (h.get("result") or {}).get("ok")
            for h in history
        ):
            return "done"
        if any(
            (h.get("call") or {}).get("tool") == "run_bash"
            and (h.get("result") or {}).get("ok")
            and re.search(
                r"(?i)\bopen\b",
                str(((h.get("call") or {}).get("args") or {}).get("command") or ""),
            )
            for h in history
        ):
            return "done"
        # Listing-only answers are incomplete for open asks.
        if re.search(
            r"(?i)your most recent download is|most recent in downloads|^found \d+",
            cand,
        ):
            return "incomplete"
        return "unknown"

    return "unknown"


def _goal_incomplete_reason(
    utterance: str, history: list[dict[str, Any]], candidate: str
) -> str:
    if _DELETE_RE.search(utterance or ""):
        return "listed or found the target but did not delete it yet"
    if _MOVE_RE.search(utterance or ""):
        return "found the target but did not move it yet"
    if _COPY_RE.search(utterance or ""):
        return "found the target but did not copy it yet"
    if _EMPTY_TRASH_RE.search(utterance or ""):
        return "trash was not emptied yet"
    if _is_open_action(utterance or ""):
        return "found the target but did not open it yet"
    return "action not completed yet"


def _delete_latest_download_command() -> str:
    return (
        'f=$(ls -t ~/Downloads/* 2>/dev/null | head -1); '
        'if [ -n "$f" ]; then rm -- "$f"; echo "Deleted: $f"; '
        'else echo "Downloads is empty"; fi'
    )


def _move_latest_download_command(dest: str = "~/Desktop", *, verb: str = "mv") -> str:
    import shlex

    d = shlex.quote(dest)
    v = "cp" if verb.lower() == "cp" else "mv"
    return (
        'f=$(ls -t ~/Downloads/* 2>/dev/null | head -1); '
        f'if [ -n "$f" ]; then {v} -- "$f" {d} && echo "Moved: $f -> {dest}"; '
        f'else echo "Downloads is empty"; fi'
    )


def _resolve_move_destination(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"(?i)\bdesktop\b", t):
        return "~/Desktop"
    m = re.search(r"(?i)\bto\s+(~/[\w./-]+|/[\w./-]+|[\w][\w./-]*)", text or "")
    if m:
        dest = m.group(1).strip().rstrip(".")
        if not dest.startswith("~") and not dest.startswith("/"):
            dest = f"~/{dest}"
        return dest
    return "~/Desktop"


def _path_from_ls_stdout(stdout: str) -> Optional[str]:
    """Best-effort newest file name from `ls -lt` stdout under Downloads."""
    for line in (stdout or "").splitlines():
        line = line.rstrip()
        if not line or line.lower().startswith("total "):
            continue
        if line[0] not in "-dlbcps":
            continue
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        name = parts[8]
        if name in {".", ".."}:
            continue
        # ls -lt ~/Downloads prints basenames; resolve under Downloads.
        from pathlib import Path as _P

        home = _P.home() / "Downloads" / name
        return str(home)
    return None


def _first_path_from_history(history: list[dict[str, Any]]) -> Optional[str]:
    import shlex

    for item in reversed(history):
        call = item.get("call") or {}
        result = item.get("result") or {}
        tool = call.get("tool")
        if tool == "find_files":
            items = result.get("items") or []
            if items:
                p = items[0].get("path")
                if p:
                    return str(p)
            paths = result.get("paths") or []
            if paths:
                return str(paths[0])
        if tool == "spotlight_file_search":
            paths = result.get("paths") or []
            if paths:
                return str(paths[0])
        if tool == "run_bash" and result.get("ok"):
            cmd = str((call.get("args") or {}).get("command") or "")
            out = str(result.get("stdout") or "")
            if _is_discovery_bash(cmd) and "download" in cmd.lower():
                p = _path_from_ls_stdout(out)
                if p:
                    return p
            # echo Opened: /path
            m = re.search(r"(?i)(?:opened|deleted|moved|copied):\s*(.+)$", out, re.M)
            if m:
                return m.group(1).strip()
    _ = shlex  # silence if unused in some paths
    return None


def _forced_followup(
    utterance: str, history: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Deterministic next tool after discovery when the user goal is clear."""
    if not history:
        return None

    # Q&A: prior search answer was too thin — search again with a sharper query.
    if _history_needs_more_search(history) and _web_search_count(history) < _MAX_WEB_SEARCHES:
        query = _next_search_query(utterance, history)
        if query:
            return {"tool": "web_search", "args": {"query": query}}

    if _history_has_successful_mutation(history):
        return None
    text = utterance or ""

    # Close/quit was mis-planned (e.g. pmset) — force the kill tool once.
    close = _close_app_heuristic(text)
    if close:
        already_kill = any(
            (h.get("call") or {}).get("tool") == "manage_system_resources"
            and str(((h.get("call") or {}).get("args") or {}).get("action") or "").lower()
            == "kill"
            for h in history
        )
        if not already_kill:
            return close

    import shlex

    if _DELETE_RE.search(text):
        # Already queued/tried an rm — don't loop forever.
        if _history_has_command_containing(history, "rm "):
            return None
        path = _first_path_from_history(history)
        if path:
            q = shlex.quote(path)
            return {
                "tool": "run_bash",
                "args": {
                    "command": f"rm -- {q} && echo Deleted: {q}",
                },
            }
        if re.search(r"(?i)download", text):
            return {
                "tool": "run_bash",
                "args": {"command": _delete_latest_download_command()},
            }

    if _is_open_action(text) and re.search(r"(?i)download", text):
        if _history_has_command_containing(history, "open "):
            return None
        path = _first_path_from_history(history)
        if path:
            q = shlex.quote(path)
            return {
                "tool": "run_bash",
                "args": {
                    "command": f"open {q} && echo Opened: {q}",
                },
            }
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

    if _MOVE_RE.search(text) or _COPY_RE.search(text):
        verb = "cp" if _COPY_RE.search(text) else "mv"
        cmd_needle = f"{verb} "
        if _history_has_command_containing(history, cmd_needle):
            return None
        path = _first_path_from_history(history)
        dest = _resolve_move_destination(text)
        if path:
            q = shlex.quote(path)
            d = shlex.quote(dest)
            label = "Copied" if verb == "cp" else "Moved"
            return {
                "tool": "run_bash",
                "args": {
                    "command": f"{verb} -- {q} {d} && echo {label}: {q} -> {dest}",
                },
            }
        if re.search(r"(?i)download", text):
            return {
                "tool": "run_bash",
                "args": {"command": _move_latest_download_command(dest, verb=verb)},
            }

    return None


def _web_search_count(history: list[dict[str, Any]]) -> int:
    return sum(
        1
        for h in history
        if (h.get("call") or {}).get("tool") == "web_search"
    )


def _history_needs_more_search(history: list[dict[str, Any]]) -> bool:
    for item in reversed(history):
        result = item.get("result") or {}
        if result.get("needs_more_search"):
            return True
        tool = (item.get("call") or {}).get("tool")
        if tool == "web_search":
            break
    return False


def _prior_search_queries(history: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for item in history:
        call = item.get("call") or {}
        if call.get("tool") != "web_search":
            continue
        q = str((call.get("args") or {}).get("query") or "").strip()
        if q:
            out.append(q)
    return out


def _combined_search_context(history: list[dict[str, Any]], limit: int = 9000) -> str:
    blocks: list[str] = []
    for i, item in enumerate(history, 1):
        call = item.get("call") or {}
        result = item.get("result") or {}
        if call.get("tool") != "web_search":
            continue
        ctx = str(result.get("context") or "").strip()
        if not ctx:
            continue
        q = str((call.get("args") or {}).get("query") or "")
        blocks.append(f"=== Search {i}: {q} ===\n{ctx}")
    merged = "\n\n".join(blocks)
    return merged[:limit] if merged else ""


def _answer_needs_more_search(text: str) -> bool:
    """True when the grounded answer admits the sources weren't enough."""
    t = (text or "").strip()
    if not t:
        return True
    from llm.inference import _is_empty_refusal

    if _is_empty_refusal(t):
        return True
    # Extra patterns common on long "sorry, context lacks…" replies.
    return bool(
        re.search(
            r"(?i)("
            r"does not (?:contain|address|include)|"
            r"do not (?:contain|address|include)|"
            r"not address the specific|"
            r"would need (?:to search|more)|"
            r"need to search for|"
            r"lacks? (?:specific |enough )?(?:pricing|price|model)|"
            r"no (?:specific )?(?:pricing|price|models?)"
            r")",
            t,
        )
    )


def _next_search_query(utterance: str, history: list[dict[str, Any]]) -> Optional[str]:
    """Build a sharper DuckDuckGo query for the next search attempt."""
    prior = [p.lower() for p in _prior_search_queries(history)]
    text = (utterance or "").strip()
    lower = text.lower()
    candidates: list[str] = []

    wants_price = bool(re.search(r"(?i)\b(price|pricing|cost|subscription|\$)\b", text))
    wants_compare = bool(re.search(r"(?i)\b(compare|comparison|vs|versus|frontier)\b", text))
    mentions_ai = bool(
        re.search(
            r"(?i)\b(gpt|chatgpt|claude|gemini|llm|openai|anthropic|model)\b",
            text,
        )
    )

    if wants_price or wants_compare or mentions_ai:
        candidates.extend(
            [
                "GPT-4o Claude Sonnet Gemini API pricing comparison 2026",
                "OpenAI Anthropic Google frontier LLM pricing per million tokens",
                "ChatGPT Plus Claude Pro Gemini Advanced subscription price comparison",
            ]
        )
    if wants_price and not mentions_ai:
        candidates.append(f"{text} official pricing")
    # Tightened original ask is a fallback, not the first retry.
    tightened = re.sub(r"(?i)\b(please|can you|could you|compare all|out there)\b", " ", text)
    tightened = re.sub(r"\s+", " ", tightened).strip()
    if tightened and tightened.lower() != lower:
        candidates.append(tightened)
    candidates.append(f"{text} pricing specs 2026")

    for q in candidates:
        q = (q or "").strip()
        if not q:
            continue
        if q.lower() in prior:
            continue
        # Also skip near-duplicates of prior queries.
        if any(q.lower() in p or p in q.lower() for p in prior if len(p) > 12):
            continue
        return q
    # Last resort: append attempt number so DDG gets a fresh query string.
    n = _web_search_count(history) + 1
    fallback = f"{text} detailed facts attempt {n}"
    if fallback.lower() not in prior:
        return fallback
    return None


def _history_summary_for_critic(history: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, item in enumerate(history[-6:], 1):
        call = item.get("call") or {}
        result = item.get("result") or {}
        tool = call.get("tool")
        if result.get("incomplete"):
            lines.append(f"{i}. goal_check incomplete: {result.get('reason')}")
            continue
        cmd = ""
        if tool == "run_bash":
            cmd = str((call.get("args") or {}).get("command") or "")[:120]
        ok = result.get("ok")
        confirm = result.get("needs_confirm")
        lines.append(
            f"{i}. {tool} cmd={cmd!r} ok={ok} confirm={confirm}"
        )
    return "\n".join(lines) if lines else "(none)"


def _scrub_open_compounds(text: str) -> str:
    """Remove open-source / open-to phrases so bare 'open' heuristics stay quiet."""
    return _OPEN_COMPOUND_RE.sub(" ", text or "")


def _active_subtask_utterance(compound: Optional[dict[str, Any]]) -> str:
    if not compound:
        return ""
    subtasks: list[str] = compound.get("subtasks") or []
    if len(subtasks) <= 1:
        return str(compound.get("full_utterance") or subtasks[0] if subtasks else "")
    idx = int(compound.get("idx") or 0)
    if idx < len(subtasks):
        return subtasks[idx]
    return str(compound.get("full_utterance") or "")


def _history_since_last_subtask_boundary(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for i in range(len(history) - 1, -1, -1):
        call = (history[i].get("call") or {}).get("tool")
        if call == "_subtask_done":
            return history[i + 1 :]
    return history


def _decompose_compound_request(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return [text]
    open_parts = _extract_compound_open_parts(text)
    if open_parts and len(open_parts) >= 2:
        return open_parts
    if not re.search(r"(?i)\b(?:and|then|also)\b", text):
        return [text]
    parts = re.split(r"(?i)\s+(?:and|then|also)\s+", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 2:
        return [text]
    action_parts = [p for p in parts if _is_action_request(p) or _is_open_action(p)]
    if len(action_parts) >= 2:
        return action_parts
    return [text]


def _extract_compound_open_parts(text: str) -> Optional[list[str]]:
    if not _is_open_action(text):
        return None
    cleaned = _scrub_open_compounds(text).strip()
    m = re.search(
        r"(?i)^(?:please\s+|can\s+you\s+|could\s+you\s+)?open\s+(.+?)\s+and\s+(.+)$",
        cleaned,
    )
    if not m:
        return None
    left, right = m.group(1).strip().rstrip(".?!"), m.group(2).strip().rstrip(".?!")
    if not left or not right:
        return None
    return [f"open {left}", f"open {right}"]


def _normalize_folder_name(raw: str) -> str:
    name = (raw or "").strip().rstrip(".?!")
    name = re.sub(r"(?i)^(the|a|my)\s+", "", name).strip()
    name = re.sub(r"(?i)\b(folder|directory|dir)\b", "", name).strip()
    name = re.sub(r"(?i)^(named|called)\s+", "", name).strip()
    course = re.search(r"(?i)\b([a-z]{2,6}\d{3,4}[a-z]?)\b", name)
    if course:
        return course.group(1)
    digits = re.search(r"\b(\d{3,5})\b", name)
    if digits:
        return digits.group(1)
    return name


def _heuristic_open_target(target: str) -> Optional[dict[str, Any]]:
    t = (target or "").strip().rstrip(".?!")
    if not t:
        return None
    lower = t.lower()
    if lower in _KNOWN_SITES:
        return {"tool": "open_url", "args": {"url": _KNOWN_SITES[lower]}}
    for key, url in _KNOWN_SITES.items():
        if lower == key or lower.startswith(key + " "):
            return {"tool": "open_url", "args": {"url": url}}
    if re.search(r"(?i)\b(folder|directory|dir)\b", t) or re.search(
        r"(?i)\b(\d{3,5}|[a-z]{2,6}\d{3,4})\b", t
    ):
        folder = _normalize_folder_name(t)
        if folder:
            return {"tool": "open_folder", "args": {"query": folder}}
    if t.startswith("http") or re.search(r"(?i)\b(www\.|\.com|\.org|\.net)\b", t):
        return {"tool": "open_url", "args": {"url": t if t.startswith("http") else f"https://{t}"}}
    compact = t.replace(" ", "")
    if re.match(r"(?i)^[a-z]{2,6}\d{3,4}[a-z]?$", compact):
        return {"tool": "open_folder", "args": {"query": compact}}
    return {"tool": "open_app", "args": {"name": t}}


def _heuristic_compound_open(text: str) -> Optional[dict[str, Any]]:
    """First step of a compound open when subtask queue hasn't started yet."""
    parts = _extract_compound_open_parts(text)
    if not parts:
        return None
    return _heuristic_open_target(re.sub(r"(?i)^open\s+", "", parts[0]).strip())


def _is_open_action(utterance: str) -> bool:
    """True only when the user asks to open/launch/reveal something on the Mac."""
    text = (utterance or "").strip()
    if not text:
        return False
    cleaned = _scrub_open_compounds(text)
    return bool(_OPEN_RE.search(cleaned))


def _is_action_request(utterance: str) -> bool:
    """True when the user wants something done on the Mac, not just answered."""
    text = (utterance or "").strip()
    if not text:
        return False
    if _META_RE.search(text):
        return False
    cleaned = _scrub_open_compounds(text)
    # Pure questions with no action verbs stay Q&A.
    if re.match(
        r"(?i)^\s*(what|why|how\s+do\s+i\s+know|who|when|where|which|whose|"
        r"define|explain|tell\s+me\s+about|is\s+there|are\s+there)\b",
        cleaned,
    ) and not _ACTION_RE.search(cleaned) and not _is_open_action(text):
        return False
    if _is_open_action(text):
        return True
    if _ACTION_RE.search(cleaned):
        return True
    return False


def _bash_open_folder(name: str) -> str:
    """Fast folder lookup via Spotlight; limited find fallback (no full-home scan)."""
    import shlex

    q = shlex.quote((name or "").strip())
    return (
        f"name={q}; "
        'p=$(mdfind -onlyin "$HOME" "kind:folder $name" 2>/dev/null | head -1); '
        'if [ -z "$p" ]; then '
        'for d in "$HOME/Desktop" "$HOME/Documents" "$HOME/Downloads" "$HOME/Projects"; do '
        '  [ -d "$d" ] || continue; '
        '  p=$(find "$d" -maxdepth 8 -type d -iname "*$name*" 2>/dev/null | head -1); '
        '  [ -n "$p" ] && break; '
        "done; fi; "
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

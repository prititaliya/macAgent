"""Multi-step tool-calling agent loop (capped iterations for 1.5B / 8GB)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from events.bus import event_bus
from events.debug_trace import trace_step
from tools.pending_actions import (
    create_pending,
    clear_all as clear_pending_actions,
    update_pending_resume,
)
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


def _publish_answer_partial(
    utterance: str, text: str, *, backend: str = "local"
) -> None:
    """Stream accumulated answer text to the overlay via SSE (detail=partial)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return
    try:
        event_bus.publish(
            utterance=utterance or "",
            kind="answer",
            text=cleaned,
            detail="partial",
            backend=backend,
        )
    except Exception:  # noqa: BLE001
        pass

_ACTION_RE = re.compile(
    r"(?i)\b("
    r"shut\s*down|turn\s+off|power\s+off|restart|reboot|sleep|log\s*out|"
    r"empty\s+(the\s+)?(bin|trash)|delete|remove|install|quit|close|"
    # bare "open" is handled via _is_open_action (avoids open-source / open to …)
    r"click|press|type|launch|start|enable|disable|toggle|"
    r"set|change|move|copy|create|make|do\s+it|perform|run\s+this|"
    r"check\s+(what|which|my|the|if)|"
    r"compare|verify|inspect"
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
    r"ok|okay|cool|nice|great|awesome|lol|haha|bro+|dude|"
    r"good\s+(morning|afternoon|evening|night)|"
    r"how\s+are\s+(you|ya|u)|how('?s|\s+is)\s+it\s+going|"
    r"how('?s|\s+is)\s+going|hows\s+going|how\s+goes\s+it|"
    r"what'?s\s+up|wassup|whats\s+going\s+on|what'?s\s+going\s+on"
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
    r"shut\s*down|power\s+off|"
    r"turn\s+off\s+(my\s+)?(mac|pc|computer|machine|system)|"
    r"empty\s+(the\s+)?(bin|trash)|clear\s+(the\s+)?(bin|trash)|"
    r"\brm\b|\bdelete\b|\bremove\b|delete\s+all|wipe|"
    r"put\s+.+\s+to\s+sleep|sleep\s+(my\s+)?(mac|pc|computer)|go\s+to\s+sleep|"
    r"log\s*out|restart|reboot"
    r")\b"
)

_DISCOVERY_BASH_RE = re.compile(
    r"(?i)(^|[;&|]\s*|\n\s*)(ls|find|mdfind|locate|stat|du|head|tail|file|wc|dirname|basename)\b"
)

_DELETE_RE = re.compile(r"(?i)\b(delete|remove|rm|trash)\b")
_MOVE_RE = re.compile(r"(?i)\b(move|relocate|transfer)\b")
# "duplicate files" means find copies — not "duplicate/copy this file".
_COPY_RE = re.compile(
    r"(?i)\b(copy)\b|\bduplicate\s+(this|that|it|the\s+file|the\s+folder)\b"
)
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

# Open alone is not enough when the user also wants on-screen control / readout.
_GUI_FOLLOWUP_RE = re.compile(
    r"(?i)\b("
    r"type|click|press|keystroke|keystrokes|"
    r"enter\s+(this|that|the|it)|"
    r"on[\s-]?screen|read\s+(the\s+)?(screen|result|display|calculator)|"
    r"tell\s+me\s+(the\s+)?(result|answer|number)|"
    r"what('?s|\s+is)\s+(on\s+)?(the\s+)?(screen|display|result)|"
    r"ui_(snapshot|click|type|key|menu)"
    r")\b"
)

# Hybrid asks: research online AND inspect this Mac (must not stop at advice).
_LOCAL_MACHINE_CHECK_RE = re.compile(
    r"(?i)\b("
    r"(installed|running|present)\s+on\s+(my\s+)?(mac|computer|machine|laptop)|"
    r"on\s+(my\s+)?(mac|computer|machine|laptop)\b|"
    r"in\s+(the\s+)?terminal|"
    r"what\s+version\s+(do\s+i|is\s+installed|am\s+i\s+running)|"
    r"my\s+(installed|local|current)\s+(version|python|node|ruby|java)|"
    r"am\s+i\s+(out\s+of\s+date|up\s+to\s+date)|"
    r"check\s+(what|which|my|the).{0,40}(installed|version|running)|"
    r"how\s+much\s+(disk|space|storage|ram|memory)\s+(do\s+i|have)|"
    r"what('?s|\s+is)\s+on\s+(my\s+)?(mac|computer|machine)"
    r")\b"
)

_SHELL_ADVICE_RE = re.compile(
    r"(?i)("
    r"you\s+can\s+run|"
    r"run\s+(this|the\s+following|`)|"
    r"in\s+(the\s+)?terminal[,:]?\s*(run|type|execute)|"
    r"execute\s+`|"
    r"try\s+running"
    r")"
)

_CAPABILITIES_TEXT = """Here's what I can do on your Mac:

• Answer questions (local model + web search when needed)
• Open apps and Chrome URLs
• Turn Wi‑Fi / Bluetooth on or off, mute volume, switch dark/light mode
• Find files with shell (find/ls)
• Change system prefs (defaults) and power settings (pmset) without opening GUI
• List / kill top CPU & memory processes
• Send Notification Center alerts (even when the overlay is hidden)
• Empty Trash, shut down / restart / sleep — with your Approve first
• Control the UI (click, type, menus) when Accessibility is enabled
• Run short bash/Python for local tasks
• Remember notes you save in Preferences

Ask me like: “turn off wifi”, “open Slack”, “dark mode”, “mute”, or “top CPU processes”."""


_SKIP_RESPOND_RE = re.compile(
    r"(?i)("
    r"almost ran a system action|"
    r"won't change power|"
    r"don'?t need screen control|"
    r"won'?t run a destructive|"
    r"what would you like me to do"
    r")"
)


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

    def run(
        self,
        utterance: str,
        use_web: str = "auto",
        *,
        resume: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Execute tool loop; returns final payload for SSE / activity.

        Pass ``resume`` after Approve/Deny to continue the ChatML tool loop with
        prior history instead of starting a fresh ask.
        """
        if resume:
            history: list[dict[str, Any]] = list(resume.get("history") or [])
            sources: list[Any] = list(resume.get("sources") or [])
            last_text = str(resume.get("last_text") or "")
            mode = str(resume.get("use_web") or use_web)
            mode = mode if mode in ("auto", "on", "off") else "auto"
            compound_state = resume.get("compound_state") or {
                "subtasks": _decompose_compound_request(utterance),
                "idx": 0,
                "results": [],
                "full_utterance": utterance,
            }
            self._use_web = mode
            self._compound_state = compound_state
            event_bus.publish(
                utterance=utterance,
                kind="action",
                text="Continuing after approval…" if history else "Planning…",
                detail="pending",
            )
        else:
            history = []
            sources = []
            last_text = ""
            mode = use_web if use_web in ("auto", "on", "off") else "auto"
            self._use_web = mode
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
                tool_input={"utterance": utterance, "use_web": mode},
            )

            # Knowledge / coding Q&A: skip the heavy tool-loop planner.
            # Cloud → structured envelope; local-only → generate_answer with a larger token budget.
            if self._should_direct_answer(utterance, mode):
                if self.parser._cloud.should_use_cloud(utterance):
                    event_bus.publish(
                        utterance=utterance,
                        kind="action",
                        text="Asking cloud…",
                        detail="pending",
                    )
                    narrate("thinking")
                    try:
                        from memory.user_context import (
                            clear_cloud_handoff,
                            set_cloud_handoff,
                        )

                        env = self.parser.generate_cloud_envelope(utterance)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("cloud envelope failed: %s", exc)
                        env = {
                            "final": True,
                            "answer": "",
                            "guidance": "",
                            "commands": [],
                        }

                    if env.get("final"):
                        reply = (env.get("answer") or "").strip()
                        if not reply:
                            reply = self._local_answer(utterance)
                        return self._finish(utterance, reply, sources, history)

                    # Not final — cloud wants MacAgent to act locally.
                    set_cloud_handoff(env)
                    event_bus.publish(
                        utterance=utterance,
                        kind="action",
                        text="Acting on cloud plan…",
                        detail="pending",
                    )
                    narrate("acting")
                    guidance = str(env.get("guidance") or "").strip()
                    commands = [
                        str(c).strip()
                        for c in (env.get("commands") or [])
                        if str(c).strip()
                    ][:5]

                    for cmd in commands:
                        if _command_is_destructive(
                            cmd
                        ) and not _DESTRUCTIVE_UTTERANCE_RE.search(utterance or ""):
                            clear_cloud_handoff()
                            return self._finish(
                                utterance,
                                "Cloud suggested a destructive command I won't run "
                                "unless you explicitly ask (e.g. delete / empty trash). "
                                f"Guidance was: {guidance or cmd}",
                                sources,
                                history,
                            )
                        call = {"tool": "run_bash", "args": {"command": cmd}}
                        event_bus.publish(
                            utterance=utterance,
                            kind="trace",
                            text="Calling run_bash",
                            detail="tool_call",
                            step="tool_call",
                            tool="run_bash",
                            tool_input={"command": cmd},
                        )
                        event_bus.publish(
                            utterance=utterance,
                            kind="action",
                            text="Tool: run_bash",
                            detail="pending",
                        )
                        result = self.registry.run("run_bash", {"command": cmd})
                        history.append({"call": call, "result": result})
                        trace_step(
                            "agent_tool_result",
                            step=len(history) - 1,
                            tool="run_bash",
                            result=_trim(result),
                            cloud_handoff=True,
                        )
                        event_bus.publish(
                            utterance=utterance,
                            kind="trace",
                            text="run_bash → output",
                            detail="tool_result",
                            step="tool_result",
                            tool="run_bash",
                            tool_input={"command": cmd},
                            tool_output=_trim(result),
                        )
                        if result.get("needs_confirm"):
                            return self._request_confirm(
                                utterance,
                                command=str(result.get("command") or cmd),
                                summary=str(
                                    result.get("summary") or guidance or cmd
                                ),
                                sources=sources,
                                history=history,
                            )

                    # If commands produced useful stdout, answer from that and stop.
                    bash_outs: list[tuple[str, str]] = []
                    for item in history:
                        call = item.get("call") or {}
                        result = item.get("result") or {}
                        if call.get("tool") != "run_bash":
                            continue
                        out = (result.get("stdout") or "").strip()
                        if result.get("ok") and out:
                            bash_outs.append(
                                (
                                    str(
                                        (call.get("args") or {}).get("command") or ""
                                    ),
                                    out,
                                )
                            )
                    if bash_outs:
                        cmd, out = bash_outs[-1]
                        text = self._format_command_answer(utterance, cmd, out)
                        if guidance and text and len(out) < 80:
                            text = f"{text}\n\n({guidance[:240]})"
                        clear_cloud_handoff()
                        return self._finish(
                            utterance, text or out, sources, history
                        )

                    # No usable command output — continue into the local planner.
                    event_bus.publish(
                        utterance=utterance,
                        kind="action",
                        text="Planning with cloud guidance…",
                        detail="pending",
                    )
                    narrate("planning")
                else:
                    # Local-only knowledge path — no planner/catalog burn.
                    event_bus.publish(
                        utterance=utterance,
                        kind="action",
                        text="Thinking…",
                        detail="pending",
                    )
                    narrate("thinking")
                    reply = self._local_answer(utterance)
                    return self._finish(utterance, reply, sources, history)
            else:
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

            # Special: generate bash then run it (cloud when configured; scrubbed ask).
            if tool == "__write_and_run_bash__":
                event_bus.publish(
                    utterance=utterance,
                    kind="action",
                    text=(
                        "Asking cloud for shell…"
                        if self.parser._cloud.cloud_ready()
                        else "Writing & running shell…"
                    ),
                    detail="pending",
                    step="shellgen",
                )
                if self.parser._cloud.cloud_ready():
                    narrate("thinking")
                # Prefer an explicit next_hint / prior plan over regenerating.
                hinted = _command_from_goal_hints(history)
                command = hinted or self.parser.generate_bash(utterance)
                if not (command or "").strip():
                    return self._finish(
                        utterance,
                        "I couldn't build a safe shell command for that. "
                        "Try naming the exact folder path (e.g. ~/Documents/…).",
                        sources,
                        history,
                    )
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
                # Refusals / clarifying responds are terminal — don't loop the goal critic.
                force_done = bool(args.get("goal_done")) or bool(
                    _SKIP_RESPOND_RE.search(last_text)
                )
                finished = self._attempt_finish(
                    utterance, last_text, sources, history, force=force_done
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
                    # Hybrid version check: local already ran → compare and finish.
                    hybrid = self._try_hybrid_version_finish(
                        utterance, sources, history
                    )
                    if hybrid is not None:
                        return hybrid
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
                scraped = bool(
                    re.search(r"(?i)Page content from\s+https?://", combined)
                )
                reply = self.parser.answer_from_search(
                    utterance,
                    combined,
                    force_cloud=True if scraped else None,
                    on_token=lambda t: _publish_answer_partial(
                        utterance,
                        t,
                        backend=(
                            "cloud"
                            if (
                                scraped and self.parser._cloud.cloud_ready()
                            )
                            or self.parser._cloud.should_use_cloud(utterance)
                            else "local"
                        ),
                    ),
                )
                searches_done = _web_search_count(history)
                if _answer_needs_more_search(reply):
                    # Prefer scraping the next unread page over a whole new search.
                    unread, pages_read, last_q = _last_search_unread(history)
                    if unread and pages_read < 3:
                        event_bus.publish(
                            utterance=utterance,
                            kind="action",
                            text=f"Reading another source ({pages_read + 1}/3)…",
                            detail="pending",
                        )
                        narrate("researching")
                        call = {
                            "tool": "web_search",
                            "args": {
                                "query": last_q or utterance,
                                "unread_urls": unread,
                                "pages_already_read": pages_read,
                            },
                        }
                        event_bus.publish(
                            utterance=utterance,
                            kind="trace",
                            text="Prior page insufficient — scraping next URL",
                            detail="page_retry",
                            step="page_retry",
                            tool_output={
                                "next_url": unread[0],
                                "pages_read": pages_read,
                            },
                        )
                        more = self.registry.run("web_search", call["args"])
                        history.append({"call": call, "result": more})
                        trace_step(
                            "agent_tool_result",
                            step=step,
                            tool="web_search",
                            result=_trim(more),
                            page_retry=True,
                        )
                        if isinstance(more.get("sources"), list):
                            for s in more["sources"]:
                                if s not in sources:
                                    sources.append(s)
                        continue
                    if searches_done < _MAX_WEB_SEARCHES:
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
                            text=(
                                f"Need better sources — searching again "
                                f"({searches_done}/{_MAX_WEB_SEARCHES})…"
                            ),
                            detail="pending",
                        )
                        narrate("researching")
                        event_bus.publish(
                            utterance=utterance,
                            kind="trace",
                            text="Search answer insufficient — retrying",
                            detail="search_retry",
                            step="search_retry",
                            tool_output={
                                "reason": "insufficient",
                                "attempt": searches_done,
                            },
                        )
                        continue
                finished = self._attempt_finish(utterance, reply, sources, history)
                if finished is not None:
                    return finished
                continue

            if tool == "run_python":
                if result.get("ok") and (result.get("stdout") or "").strip():
                    out = str(result["stdout"]).strip()
                    # Bare numeric stdout is fine for pure calc asks; for explain/Q&A
                    # turn it into a real answer (don't stop at "0.5").
                    if (
                        len(out) < 80
                        and "\n" not in out
                        and not _is_info_question(utterance)
                        and not re.search(
                            r"(?i)\b(explain|define|describe|what (are|is|does)|mean)\b",
                            utterance or "",
                        )
                    ):
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
                # Soft-ok: useful stdout despite later `&&` failure
                # (e.g. python3 --version worked; bare `python` → 127).
                bash_ok = bool(result.get("ok")) or (
                    bool(out)
                    and not _bash_stdout_is_useless(out)
                    and bool(
                        re.search(
                            r"(?i)command not found|not found|"
                            r"No such file|permission denied",
                            err,
                        )
                        or re.search(
                            r"(?i)^(Python|Node|v?\d|ruby|java|go |ProductName|"
                            r"System Version)",
                            out,
                            re.M,
                        )
                    )
                )
                if bash_ok and out:
                    # Useless curl/error stdout is not a completed local check.
                    if _bash_stdout_is_useless(out) and _needs_local_machine_check(
                        utterance
                    ):
                        local_cmd = _preferred_local_check_command(utterance)
                        if local_cmd and local_cmd not in cmd:
                            event_bus.publish(
                                utterance=utterance,
                                kind="action",
                                text="Checking this Mac…",
                                detail="pending",
                            )
                            call = {
                                "tool": "run_bash",
                                "args": {"command": local_cmd},
                            }
                            result = self.registry.run(
                                "run_bash", {"command": local_cmd}
                            )
                            history.append({"call": call, "result": result})
                            trace_step(
                                "agent_tool_result",
                                step=step,
                                tool="run_bash",
                                result=_trim(result),
                                repaired=True,
                            )
                            out2 = (result.get("stdout") or "").strip()
                            if result.get("ok") and out2 and not _bash_stdout_is_useless(
                                out2
                            ):
                                text = self._format_command_answer(
                                    utterance, local_cmd, out2
                                )
                                finished = self._attempt_finish(
                                    active, text, sources, history
                                )
                                if finished is not None:
                                    return finished
                            continue
                    text = self._format_command_answer(utterance, cmd, out)
                    # Never finish with "please run this yourself" on a Mac check ask.
                    if _candidate_is_shell_advice(text) and _needs_local_machine_check(
                        utterance
                    ):
                        local_cmd = _preferred_local_check_command(utterance)
                        if local_cmd and not _history_has_command_containing(
                            history, local_cmd
                        ):
                            event_bus.publish(
                                utterance=utterance,
                                kind="action",
                                text="Running local check…",
                                detail="pending",
                            )
                            call = {
                                "tool": "run_bash",
                                "args": {"command": local_cmd},
                            }
                            result = self.registry.run(
                                "run_bash", {"command": local_cmd}
                            )
                            history.append({"call": call, "result": result})
                            trace_step(
                                "agent_tool_result",
                                step=step,
                                tool="run_bash",
                                result=_trim(result),
                                repaired=True,
                            )
                            out2 = (result.get("stdout") or "").strip()
                            if result.get("ok") and out2 and not _bash_stdout_is_useless(
                                out2
                            ):
                                text = self._format_command_answer(
                                    utterance, local_cmd, out2
                                )
                                finished = self._attempt_finish(
                                    active, text, sources, history
                                )
                                if finished is not None:
                                    return finished
                            continue
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
                    # Hybrid "latest online + what's on my Mac" → compare once both exist.
                    hybrid = self._try_hybrid_version_finish(
                        active, sources, history
                    )
                    if hybrid is not None:
                        return hybrid
                    finished = self._attempt_finish(
                        active, text, sources, history
                    )
                    if finished is not None:
                        return finished
                    continue
                # Empty stdout + stderr (e.g. GNU -printf on macOS) is a failure, not "Done".
                if err and not out:
                    # Broken quoting / shell syntax → prefer a known-good rewrite
                    # before asking cloud (cloud often returns empty).
                    if re.search(
                        r"(?i)syntax error|unexpected token|unmatched|parse error",
                        err,
                    ) and not any(
                        (h.get("result") or {}).get("cloud_bash_retry") for h in history
                    ):
                        local_fix = _repair_bash_command(cmd, utterance)
                        try:
                            from llm.inference import (
                                _deterministic_zip_command,
                                _deterministic_create_command,
                            )

                            if not local_fix:
                                local_fix = _deterministic_zip_command(
                                    utterance
                                ) or _deterministic_create_command(utterance)
                        except Exception:  # noqa: BLE001
                            pass
                        if local_fix and local_fix != cmd:
                            event_bus.publish(
                                utterance=utterance,
                                kind="action",
                                text="Retrying with a safer shell command…",
                                detail="pending",
                            )
                            call = {
                                "tool": "run_bash",
                                "args": {"command": local_fix},
                            }
                            result = self.registry.run(
                                "run_bash", {"command": local_fix}
                            )
                            result = dict(result)
                            result["cloud_bash_retry"] = True
                            history.append({"call": call, "result": result})
                            trace_step(
                                "agent_tool_result",
                                step=step,
                                tool="run_bash",
                                result=_trim(result),
                                local_bash_retry=True,
                            )
                            out2 = (result.get("stdout") or "").strip()
                            err2 = (
                                result.get("error") or result.get("stderr") or ""
                            ).strip()
                            if result.get("ok"):
                                text = (
                                    self._format_command_answer(
                                        utterance, local_fix, out2
                                    )
                                    if out2
                                    else (
                                        f"Done (`{local_fix}`)."
                                        if local_fix
                                        else "Done."
                                    )
                                )
                                finished = self._attempt_finish(
                                    active, text, sources, history
                                )
                                if finished is not None:
                                    return finished
                                continue
                            err = err2 or err
                            cmd = local_fix
                        else:
                            event_bus.publish(
                                utterance=utterance,
                                kind="action",
                                text="Asking cloud for a safer shell command…",
                                detail="pending",
                            )
                            narrate("thinking")
                            rewritten = self.parser.generate_bash(utterance)
                            if rewritten and rewritten != cmd:
                                call = {
                                    "tool": "run_bash",
                                    "args": {"command": rewritten},
                                }
                                result = self.registry.run(
                                    "run_bash", {"command": rewritten}
                                )
                                result = dict(result)
                                result["cloud_bash_retry"] = True
                                history.append({"call": call, "result": result})
                                trace_step(
                                    "agent_tool_result",
                                    step=step,
                                    tool="run_bash",
                                    result=_trim(result),
                                    cloud_bash_retry=True,
                                )
                                out2 = (result.get("stdout") or "").strip()
                                err2 = (
                                    result.get("error")
                                    or result.get("stderr")
                                    or ""
                                ).strip()
                                if result.get("ok") and out2:
                                    text = self._format_command_answer(
                                        utterance, rewritten, out2
                                    )
                                    finished = self._attempt_finish(
                                        active, text, sources, history
                                    )
                                    if finished is not None:
                                        return finished
                                    continue
                                if result.get("ok"):
                                    text = (
                                        "Done."
                                        if not rewritten
                                        else f"Done (`{rewritten}`)."
                                    )
                                    finished = self._attempt_finish(
                                        active, text, sources, history
                                    )
                                    if finished is not None:
                                        return finished
                                    continue
                                err = err2 or err
                                cmd = rewritten
                    repaired = _repair_bash_command(cmd, utterance)
                    already = any(
                        str(((h.get("call") or {}).get("args") or {}).get("command") or "")
                        == repaired
                        for h in history
                    )
                    if repaired and repaired != cmd and not already:
                        event_bus.publish(
                            utterance=utterance,
                            kind="action",
                            text="Retrying shell…",
                            detail="pending",
                        )
                        call = {"tool": "run_bash", "args": {"command": repaired}}
                        result = self.registry.run("run_bash", {"command": repaired})
                        history.append({"call": call, "result": result})
                        trace_step(
                            "agent_tool_result",
                            step=step,
                            tool="run_bash",
                            result=_trim(result),
                            repaired=True,
                        )
                        out2 = (result.get("stdout") or "").strip()
                        err2 = (
                            result.get("error") or result.get("stderr") or ""
                        ).strip()
                        if result.get("ok") and out2:
                            text = self._format_command_answer(
                                utterance, repaired, out2
                            )
                            finished = self._attempt_finish(
                                active, text, sources, history
                            )
                            if finished is not None:
                                return finished
                            continue
                        finished = self._attempt_finish(
                            active,
                            f"Shell error: {_scrub_answer_paths(err2 or err)}",
                            sources,
                            history,
                            force=True,
                        )
                        if finished is not None:
                            return finished
                        continue
                    finished = self._attempt_finish(
                        active,
                        f"Shell error: {_scrub_answer_paths(err)}",
                        sources,
                        history,
                        force=True,
                    )
                    if finished is not None:
                        return finished
                    continue
                if result.get("ok") and not out:
                    # Side-effect cmds (mkdir/touch) may have empty stdout; discovery
                    # must never report "Done (`find…`)" with no results.
                    is_discovery = bool(
                        re.search(
                            r"(?i)\b(find|du|ls|stat|mdfind|locate|wc|head|tail|"
                            r"cat|grep|awk|sort)\b",
                            cmd,
                        )
                    )
                    if is_discovery:
                        repaired = _repair_bash_command(cmd, utterance)
                        already = any(
                            str(
                                ((h.get("call") or {}).get("args") or {}).get(
                                    "command"
                                )
                                or ""
                            )
                            == repaired
                            for h in history
                        )
                        if repaired and repaired != cmd and not already:
                            event_bus.publish(
                                utterance=utterance,
                                kind="action",
                                text="Retrying shell…",
                                detail="pending",
                            )
                            call = {
                                "tool": "run_bash",
                                "args": {"command": repaired},
                            }
                            result = self.registry.run(
                                "run_bash", {"command": repaired}
                            )
                            history.append({"call": call, "result": result})
                            trace_step(
                                "agent_tool_result",
                                step=step,
                                tool="run_bash",
                                result=_trim(result),
                                repaired=True,
                            )
                            out2 = (result.get("stdout") or "").strip()
                            if result.get("ok") and out2:
                                text = self._format_command_answer(
                                    utterance, repaired, out2
                                )
                                finished = self._attempt_finish(
                                    active, text, sources, history
                                )
                                if finished is not None:
                                    return finished
                                continue
                        finished = self._attempt_finish(
                            active,
                            "That scan returned no files. "
                            "Try a specific folder (e.g. Downloads or Documents).",
                            sources,
                            history,
                            force=True,
                        )
                        if finished is not None:
                            return finished
                        continue
                    text = "Done." if not cmd else f"Done (`{cmd}`)."
                    finished = self._attempt_finish(
                        active, text, sources, history
                    )
                    if finished is not None:
                        return finished
                    continue
                # Failed discovery: try a known-good rewrite before giving up.
                if not result.get("ok") and not out:
                    repaired = _repair_bash_command(cmd, utterance)
                    already = any(
                        str(
                            ((h.get("call") or {}).get("args") or {}).get("command")
                            or ""
                        )
                        == repaired
                        for h in history
                    )
                    if repaired and repaired != cmd and not already:
                        event_bus.publish(
                            utterance=utterance,
                            kind="action",
                            text="Retrying shell…",
                            detail="pending",
                        )
                        call = {
                            "tool": "run_bash",
                            "args": {"command": repaired},
                        }
                        result = self.registry.run(
                            "run_bash", {"command": repaired}
                        )
                        history.append({"call": call, "result": result})
                        trace_step(
                            "agent_tool_result",
                            step=step,
                            tool="run_bash",
                            result=_trim(result),
                            repaired=True,
                        )
                        out2 = (result.get("stdout") or "").strip()
                        err2 = (
                            result.get("error") or result.get("stderr") or ""
                        ).strip()
                        if result.get("ok") and out2:
                            text = self._format_command_answer(
                                utterance, repaired, out2
                            )
                            finished = self._attempt_finish(
                                active, text, sources, history
                            )
                            if finished is not None:
                                return finished
                            continue
                        finished = self._attempt_finish(
                            active,
                            f"Shell error: {_scrub_answer_paths(err2 or err or 'no output')}",
                            sources,
                            history,
                            force=True,
                        )
                        if finished is not None:
                            return finished
                        continue
                finished = self._attempt_finish(
                    active,
                    f"Shell error: {_scrub_answer_paths(err or 'command failed')}",
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
                "control_mac",
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
                elif tool == "control_mac":
                    msg = result.get("message") or f"Updated {result.get('feature')}."
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

            if tool == "control_mac" and not result.get("ok"):
                err = str(
                    result.get("error")
                    or result.get("message")
                    or "Mac control failed"
                )
                return self._finish(utterance, err, sources, history)

            # UI tools: soft failures retry once via planner; hard permission errors stop.
            if tool in {"ui_snapshot", "ui_click", "ui_type", "ui_key", "ui_menu"}:
                if not result.get("ok"):
                    err = _friendly_ui_error(result.get("error") or "UI action failed")
                    hard = bool(
                        re.search(
                            r"(?i)accessibility|bridge is offline|not allowed|assistive",
                            err,
                        )
                    )
                    ui_fails = sum(
                        1
                        for h in history
                        if (h.get("call") or {}).get("tool")
                        in {
                            "ui_snapshot",
                            "ui_click",
                            "ui_type",
                            "ui_key",
                            "ui_menu",
                        }
                        and not (h.get("result") or {}).get("ok")
                    )
                    if hard or ui_fails >= 2:
                        return self._finish(utterance, err, sources, history)
                    # Transient bridge / focus glitch — keep going (e.g. retry type, or click).
                    event_bus.publish(
                        utterance=utterance,
                        kind="action",
                        text="Screen control hiccup — retrying…",
                        detail="pending",
                    )
                    continue
                # Successful UI step — continue toward respond / more clicks.
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
        # Only light safety shortcuts — Mac actions go through the planner (Qwen3).
        use_web = getattr(self, "_use_web", "auto") or "auto"
        scoped = _history_since_last_subtask_boundary(history)
        if not scoped:
            quick = _safety_heuristic_tool(utterance)
            if quick:
                return _apply_use_web(utterance, quick, history=history, use_web=use_web)
            # Don't let the local model invent world facts when cloud is off.
            # When cloud can answer, skip forced web_search (cloud fast-path handles it).
            text = (utterance or "").strip()
            force_web = (
                use_web == "on"
                and text
                and not _CHAT_RE.match(text)
                and not _META_RE.search(text)
                and not _ABOUT_ME_RE.search(text)
            )
            cloud_ok = False
            try:
                cloud_ok = self.parser._cloud.should_use_cloud(utterance)
            except Exception:  # noqa: BLE001
                cloud_ok = False
            if use_web != "off" and (force_web or _is_factual_lookup(utterance)):
                if cloud_ok and use_web != "on":
                    return {
                        "tool": "respond",
                        "args": {"text": self._local_answer(utterance)},
                    }
                return {"tool": "web_search", "args": {"query": utterance}}

        # After discovery, force the mutating follow-up when the goal is clear.
        follow = _forced_followup(utterance, scoped or history)
        if follow:
            return _apply_use_web(
                utterance, follow, history=scoped or history, use_web=use_web
            )

        raw = self.parser.plan_tool_call(utterance, history, TOOL_CATALOG)
        parsed = _parse_tool_call(raw)
        if parsed:
            return _sanitize_planned_call(
                utterance,
                parsed,
                history=scoped or history,
                use_web=use_web,
            )
        # Parse failed. Mac actions → write bash; never invent a web how-to.
        if _is_action_request(utterance):
            return _apply_use_web(
                utterance,
                {"tool": "__write_and_run_bash__", "args": {}},
                history=scoped or history,
                use_web=use_web,
            )
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
        if use_web == "off":
            return {
                "tool": "respond",
                "args": {"text": self._local_answer(utterance)},
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
                        utterance,
                        str(result["context"]),
                        on_token=lambda t: _publish_answer_partial(
                            utterance,
                            t,
                            backend=(
                                "cloud"
                                if self.parser._cloud.should_use_cloud(utterance)
                                else "local"
                            ),
                        ),
                    )
                except Exception:  # noqa: BLE001
                    pass
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
            if tool == "control_mac" and result.get("ok"):
                return str(result.get("message") or "Done.")
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

    def _should_direct_answer(self, utterance: str, use_web: str) -> bool:
        """True for knowledge/coding Q&A — skip the Mac tool-loop planner."""
        mode = use_web if use_web in ("auto", "on", "off") else "auto"
        # User forced web search — keep the grounded search path.
        if mode == "on":
            return False
        # Follow-ups that act on a prior Mac result must stay in the tool loop.
        try:
            from memory.user_context import get_prior_turns

            if get_prior_turns() and _followup_needs_local_tools(utterance):
                return False
        except Exception:  # noqa: BLE001
            pass
        text = (utterance or "").strip()
        if not text:
            return False
        # Live Mac / filesystem / UI asks stay on the tool loop.
        try:
            from llm.cloud import is_local_system_task

            if is_local_system_task(text):
                return False
        except Exception:  # noqa: BLE001
            pass
        # Profile notes use a dedicated path in the tool loop.
        if _ABOUT_ME_RE.search(text):
            return False
        # Capabilities / greetings still use direct answer (no planner burn).
        if _META_RE.search(text) or _CHAT_RE.match(text):
            return True
        # Opening apps / Mac UI controls stay on the local tool loop.
        # Do NOT use _is_action_request here — "make/create/type" also match coding asks.
        if _is_open_action(text):
            return False
        if _control_mac_heuristic(text) or _close_app_heuristic(text):
            return False
        safety = _safety_heuristic_tool(text)
        if safety and safety.get("tool") not in {
            None,
            "respond",
            "__answer_about_user__",
        }:
            return False
        return True

    def _should_answer_via_cloud(self, utterance: str, use_web: str) -> bool:
        """True when this ask should skip the local tool loop and hit the cloud LLM."""
        if not self._should_direct_answer(utterance, use_web):
            return False
        text = (utterance or "").strip()
        # Greetings / capabilities / profile stay on-device even when cloud is enabled.
        if _ABOUT_ME_RE.search(text) or _META_RE.search(text) or _CHAT_RE.match(text):
            return False
        return bool(self.parser._cloud.should_use_cloud(utterance))

    def _local_answer(self, utterance: str) -> str:
        try:
            # Assume local until generate_answer flips to cloud on success.
            self.parser.last_answer_backend = "local"

            def _on_token(t: str) -> None:
                backend = getattr(self.parser, "last_answer_backend", "local") or "local"
                # While streaming we may not know yet — prefer cloud if routing would allow.
                if self.parser._cloud.should_use_cloud(utterance):
                    backend = "cloud"
                _publish_answer_partial(utterance, t, backend=backend)

            return self.parser.generate_answer(utterance, on_token=_on_token)
        except Exception:  # noqa: BLE001
            self.parser.last_answer_backend = "local"
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

    def _try_hybrid_version_finish(
        self,
        utterance: str,
        sources: list[Any],
        history: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Finish 'latest online + what's installed' once both tool results exist."""
        if not (
            _needs_local_machine_check(utterance)
            and _utterance_wants_web_search(utterance)
        ):
            return None
        if not _history_has_successful_bash(history):
            return None
        if _web_search_count(history) == 0:
            return None
        local_out = _latest_useful_bash_stdout(history)
        web_ctx = _combined_search_context(history)
        if not local_out or not web_ctx:
            return None
        event_bus.publish(
            utterance=utterance,
            kind="action",
            text="Comparing with latest…",
            detail="pending",
        )
        # Prefer deterministic compare — small local models botch "3.12 < 3.14"
        # and invent "run this yourself" advice.
        reply = _deterministic_hybrid_version_answer(utterance, local_out, web_ctx)
        if not reply:
            narrate("thinking")
            try:
                reply = self.parser.answer_from_search(
                    utterance,
                    (
                        f"{web_ctx}\n\n"
                        f"=== Installed on this Mac (already checked) ===\n"
                        f"{local_out}\n\n"
                        "Rules: Do NOT tell the user to run a terminal command — "
                        "MacAgent already checked this Mac. State the latest version "
                        "from the web, the installed version from the Mac output, "
                        "and whether the Mac is out of date. Be consistent: if "
                        "installed < latest, say out of date."
                    ),
                    on_token=lambda t: _publish_answer_partial(
                        utterance,
                        t,
                        backend=(
                            "cloud"
                            if self.parser._cloud.should_use_cloud(utterance)
                            else "local"
                        ),
                    ),
                )
            except Exception:  # noqa: BLE001
                reply = ""
            if _candidate_is_shell_advice(reply or "") or not (reply or "").strip():
                reply = (
                    f"On this Mac: {local_out.strip()}\n\n"
                    "I looked up the latest release online (see sources). "
                    "I couldn't reliably parse both versions to compare."
                )
        return self._attempt_finish(
            utterance, reply.strip(), sources, history, force=True
        )

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
        history.append(
            {
                "call": {"tool": "run_bash", "args": {"command": cmd}},
                "result": {"ok": False, "needs_confirm": True},
            }
        )
        # Snapshot loop state so Approve/Deny can resume the ChatML tool cycle.
        resume = {
            "history": list(history),
            "sources": list(sources),
            "use_web": getattr(self, "_use_web", "auto"),
            "compound_state": getattr(self, "_compound_state", None),
            "last_text": "",
        }
        pending = create_pending(
            utterance=utterance,
            summary=summary,
            command=cmd,
            tool="run_bash",
            resume=resume,
        )
        # Stamp the pending id onto the placeholder for debugging.
        history[-1]["result"]["id"] = pending["id"]
        resume["history"] = list(history)
        update_pending_resume(pending["id"], resume)

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
        return {
            "action": "confirm",
            "pending_id": pending["id"],
            "summary": summary,
            "command": cmd,
            "answer": summary,
            "sources": sources,
            "steps": len(history),
        }

    def continue_after_confirm(
        self,
        *,
        utterance: str,
        command: str,
        approved: bool,
        tool_result: dict[str, Any],
        resume: dict[str, Any],
    ) -> dict[str, Any]:
        """Resume the agent loop after Approve/Deny (append tool result → back to Qwen)."""
        history = list(resume.get("history") or [])
        sources = list(resume.get("sources") or [])
        entry = {
            "call": {"tool": "run_bash", "args": {"command": command}},
            "result": tool_result,
        }
        if history and (history[-1].get("result") or {}).get("needs_confirm"):
            history[-1] = entry
        else:
            history.append(entry)

        if not approved:
            return self._finish(
                utterance, "Cancelled — nothing was changed.", sources, history
            )

        last_text = ""
        if tool_result.get("ok"):
            last_text = (tool_result.get("stdout") or "").strip() or "Done."
            cmd_l = (command or "").lower()
            if "empty the trash" in cmd_l:
                last_text = "Trash emptied."
            elif "shut down" in cmd_l:
                last_text = "Shutting down…"
            elif "restart" in cmd_l:
                last_text = "Restarting…"
            elif re.search(r"\bsleep\b", cmd_l):
                last_text = "Sleeping…"
        else:
            last_text = str(
                tool_result.get("error") or tool_result.get("stderr") or "command failed"
            )

        # Feed tool stdout/stderr back into ChatML and let Qwen decide the final reply.
        return self.run(
            utterance,
            use_web=str(resume.get("use_web") or "auto"),
            resume={
                "history": history,
                "sources": sources,
                "use_web": resume.get("use_web") or "auto",
                "compound_state": resume.get("compound_state"),
                "last_text": last_text,
            },
        )

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
        try:
            from memory.user_context import clear_cloud_handoff

            clear_cloud_handoff()
        except Exception:  # noqa: BLE001
            pass
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

        # Tool-executed Mac actions are always local — never attribute to cloud.
        used_tools = [
            (h.get("call") or {}).get("tool")
            for h in history
            if (h.get("call") or {}).get("tool") not in {None, "respond", ""}
        ]
        if used_tools:
            backend = "local"
        else:
            backend = getattr(self.parser, "last_answer_backend", "local") or "local"

        event_bus.publish(
            utterance=utterance,
            kind="answer",
            text=_scrub_answer_paths(text),
            detail="agent",
            sources=normalized or None,
            backend=backend,
        )
        try:
            narrate_answer(_scrub_answer_paths(text or ""))
        except Exception as exc:  # noqa: BLE001
            logger.debug("tts answer skipped: %s", exc)
        trace_step(
            "final",
            kind="answer",
            detail="agent",
            text=_scrub_answer_paths(text),
            backend=backend,
            tools=[h.get("call", {}).get("tool") for h in history],
        )
        return {
            "action": "answer",
            "answer": _scrub_answer_paths(text),
            "sources": normalized,
            "steps": len(history),
            "backend": backend,
        }


def _scrub_answer_paths(text: str) -> str:
    """Never show /Users/<name>/… in overlay answers — use ~."""
    try:
        from tools.run_bash import scrub_home_paths_for_display

        return scrub_home_paths_for_display(text or "")
    except Exception:  # noqa: BLE001
        t = text or ""
        return re.sub(r"/Users/[^/\s\"']+", "~", t)


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



# Scoped home scan — never walk / or /Volumes (often empty/hangs).
_BIGGEST_FILE_BASH = (
    'find "$HOME" -type f -maxdepth 5 '
    "! -path '*/Library/Caches/*' ! -path '*/.Trash/*' "
    "-exec du -k {} + 2>/dev/null | sort -nr | head -n 5 | "
    "awk '{printf \"%.1f MB\\t\", $1/1024; $1=\"\"; sub(/^ /,\"\"); print}'"
)

_WANTS_BIGGEST_FILE_RE = re.compile(
    r"(?i)("
    r"\b(biggest|largest)\b.{0,40}\bfiles?\b|"
    r"\bfiles?\b.{0,40}\b(biggest|largest)\b|"
    r"what'?s\s+the\s+(biggest|largest)\s+file"
    r")"
)


def _followup_needs_local_tools(utterance: str) -> bool:
    """True when a follow-up likely acts on a prior Mac result (open/delete/…)."""
    text = (utterance or "").strip()
    if not text:
        return False
    if _is_open_action(text):
        return True
    if _control_mac_heuristic(text) or _close_app_heuristic(text):
        return True
    if re.search(
        r"(?i)\b("
        r"open|launch|delete|remove|trash|move|copy|kill|quit|close|"
        r"run\s+it|show\s+(me\s+)?(the\s+)?(file|path|folder)|"
        r"where\s+is\s+(it|that|the\s+file)"
        r")\b",
        text,
    ):
        return True
    return False


def _path_from_prior_assistant() -> Optional[str]:
    """Best absolute/~ path from the latest follow-up Assistant reply."""
    try:
        from memory.user_context import get_prior_turns
    except Exception:  # noqa: BLE001
        return None
    turns = get_prior_turns()
    if not turns:
        return None
    text = str(turns[-1].get("assistant") or "")
    paths = re.findall(
        r"(?:/Users/[^\s`\"'<>]+|~/[^\s`\"'<>]+)",
        text,
    )
    if not paths:
        return None
    # Prefer a real file path over a directory listing noise.
    for p in reversed(paths):
        cleaned = p.rstrip(").,;:]")
        if cleaned:
            return cleaned
    return None


def _repair_bash_command(command: str, utterance: str = "") -> Optional[str]:
    """Rewrite known-bad Linux-only flags into macOS-safe equivalents.

    Does not replace whole commands with canned Downloads/latest templates —
    the model owns the plan; we only fix portability nits.
    """
    cmd = (command or "").strip()
    blob = f"{cmd}\n{utterance or ''}"

    # quote_paths_with_spaces bug remnant: find "~/Downloads -type …"
    if re.search(r'(?i)\bfind\s+"~/', cmd) and re.search(
        r'(?i)"~/\w[\w /.-]*\s+-[a-zA-Z]', cmd
    ):
        try:
            from llm.inference import _deterministic_zip_command

            zipped = _deterministic_zip_command(utterance or "")
            if zipped:
                return zipped
        except Exception:  # noqa: BLE001
            pass
        fixed = re.sub(
            r'(?i)\b(find|cd|ls|du)\s+"(~/[^"]*?)\s+(-[a-zA-Z])',
            r"\1 \2 \3",
            cmd,
        )
        fixed = re.sub(
            r'(?i)\s+"(~/[^"]*?)\s+(-[a-zA-Z])',
            r" \1 \2",
            fixed,
        )
        # Drop stray quote fragments left after the mangled name pattern.
        fixed = re.sub(r""""'\*\.(\w+)'""", r"'*.\1'", fixed)
        fixed = re.sub(r'''"'\*\.(\w+)'"''', r"'*.\1'", fixed)
        if fixed != cmd and not _bash_command_looks_broken(fixed):
            return fixed

    # Whole-volume / root finds are empty or hang; biggest-file asks → home scan.
    root_find = bool(
        re.search(r"(?i)\bfind\s+(/Volumes(?:/\S*)?|/(?:\s|$)|/System\b)", cmd)
    )
    if _WANTS_BIGGEST_FILE_RE.search(blob) or (
        root_find and re.search(r"(?i)\b(du|sort|head)\b", cmd)
    ):
        if cmd != _BIGGEST_FILE_BASH:
            return _BIGGEST_FILE_BASH
    if root_find and "-maxdepth" not in cmd.lower():
        return _BIGGEST_FILE_BASH
    # Linux checksum → macOS.
    if re.search(r"(?i)\bsha256sum\b", cmd):
        return re.sub(r"(?i)\bsha256sum\b", "shasum -a 256", cmd)
    if re.search(r"(?i)\bmd5sum\b", cmd):
        return re.sub(r"(?i)\bmd5sum\b", "md5 -r", cmd)
    # Drop GNU-only find flags macOS BSD find rejects (keep the rest of the cmd).
    if re.search(r"(?i)-printf\b", cmd):
        repaired = re.sub(r"(?i)\s*-printf\s+'[^']*'|\s*-printf\s+\"[^\"]*\"|\s*-printf\s+\S+", "", cmd)
        repaired = re.sub(r"\s{2,}", " ", repaired).strip()
        if repaired and repaired != cmd:
            return repaired

    # Clear zip-PDFs asks: prefer a known-good pipeline over a broken find -exec.
    if re.search(r"(?i)\b(zip|compress|archive)\b", utterance or "") and (
        _bash_command_looks_broken(cmd)
        or re.search(r'(?i)syntax|\\\\";|"~/Downloads\s+-', cmd)
    ):
        try:
            from llm.inference import _deterministic_zip_command

            zipped = _deterministic_zip_command(utterance or "")
            if zipped and zipped != cmd:
                return zipped
        except Exception:  # noqa: BLE001
            pass

    # Broken curl (URL on next line / missing URL) or explicit reachability ask.
    if re.search(r"(?i)\bcurl\b", cmd) or re.search(
        r"(?i)\breachable\b|\bresponse\s+time\b|\blatency\b|\bping\b",
        utterance or "",
    ):
        try:
            from llm.inference import _deterministic_reachability_command

            reach = _deterministic_reachability_command(utterance or "")
            if reach and reach != cmd:
                return reach
        except Exception:  # noqa: BLE001
            pass
        # Soft fix: join newline before https:// into one curl argv.
        if re.search(r"(?i)\bcurl\b", cmd) and re.search(
            r"(?i)\n\s*https?://", cmd
        ):
            joined = re.sub(r"\n\s*(https?://\S+)", r" \1", cmd)
            joined = re.sub(r"\s{2,}", " ", joined).strip()
            if joined != cmd and not _bash_command_looks_broken(joined):
                return joined
    return None


def _bash_command_looks_broken(command: str) -> bool:
    """True when a planner-invented shell string is truncated or nonsensical."""
    try:
        from llm.inference import _bash_looks_broken_cmd

        if _bash_looks_broken_cmd(command):
            return True
    except Exception:  # noqa: BLE001
        pass
    cmd = (command or "").strip()
    if not cmd:
        return True
    if len(cmd) > 700:
        return True
    # Obviously incomplete JSON-ish or cut mid-token.
    if re.search(r"(?i)\b(xargs|find|awk|sort)\s*$", cmd):
        return True
    return False



def _process_monitor_heuristic(text: str) -> Optional[dict[str, Any]]:
    """Local-only: list top CPU/memory processes (never invent from history/cloud)."""
    if not text:
        return None
    if re.search(
        r"(?i)\b("
        r"top\s+(cpu|memory|ram|process(?:es)?|apps)|"
        r"(cpu|memory|ram)\s+(hogs?|usage|process(?:es)?)|"
        r"what('?s|\s+is)\s+using\s+(my\s+)?(cpu|memory|ram)|"
        r"list\s+(all\s+|the\s+|my\s+|running\s+)*process(?:es)?|"
        r"show\s+(all\s+|the\s+|my\s+|running\s+)*process(?:es)?|"
        r"(all|running)\s+process(?:es)?|"
        r"what\s+process(?:es)?\s+(are\s+)?(running|on)|"
        r"process(?:es)?\s+on\s+(my\s+)?(mac|computer|machine)|"
        r"system\s+resources|"
        r"resource\s+usage|"
        r"\bps\s+aux\b|\bactivity\s+monitor\b"
        r")\b",
        text,
    ):
        return {"tool": "manage_system_resources", "args": {"action": "list"}}
    return None


def _safety_heuristic_tool(utterance: str) -> Optional[dict[str, Any]]:
    """Minimal fast-path only — greetings, help, notes, math, hard power/trash.

    Everything else (wifi, open app, mute, search, …) is planned by the LLM.
    """
    text = (utterance or "").strip()
    if not text:
        return None

    if _META_RE.search(text):
        return {"tool": "respond", "args": {"text": _CAPABILITIES_TEXT}}

    if _ABOUT_ME_RE.search(text):
        return {"tool": "__answer_about_user__", "args": {}}

    if _CHAT_RE.match(text):
        return {
            "tool": "respond",
            "args": {
                "text": "Hey — I'm MacAgent. Ask me anything, or say “what can you do?” for a quick list."
            },
        }

    # Confirm-gated destructive actions — keep deterministic for safety.
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

    # Process monitor — must stay local (never invent from chat history / cloud).
    proc = _process_monitor_heuristic(text)
    if proc:
        return proc

    # Biggest file on disk — deterministic home-scoped scan (never /Volumes).
    if _WANTS_BIGGEST_FILE_RE.search(text):
        return {"tool": "run_bash", "args": {"command": _BIGGEST_FILE_BASH}}

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

    return None



def _control_mac_heuristic(utterance: str) -> Optional[dict[str, Any]]:
    """Deterministic Wi‑Fi / Bluetooth / volume / appearance toggles."""
    text = (utterance or "").strip()
    if not text:
        return None

    # Don't steal "open wifi settings" — that path uses open_system_settings.
    if _is_open_action(text) and re.search(
        r"(?i)\b(settings?|preferences?|pane)\b", text
    ):
        return None

    # Wi‑Fi
    if re.search(r"(?i)\b(wi-?fi|wifi|wireless(\s+network)?|airport)\b", text):
        if re.search(r"(?i)\b(turn|switch|put)\s+off\b|\bdisable\b|\bshut\s*off\b", text):
            return {"tool": "control_mac", "args": {"feature": "wifi", "state": "off"}}
        if re.search(r"(?i)\b(turn|switch|put)\s+on\b|\benable\b", text):
            return {"tool": "control_mac", "args": {"feature": "wifi", "state": "on"}}
        if re.search(r"(?i)\b(toggle|flip)\b", text):
            return {
                "tool": "control_mac",
                "args": {"feature": "wifi", "state": "toggle"},
            }

    # Bluetooth
    if re.search(r"(?i)\b(bluetooth|blue\s*tooth)\b", text):
        if re.search(r"(?i)\b(turn|switch|put)\s+off\b|\bdisable\b|\bshut\s*off\b", text):
            return {
                "tool": "control_mac",
                "args": {"feature": "bluetooth", "state": "off"},
            }
        if re.search(r"(?i)\b(turn|switch|put)\s+on\b|\benable\b", text):
            return {
                "tool": "control_mac",
                "args": {"feature": "bluetooth", "state": "on"},
            }
        if re.search(r"(?i)\b(toggle|flip)\b", text):
            return {
                "tool": "control_mac",
                "args": {"feature": "bluetooth", "state": "toggle"},
            }

    # Volume mute / unmute / set level
    if re.search(r"(?i)\bunmute\b", text) or re.search(
        r"(?i)\b(turn|switch)\s+(the\s+)?(volume|sound)\s+on\b", text
    ):
        return {"tool": "control_mac", "args": {"feature": "volume", "state": "unmute"}}
    if re.search(r"(?i)\bmute\b", text) or re.search(
        r"(?i)\b(turn|switch)\s+(the\s+)?(volume|sound)\s+off\b", text
    ):
        return {"tool": "control_mac", "args": {"feature": "volume", "state": "mute"}}
    vol_m = re.search(
        r"(?i)\b(set|change)\s+(the\s+)?(volume|sound)\s+(to\s+)?(?P<n>\d{1,3})\s*%?",
        text,
    )
    if vol_m:
        return {
            "tool": "control_mac",
            "args": {"feature": "volume", "state": vol_m.group("n")},
        }

    # Appearance
    if re.search(r"(?i)\b(dark\s*mode|dark\s*theme)\b", text):
        if re.search(r"(?i)\b(turn|switch|put)\s+off\b|\bdisable\b|\blight\b", text):
            return {
                "tool": "control_mac",
                "args": {"feature": "appearance", "state": "light"},
            }
        return {"tool": "control_mac", "args": {"feature": "appearance", "state": "dark"}}
    if re.search(r"(?i)\b(light\s*mode|light\s*theme)\b", text):
        return {
            "tool": "control_mac",
            "args": {"feature": "appearance", "state": "light"},
        }
    if re.search(r"(?i)\b(toggle|switch)\s+(the\s+)?(appearance|theme)\b", text):
        return {
            "tool": "control_mac",
            "args": {"feature": "appearance", "state": "toggle"},
        }

    return None


def _refuse_system_action(utterance: str, message: str) -> dict[str, Any]:
    """On a wrong planned tool, remap to a real heuristic action when possible."""
    remapped = (
        _control_mac_heuristic(utterance)
        or _close_app_heuristic(utterance)
        or _remap_catalog_hijack(utterance, [])
    )
    if remapped:
        return remapped
    return {
        "tool": "respond",
        "args": {"text": message, "goal_done": True},
    }


def _remap_catalog_hijack(
    utterance: str, history: Optional[list[dict[str, Any]]] = None
) -> Optional[dict[str, Any]]:
    """When the planner invents a catalog example tool, recover the real ask."""
    text = (utterance or "").strip()
    if not text:
        return None
    hist = history or []

    # Open Calculator / type / read screen — never answer with Wi‑Fi refusal.
    if _needs_gui_control(text) or _is_open_action(text):
        opened = any(
            (h.get("call") or {}).get("tool") == "open_app"
            and (h.get("result") or {}).get("ok")
            for h in hist
        )
        if not opened and _is_open_action(text):
            name = _open_app_name_from_utterance(text)
            if name:
                return {"tool": "open_app", "args": {"name": name}}
        if _needs_gui_control(text):
            # Prefer typing the expression when they asked to type a calculation.
            expr = _calc_expression_from_utterance(text)
            if expr:
                args: dict[str, Any] = {"text": expr}
                for item in reversed(hist):
                    call = item.get("call") or {}
                    if call.get("tool") == "open_app":
                        app = str((call.get("args") or {}).get("name") or "").strip()
                        if app:
                            args["app"] = app
                        break
                if "app" not in args and re.search(r"(?i)\bcalculator\b", text):
                    args["app"] = "Calculator"
                return {"tool": "ui_type", "args": args}
            return {"tool": "ui_snapshot", "args": {"limit": 40}}
        if _is_open_action(text):
            name = _open_app_name_from_utterance(text)
            if name:
                return {"tool": "open_app", "args": {"name": name}}

    if _needs_local_machine_check(text):
        if _utterance_wants_web_search(text) and not any(
            (h.get("call") or {}).get("tool") == "web_search" for h in hist
        ):
            return {
                "tool": "web_search",
                "args": {"query": _hybrid_web_search_query(text)},
            }
        local_cmd = _preferred_local_check_command(text)
        if local_cmd:
            return {"tool": "run_bash", "args": {"command": local_cmd}}
        return {"tool": "__write_and_run_bash__", "args": {}}

    return None


def _hybrid_web_search_query(text: str) -> str:
    """Sharper query for 'latest X + am I out of date?' hybrid asks."""
    t = text or ""
    if re.search(r"(?i)\bpython\b", t):
        return "latest stable Python release site:python.org"
    if re.search(r"(?i)\bnode(\.?js)?\b", t):
        return "latest stable Node.js LTS release"
    if re.search(r"(?i)\bruby\b", t):
        return "latest stable Ruby release"
    if re.search(r"(?i)\bjava\b", t):
        return "latest stable Java JDK release"
    return t


def _open_app_name_from_utterance(text: str) -> Optional[str]:
    cleaned = _scrub_open_compounds(text or "")
    m = re.search(
        r"(?i)(?:^|\b)(?:please\s+|can\s+you\s+|could\s+you\s+)?"
        r"(?:open|launch|start)\s+(?P<name>[A-Za-z][A-Za-z0-9 .+-]{1,40}?)"
        r"(?=\s*[,.]|\s+and\b|\s+type\b|\s+then\b|$)",
        cleaned,
    )
    if not m:
        return None
    name = m.group("name").strip(" .,")
    # Avoid swallowing the rest of a sentence.
    name = re.split(r"(?i)\s+(type|click|tell|and|then)\b", name)[0].strip()
    return name or None


def _calc_expression_from_utterance(text: str) -> Optional[str]:
    """Best-effort '154 multiplied by 8' → '154*8=' for Calculator keystroke."""
    t = text or ""
    m = re.search(
        r"(?i)\btype\s+(?P<a>\d+(?:\.\d+)?)\s*"
        r"(?P<op>multiplied\s+by|times|divided\s+by|over|plus|minus|\*|x|/|\+|-)\s*"
        r"(?P<b>\d+(?:\.\d+)?)",
        t,
    )
    if not m:
        m = re.search(
            r"(?i)(?P<a>\d+(?:\.\d+)?)\s*"
            r"(?P<op>multiplied\s+by|times|divided\s+by|\*|x|/|\+|-)\s*"
            r"(?P<b>\d+(?:\.\d+)?)",
            t,
        )
    if not m:
        return None
    op_raw = m.group("op").lower()
    if "multipl" in op_raw or op_raw in {"times", "*", "x"}:
        op = "*"
    elif "divid" in op_raw or op_raw in {"over", "/"}:
        op = "/"
    elif "plus" in op_raw or op_raw == "+":
        op = "+"
    elif "minus" in op_raw or op_raw == "-":
        op = "-"
    else:
        op = "*"
    return f"{m.group('a')}{op}{m.group('b')}="


def _command_is_destructive(command: str) -> bool:
    from tools.run_bash import classify_command

    return classify_command(command) in {"needs_confirm", "hard_block"}


def _sanitize_planned_call(
    utterance: str,
    call: dict[str, Any],
    history: Optional[list[dict[str, Any]]] = None,
    use_web: str = "auto",
) -> dict[str, Any]:
    """Block hallucinated destructive / UI tools on casual chat."""
    tool = (call.get("tool") or "").strip()
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    text = (utterance or "").strip()
    hist = history or []
    mode = use_web if use_web in ("auto", "on", "off") else "auto"

    # Follow-up deixis on a prior path — don't make the small planner invent tools.
    prior_path = _path_from_prior_assistant()
    if prior_path and re.search(
        r"(?i)^\s*(please\s+|can\s+you\s+|could\s+you\s+)?("
        r"open|show|reveal|launch"
        r")\s+(it|that|this|the\s+file|the\s+folder)\b",
        text,
    ):
        import shlex

        q = shlex.quote(prior_path)
        return {
            "tool": "run_bash",
            "args": {"command": f"open {q} && echo Opened: {q}"},
        }
    if prior_path and re.search(
        r"(?i)^\s*(please\s+|can\s+you\s+|could\s+you\s+)?("
        r"delete|remove|trash|rm"
        r")\s+(it|that|this|the\s+file)\b",
        text,
    ):
        import shlex

        q = shlex.quote(prior_path)
        return {
            "tool": "run_bash",
            "args": {"command": f"rm -- {q} && echo Deleted: {q}"},
        }

    # File locate → bash only (never Spotlight / find_files / mdfind).
    if tool in {"spotlight_file_search", "find_files"}:
        return {"tool": "__write_and_run_bash__", "args": {}}
    if tool == "run_bash" and re.search(
        r"(?i)\bmdfind\b", str(args.get("command") or "")
    ):
        return {"tool": "__write_and_run_bash__", "args": {}}

    # Mac side-effects: don't web-howto a pure local task — but hybrid
    # "search the web AND check my Mac" must keep web_search first.
    if (
        tool == "web_search"
        and mode != "on"
        and _is_action_request(text)
        and not _utterance_wants_web_search(text)
    ):
        return {"tool": "__write_and_run_bash__", "args": {}}

    # Hybrid asks: if the planner skipped the web half, insert it before local.
    if (
        mode != "off"
        and _needs_local_machine_check(text)
        and _utterance_wants_web_search(text)
        and tool in {"run_bash", "respond", "__write_and_run_bash__"}
        and not any((h.get("call") or {}).get("tool") == "web_search" for h in hist)
    ):
        return {
            "tool": "web_search",
            "args": {"query": _hybrid_web_search_query(text)},
        }

    # Rewrite Linux-only find / Downloads guesses before they run.
    if tool == "run_bash":
        cmd = str(args.get("command") or "")
        if _bash_command_looks_broken(cmd):
            # Prefer repairing a nearly-right plan over regenerating (Downloads trap).
            from llm.inference import (
                _deterministic_create_command,
                _deterministic_reachability_command,
                _deterministic_zip_command,
                _repair_dropped_home_folder,
            )

            repaired_path = _repair_dropped_home_folder(cmd, text)
            if repaired_path and not _bash_command_looks_broken(repaired_path):
                return {"tool": "run_bash", "args": {"command": repaired_path}}
            deterministic = (
                _deterministic_create_command(text)
                or _deterministic_zip_command(text)
                or _deterministic_reachability_command(text)
            )
            if deterministic:
                return {"tool": "run_bash", "args": {"command": deterministic}}
            return {"tool": "__write_and_run_bash__", "args": {}}
        # Coerce version-check asks to a single reliable local command.
        # Avoids `python3 && python && pip` dying on missing `python`.
        local_cmd = _preferred_local_check_command(text)
        if local_cmd and _needs_local_machine_check(text) and cmd.strip() != local_cmd:
            return {"tool": "run_bash", "args": {"command": local_cmd}}
        # Reachability / latency — always use the known-good multi-probe curl.
        try:
            from llm.inference import _deterministic_reachability_command

            reach = _deterministic_reachability_command(text)
        except Exception:  # noqa: BLE001
            reach = ""
        if reach:
            return {"tool": "run_bash", "args": {"command": reach}}
        # Never curl|grep the web from bash when the user asked to search.
        if local_cmd and re.search(r"(?i)\b(curl|wget)\b", cmd):
            return {"tool": "run_bash", "args": {"command": local_cmd}}
        # Dropped ~/Documents → ~/Name hallucination from the planner.
        try:
            from llm.inference import _repair_dropped_home_folder

            anchored = _repair_dropped_home_folder(cmd, text)
            if anchored and anchored != cmd and not _bash_command_looks_broken(anchored):
                cmd = anchored
        except Exception:  # noqa: BLE001
            pass
        repaired = _repair_bash_command(cmd, text)
        if repaired and repaired != cmd:
            cmd = repaired
        from tools.run_bash import quote_paths_with_spaces

        quoted = quote_paths_with_spaces(cmd)
        if quoted != str(args.get("command") or "").strip():
            return {"tool": "run_bash", "args": {"command": quoted}}
        if cmd != str(args.get("command") or "").strip():
            return {"tool": "run_bash", "args": {"command": cmd}}

    # After open_app, stamp the app name onto ui_type so keys hit Calculator not the overlay.
    if tool in {"ui_type", "ui_key", "ui_click", "ui_snapshot"}:
        if not str(args.get("app") or "").strip():
            for item in reversed(hist):
                call = item.get("call") or {}
                result = item.get("result") or {}
                if call.get("tool") != "open_app" or not result.get("ok"):
                    continue
                name = str((call.get("args") or {}).get("name") or "").strip()
                if name:
                    merged = dict(args)
                    merged["app"] = name
                    return {"tool": tool, "args": merged}
                break

    # On-disk Downloads / files must run locally — never cloud inventing "I ran ls".
    # Do not force a canned "latest download" command; let the planner/bash writer decide.
    if tool in {"respond", "web_search"} and re.search(
        r"(?i)\bdownl", text
    ):
        return {"tool": "__write_and_run_bash__", "args": {}}

    # Factual lookups must search before respond / run_python hallucinations.
    if (
        mode != "off"
        and _is_factual_lookup(text)
        and tool in {"respond", "run_python"}
    ):
        if not any((h.get("call") or {}).get("tool") == "web_search" for h in hist):
            return {"tool": "web_search", "args": {"query": text}}

    if mode == "on" and tool == "respond" and not _CHAT_RE.match(text):
        if not any((h.get("call") or {}).get("tool") == "web_search" for h in hist):
            if not _META_RE.search(text):
                return {"tool": "web_search", "args": {"query": text}}

    if mode == "off" and tool == "web_search":
        return {
            "tool": "respond",
            "args": {
                "text": (
                    "Web search is off for this ask. "
                    "Turn Search to Auto or On, or ask me to do something local."
                ),
                "goal_done": True,
            },
        }

    if _CHAT_RE.match(text) or _META_RE.search(text):
        if tool != "respond":
            if _META_RE.search(text):
                return {"tool": "respond", "args": {"text": _CAPABILITIES_TEXT}}
            return {
                "tool": "respond",
                "args": {
                    "text": "Hey — I'm MacAgent. Ask me anything, or say “what can you do?”",
                    "goal_done": True,
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

    # Prefer deterministic Mac toggles over whatever the small planner invented.
    if tool != "control_mac":
        control = _control_mac_heuristic(text)
        if control and tool in {
            "control_power_management",
            "modify_system_setting",
            "manage_system_resources",
            "run_bash",
            "open_system_settings",
            "ui_snapshot",
            "ui_click",
            "ui_type",
            "ui_key",
            "ui_menu",
        }:
            return control

    # Never allow control_mac unless the utterance clearly asks for that toggle.
    # IQ2/small planners invent "dark mode" from catalog examples on unrelated asks.
    if tool == "control_mac":
        expected = _control_mac_heuristic(text)
        if expected:
            return expected
        # Catalog hijack — remap to the real Mac intent instead of a Wi‑Fi refusal.
        remapped = _remap_catalog_hijack(text, hist)
        if remapped:
            return remapped
        if mode != "off" and _is_info_question(text):
            return {"tool": "web_search", "args": {"query": text}}
        return {
            "tool": "respond",
            "args": {
                "text": (
                    "I won't change Wi‑Fi / volume / appearance unless you ask "
                    "(e.g. “dark mode”, “mute”, “turn off wifi”)."
                ),
                "goal_done": True,
            },
        }

    if tool == "run_bash":
        cmd = str(args.get("command") or "")
        if _command_is_destructive(cmd) and not _DESTRUCTIVE_UTTERANCE_RE.search(text):
            return _refuse_system_action(
                text,
                "I almost ran a system action you didn't ask for — skipped it. "
                "What would you like me to do?",
            )

    if tool in {"ui_snapshot", "ui_click", "ui_type", "ui_key", "ui_menu"}:
        if not _is_action_request(text) or _CHAT_RE.match(text):
            return {
                "tool": "respond",
                "args": {
                    "text": (
                        "I don't need screen control for that. "
                        "Ask a question or tell me what to open/do."
                    ),
                    "goal_done": True,
                },
            }

    # Don't invent system mutations / kills on casual chat.
    if tool in {
        "modify_system_setting",
        "control_power_management",
        "manage_system_resources",
        "trigger_native_notification",
        "control_mac",
    }:
        if _CHAT_RE.match(text) or (
            tool == "manage_system_resources"
            and str(args.get("action") or "").lower() == "kill"
            and not re.search(r"(?i)\b(kill|quit|force[\s-]?quit|terminate|close)\b", text)
        ):
            return _refuse_system_action(
                text,
                "I almost ran a system action you didn't ask for — skipped it. "
                "What would you like me to do?",
            )

    # Catalog-example hijack: pmset / defaults only when the user asked for that.
    if tool == "control_power_management" and not _POWER_UTTERANCE_RE.search(text):
        close = _close_app_heuristic(text)
        if close:
            return close
        control = _control_mac_heuristic(text)
        if control:
            return control
        remapped = _remap_catalog_hijack(text, hist)
        if remapped:
            return remapped
        return {
            "tool": "respond",
            "args": {
                "text": (
                    "I won't change power/sleep settings unless you ask for that. "
                    "If you meant to close an app, say e.g. “close Chrome”."
                ),
                "goal_done": True,
            },
        }

    if tool == "modify_system_setting" and not _PREF_UTTERANCE_RE.search(text):
        close = _close_app_heuristic(text)
        if close:
            return close
        control = _control_mac_heuristic(text)
        if control:
            return control
        remapped = _remap_catalog_hijack(text, hist)
        if remapped:
            return remapped
        if mode != "off" and _is_info_question(text):
            return {"tool": "web_search", "args": {"query": text}}
        if re.search(r"(?i)\b(close|quit|kill)\b", text):
            return {
                "tool": "respond",
                "args": {
                    "text": (
                        "To close an app, ask me to quit it by name "
                        "(e.g. “close Chrome”) — I won't change system prefs for that."
                    ),
                    "goal_done": True,
                },
            }

    # Info questions must not run Mac mutation tools the planner invents.
    if _is_info_question(text) and tool in {
        "modify_system_setting",
        "control_power_management",
        "manage_system_resources",
        "open_system_settings",
        "ui_snapshot",
        "ui_click",
        "ui_type",
        "ui_key",
        "ui_menu",
    }:
        if mode == "off":
            return {
                "tool": "respond",
                "args": {
                    "text": (
                        "Web search is off for this ask. "
                        "Turn Search to Auto or On to look that up online."
                    ),
                    "goal_done": True,
                },
            }
        return {"tool": "web_search", "args": {"query": text}}

    return _apply_use_web(
        utterance,
        {"tool": tool, "args": args},
        history=hist,
        use_web=mode,
    )


def _apply_use_web(
    utterance: str,
    call: dict[str, Any],
    *,
    history: Optional[list[dict[str, Any]]] = None,
    use_web: str = "auto",
) -> dict[str, Any]:
    """Honor overlay Search chip: auto | on | off."""
    mode = use_web if use_web in ("auto", "on", "off") else "auto"
    tool = (call.get("tool") or "").strip()
    text = (utterance or "").strip()
    hist = history or []

    if mode == "off" and tool == "web_search":
        return {
            "tool": "respond",
            "args": {
                "text": (
                    "Web search is off for this ask. "
                    "Turn Search to Auto or On, or ask me to do something local."
                ),
                "goal_done": True,
            },
        }

    if mode == "on" and tool == "respond" and text and not _CHAT_RE.match(text):
        if _META_RE.search(text) or _ABOUT_ME_RE.search(text):
            return call
        if not any((h.get("call") or {}).get("tool") == "web_search" for h in hist):
            return {"tool": "web_search", "args": {"query": text}}

    return call


def _friendly_ui_error(raw: str) -> str:
    lower = (raw or "").lower()
    if (
        "assistive" in lower
        or "not allowed" in lower
        or "accessibility" in lower
        or "osascript is not allowed" in lower
        or "untrusted" in lower
    ):
        return (
            "macOS still reports MacAgent as untrusted for screen control "
            "(common after a rebuild even if the toggle looks On).\n\n"
            "1. System Settings → Privacy & Security → Accessibility\n"
            "2. Turn MacAgent OFF, then ON again\n"
            "3. Quit MacAgent (menu bar → Quit) and reopen /Applications/MacAgent.app\n\n"
            "Enable that app only — not AEServer or Terminal."
        )
    if "invalid request" in lower or "empty request" in lower or "incomplete http" in lower:
        return (
            "Screen control glitched talking to MacAgent.app. "
            "Keep the MacAgent app running, then try again "
            "(or rebuild/relaunch the app if this keeps happening)."
        )
    if "bridge is offline" in lower or "bridge_down" in lower:
        return (
            "UI bridge is offline. Open/restart the MacAgent app (menu bar) "
            "so screen control can use Accessibility."
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
            "control_mac",
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
                    r"(?i)\b(rm|rmdir|mv|cp|empty|trash|shut\s*down|restart|sleep)\b",
                    cmd,
                ):
                    return True
            out = str(result.get("stdout") or "")
            if result.get("ok") and re.search(
                r"(?i)^(deleted|removed|emptied|moved|opened):", out
            ):
                return True
    return False


def _bash_respects_home_folder(command: str, utterance: str) -> bool:
    """False when the ask required ~/Documents but the command omitted it."""
    try:
        from llm.inference import _home_folder_from_ask
    except Exception:  # noqa: BLE001
        return True
    folder = _home_folder_from_ask(utterance)
    if not folder:
        return True
    cmd = command or ""
    if not re.search(r"(?i)\b(mkdir|touch|echo|printf|mv|cp)\b", cmd):
        return True
    return bool(re.search(rf"(?i)(?:~/|\$HOME/){re.escape(folder)}\b", cmd))


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

    # Create folder / file — require the mutating bash and the right parent folder.
    if re.search(
        r"(?i)\b(create|make|new)\b.*\b(folder|director(?:y|ies)|file)\b"
        r"|\b(folder|director(?:y|ies)|file)\b.*\b(create|make|new)\b",
        text,
    ):
        saw_create = False
        wrong_folder = False
        only_discovery = True
        for item in history:
            call = item.get("call") or {}
            result = item.get("result") or {}
            if call.get("tool") != "run_bash":
                continue
            cmd = str((call.get("args") or {}).get("command") or "")
            if _is_discovery_bash(cmd):
                continue
            only_discovery = False
            if result.get("ok") and re.search(r"(?i)\b(mkdir|touch|echo|printf)\b", cmd):
                saw_create = True
                if not _bash_respects_home_folder(cmd, text):
                    wrong_folder = True
        if wrong_folder:
            return "incomplete"
        if saw_create:
            return "done"
        if only_discovery:
            return "incomplete"
        return "unknown"

    if _is_open_action(text) and not _DELETE_RE.search(text):
        # "Open Calculator, type …, tell me the result" — launch alone is incomplete.
        if _needs_gui_control(text):
            has_open = any(
                (h.get("call") or {}).get("tool")
                in {"open_app", "open_url", "open_folder", "open_system_settings"}
                and (h.get("result") or {}).get("ok")
                for h in history
            ) or any(
                (h.get("call") or {}).get("tool") == "run_bash"
                and (h.get("result") or {}).get("ok")
                and re.search(
                    r"(?i)\bopen\b",
                    str(((h.get("call") or {}).get("args") or {}).get("command") or ""),
                )
                for h in history
            )
            has_ui = _history_has_ui_action(history)
            if has_open and not has_ui:
                return "incomplete"
            if has_ui and _needs_screen_read(text) and not _history_has_ui_snapshot(
                history
            ):
                return "incomplete"
            if has_ui and (not _needs_screen_read(text) or _history_has_ui_snapshot(history)):
                return "done"
            return "incomplete" if not has_open else "unknown"
        # Plain open asks.
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

    # Wi‑Fi / Bluetooth / mute / appearance — done once control_mac succeeds.
    if re.search(
        r"(?i)\b("
        r"wi-?fi|wifi|bluetooth|mute|unmute|"
        r"dark\s*mode|light\s*mode|appearance|theme"
        r")\b",
        text,
    ) and re.search(
        r"(?i)\b(turn|switch|put|enable|disable|toggle|set|mute|unmute)\b",
        text,
    ):
        if any(
            (h.get("call") or {}).get("tool") == "control_mac"
            and (h.get("result") or {}).get("ok")
            for h in history
        ):
            return "done"
        return "incomplete"

    # "Search the web… and check what's on my Mac" — research alone is intermediate.
    if _needs_local_machine_check(text):
        if not _history_has_successful_bash(history):
            return "incomplete"
        if _utterance_wants_web_search(text) and _web_search_count(history) == 0:
            return "incomplete"
        return "unknown"

    # Any Mac action: answering with "run this yourself" is not finished.
    if (
        _is_action_request(text)
        and _candidate_is_shell_advice(cand)
        and not _history_has_successful_bash(history)
    ):
        return "incomplete"

    return "unknown"


def _goal_incomplete_reason(
    utterance: str, history: list[dict[str, Any]], candidate: str
) -> str:
    if _needs_local_machine_check(utterance or ""):
        if not _history_has_successful_bash(history):
            return "researched or advised, but did not check this Mac yet"
        if _utterance_wants_web_search(utterance or "") and _web_search_count(
            history
        ) == 0:
            return "checked this Mac but did not search the web for the latest yet"
        if _candidate_is_shell_advice(candidate or ""):
            return "suggested a terminal command instead of running it"
        return "local Mac check not finished yet"
    if _needs_gui_control(utterance or ""):
        if not _history_has_ui_action(history):
            return "app opened but on-screen type/click not done yet"
        if _needs_screen_read(utterance or "") and not _history_has_ui_snapshot(history):
            return "typed/clicked but did not read the screen result yet"
        return "GUI steps not finished yet"
    if _candidate_is_shell_advice(candidate or "") and _is_action_request(utterance or ""):
        return "gave advice to run a command instead of running it"
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


def _needs_gui_control(text: str) -> bool:
    """True when the ask needs ui_type / ui_click / screen read, not just open."""
    return bool(_GUI_FOLLOWUP_RE.search(text or ""))


def _needs_screen_read(text: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b("
            r"on[\s-]?screen|read\s+(the\s+)?(screen|result|display)|"
            r"tell\s+me\s+(the\s+)?(result|answer|number)|"
            r"what('?s|\s+is)\s+(on\s+)?(the\s+)?(screen|display|result)"
            r")\b",
            text or "",
        )
    )


def _needs_local_machine_check(text: str) -> bool:
    """True when the user wants MacAgent to inspect THIS Mac (not only advise)."""
    return bool(_LOCAL_MACHINE_CHECK_RE.search(text or ""))


def _utterance_wants_web_search(text: str) -> bool:
    """True when the user explicitly asked to look something up online."""
    return bool(
        re.search(
            r"(?i)\b("
            r"search\s+(the\s+)?web|look\s+up\s+online|google|"
            r"latest|current\s+stable|newest\s+(stable\s+)?(release|version)|"
            r"what('?s|\s+is)\s+the\s+latest|"
            r"from\s+the\s+(web|internet)|online\b"
            r")\b",
            text or "",
        )
    )


def _preferred_local_check_command(text: str) -> Optional[str]:
    """Deterministic local inspect command for common 'am I up to date?' asks."""
    t = text or ""
    if re.search(r"(?i)\bpython\b", t):
        return "python3 --version"
    if re.search(r"(?i)\bnode(\.?js)?\b", t):
        return "node --version"
    if re.search(r"(?i)\bruby\b", t):
        return "ruby --version"
    if re.search(r"(?i)\bjava\b", t):
        return "java -version 2>&1"
    if re.search(r"(?i)\b(macos|mac\s*os)\s+version|sw_vers\b", t):
        return "sw_vers"
    try:
        from llm.inference import _deterministic_reachability_command

        reach = _deterministic_reachability_command(t)
        if reach:
            return reach
    except Exception:  # noqa: BLE001
        pass
    return None


def _bash_stdout_is_useless(out: str) -> bool:
    t = (out or "").strip()
    if not t:
        return True
    return bool(
        re.search(
            r"(?i)^(error|failed|unable|cannot|command not found)\b|"
            r"error fetching|no matches|nothing found",
            t,
        )
    )


def _candidate_is_shell_advice(text: str) -> bool:
    """True when the reply tells the user to run a command instead of doing it."""
    t = text or ""
    if not t.strip():
        return False
    if not _SHELL_ADVICE_RE.search(t):
        # Bare backtick shell without "you can run" still counts if clearly a cmd.
        return bool(
            re.search(
                r"(?i)`(python3?|node|npm|brew|sw_vers|uname|which|pip3?)\b[^`]*`",
                t,
            )
        )
    return True


def _extract_suggested_shell_commands(text: str) -> list[str]:
    """Pull runnable shell snippets out of intermediate advice / answers."""
    out: list[str] = []
    seen: set[str] = set()

    # Critic / planner hints: run_bash cmd='mkdir …' or bare mkdir …
    hint_cmd = _command_from_next_hint(text)
    if hint_cmd and hint_cmd not in seen:
        seen.add(hint_cmd)
        out.append(hint_cmd)

    for m in re.finditer(r"`([^`\n]{2,200})`", text or ""):
        cmd = m.group(1).strip().lstrip("$").strip()
        if not cmd or cmd in seen:
            continue
        # Skip URLs and prose.
        if re.search(r"(?i)^https?://", cmd) or " " not in cmd and "/" in cmd and not cmd.startswith("~"):
            if not re.match(
                r"(?i)^(python|python3|node|npm|npx|brew|pip|pip3|sw_vers|"
                r"uname|which|ls|df|du|git|ruby|java|go|rustc|php)\b",
                cmd,
            ):
                continue
        if re.match(
            r"(?i)^(python|python3|node|npm|npx|brew|pip|pip3|sw_vers|uname|"
            r"which|ls|df|du|git|ruby|java|system_profiler|defaults|"
            r"mkdir|touch|echo|printf|open|mv|cp|rm|find)\b",
            cmd,
        ) or re.search(r"(?i)--version|-V\b", cmd):
            seen.add(cmd)
            out.append(cmd)
    return out[:3]


def _command_from_next_hint(hint: str) -> Optional[str]:
    """Extract a runnable shell command from a goal-check next_hint."""
    h = (hint or "").strip()
    if not h or h.lower() in {"none", "n/a", "null"}:
        return None
    m = re.search(
        r"(?i)run_bash\s+cmd\s*=\s*['\"](.+)['\"]\s*$",
        h,
        re.DOTALL,
    )
    if m:
        cmd = m.group(1).strip()
        cmd = cmd.replace('\\"', '"').replace("\\'", "'")
        return cmd or None
    m = re.search(r"(?i)\bcmd\s*=\s*['\"](.+)['\"]\s*$", h, re.DOTALL)
    if m:
        cmd = m.group(1).strip().replace('\\"', '"').replace("\\'", "'")
        return cmd or None
    if re.match(
        r"(?i)^(mkdir|touch|echo|printf|ls|mv|cp|rm|open|find|python3|node)\b",
        h,
    ):
        return h
    return None


def _command_from_goal_hints(history: list[dict[str, Any]]) -> Optional[str]:
    """Latest goal-check next_hint command, if any."""
    for item in reversed(history or []):
        call = item.get("call") or {}
        result = item.get("result") or {}
        if call.get("tool") != "_goal_check":
            continue
        cmd = _command_from_next_hint(str(result.get("next_hint") or ""))
        if cmd and not _bash_command_looks_broken(cmd):
            return cmd
    return None


def _history_has_successful_bash(history: list[dict[str, Any]]) -> bool:
    for item in history:
        call = item.get("call") or {}
        result = item.get("result") or {}
        if call.get("tool") != "run_bash":
            continue
        out = (result.get("stdout") or "").strip()
        if not out or _bash_stdout_is_useless(out):
            continue
        if result.get("ok"):
            return True
        # Soft success: useful stdout before a later failing `&&` link.
        err = (result.get("error") or result.get("stderr") or "").strip()
        if re.search(r"(?i)command not found|not found", err) or re.search(
            r"(?i)^(Python|Node|v?\d|ruby|java|go |ProductName|System Version)",
            out,
            re.M,
        ):
            return True
    return False


def _latest_useful_bash_stdout(history: list[dict[str, Any]]) -> str:
    for item in reversed(history or []):
        call = item.get("call") or {}
        result = item.get("result") or {}
        if call.get("tool") != "run_bash":
            continue
        out = (result.get("stdout") or "").strip()
        if out and not _bash_stdout_is_useless(out):
            return out[:2000]
    return ""


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in re.split(r"[^\d]+", (version or "").strip()):
        if piece.isdigit():
            parts.append(int(piece))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def _extract_installed_software_version(local_out: str, utterance: str) -> Optional[str]:
    """Parse installed version from `python3 --version` / similar stdout."""
    out = local_out or ""
    if re.search(r"(?i)\bpython\b", utterance or ""):
        m = re.search(r"(?i)\bPython\s+(\d+\.\d+(?:\.\d+)?)\b", out)
        if m:
            return m.group(1)
    if re.search(r"(?i)\bnode\b", utterance or ""):
        m = re.search(r"(?i)\bv?(\d+\.\d+(?:\.\d+)?)\b", out)
        if m:
            return m.group(1)
    m = re.search(r"(?i)\b(?:version|v)?\s*(\d+\.\d+(?:\.\d+)?)\b", out)
    return m.group(1) if m else None


def _extract_latest_software_version(web_ctx: str, utterance: str) -> Optional[str]:
    """Best-effort latest version from search context (prefer explicit 'latest')."""
    ctx = web_ctx or ""
    name = "Python"
    if re.search(r"(?i)\bnode(\.?js)?\b", utterance or ""):
        name = "Node"
    elif re.search(r"(?i)\bruby\b", utterance or ""):
        name = "Ruby"
    elif re.search(r"(?i)\bjava\b", utterance or ""):
        name = "Java"

    preferred: list[str] = []
    # "Latest Python 3 Release - Python 3.14.6"
    for m in re.finditer(
        rf"(?i)latest[^\n]{{0,80}}{name}[^\n]{{0,40}}?"
        rf"(\d+\.\d+(?:\.\d+)?)",
        ctx,
    ):
        preferred.append(m.group(1))
    for m in re.finditer(
        rf"(?i){name}\s+(\d+\.\d+\.\d+)\s+is\s+now\s+available",
        ctx,
    ):
        preferred.append(m.group(1))
    if preferred:
        return max(preferred, key=_version_tuple)

    found: list[str] = []
    for m in re.finditer(rf"(?i)\b{name}\s+(\d+\.\d+\.\d+)\b", ctx):
        found.append(m.group(1))
    # Prefer versions near python.org download language.
    if not found:
        for m in re.finditer(r"(?i)\b(\d+\.\d+\.\d+)\b", ctx):
            found.append(m.group(1))
    if not found:
        return None
    # Drop ancient majors when talking about Python 3.
    if name.lower() == "python":
        found = [v for v in found if _version_tuple(v)[0] >= 3]
    return max(found, key=_version_tuple) if found else None


def _deterministic_hybrid_version_answer(
    utterance: str, local_out: str, web_ctx: str
) -> Optional[str]:
    """Rule-based latest-vs-installed answer — no LLM arithmetic."""
    installed = _extract_installed_software_version(local_out, utterance)
    latest = _extract_latest_software_version(web_ctx, utterance)
    if not installed or not latest:
        return None
    label = "Python"
    if re.search(r"(?i)\bnode\b", utterance or ""):
        label = "Node.js"
    elif re.search(r"(?i)\bruby\b", utterance or ""):
        label = "Ruby"
    elif re.search(r"(?i)\bjava\b", utterance or ""):
        label = "Java"

    inst_t = _version_tuple(installed)
    late_t = _version_tuple(latest)
    if inst_t < late_t:
        verdict = (
            f"You are out of date: this Mac has {installed}, "
            f"which is behind the latest stable {latest}."
        )
    elif inst_t > late_t:
        verdict = (
            f"This Mac has {installed}, which is newer than the latest stable "
            f"release I found ({latest})."
        )
    else:
        verdict = (
            f"You are up to date: this Mac already has the latest stable "
            f"release ({installed})."
        )
    return (
        f"Latest stable {label}: {latest}\n"
        f"Installed on this Mac: {installed}\n\n"
        f"{verdict}"
    )


def _suggested_cmds_from_history(history: list[dict[str, Any]]) -> list[str]:
    for item in reversed(history or []):
        call = item.get("call") or {}
        result = item.get("result") or {}
        tool = call.get("tool")
        blob = ""
        if tool == "respond":
            blob = str(
                result.get("text")
                or (call.get("args") or {}).get("text")
                or ""
            )
        elif tool == "_goal_check":
            # Prefer the critic's next_hint (often the exact mkdir to run).
            hint = _command_from_next_hint(str(result.get("next_hint") or ""))
            if hint:
                return [hint]
            blob = str(result.get("candidate") or "")
        if blob:
            cmds = _extract_suggested_shell_commands(blob)
            if cmds:
                return cmds
    return []


def _history_has_ui_action(history: list[dict[str, Any]]) -> bool:
    return any(
        (h.get("call") or {}).get("tool")
        in {"ui_type", "ui_click", "ui_key", "ui_menu"}
        and (h.get("result") or {}).get("ok")
        for h in history
    )


def _history_has_ui_snapshot(history: list[dict[str, Any]]) -> bool:
    return any(
        (h.get("call") or {}).get("tool") == "ui_snapshot"
        and (h.get("result") or {}).get("ok")
        for h in history
    )


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
    for item in reversed(history):
        call = item.get("call") or {}
        result = item.get("result") or {}
        tool = call.get("tool")
        if tool == "run_bash" and result.get("ok"):
            cmd = str((call.get("args") or {}).get("command") or "")
            out = str(result.get("stdout") or "")
            if _is_discovery_bash(cmd) and "download" in cmd.lower():
                p = _path_from_ls_stdout(out)
                if p:
                    return p
            # echo Opened: /path  or bare absolute path line
            m = re.search(r"(?i)(?:opened|deleted|moved|copied):\s*(.+)$", out, re.M)
            if m:
                return m.group(1).strip()
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("/") and " " not in line:
                    return line
    return None


def _forced_followup(
    utterance: str, history: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Deterministic next tool after discovery when the user goal is clear."""
    if not history:
        return None

    # Q&A: prior page/search was thin — next unread page first, else new query.
    if _history_needs_more_search(history) and _web_search_count(history) < _MAX_WEB_SEARCHES:
        unread, pages_read, last_q = _last_search_unread(history)
        if unread and pages_read < 3:
            return {
                "tool": "web_search",
                "args": {
                    "query": last_q or utterance,
                    "unread_urls": unread,
                    "pages_already_read": pages_read,
                },
            }
        query = _next_search_query(utterance, history)
        if query:
            return {"tool": "web_search", "args": {"query": query}}

    if _history_has_successful_mutation(history):
        return None
    text = utterance or ""

    # Misrouted web research / intermediate advice on a Mac action → run shell.
    if _is_action_request(text) or _needs_local_machine_check(text):
        last_tool = ((history[-1].get("call") or {}).get("tool") or "")
        ran_bash = _history_has_successful_bash(history)
        # Critic / planner already named the exact next command — run it even
        # after a wasted discovery ls (e.g. Downloads).
        suggested = _suggested_cmds_from_history(history)
        if suggested:
            cmd0 = suggested[0]
            already = any(
                str(((h.get("call") or {}).get("args") or {}).get("command") or "").strip()
                == cmd0.strip()
                for h in history
                if (h.get("call") or {}).get("tool") == "run_bash"
            )
            if not already and not _bash_command_looks_broken(cmd0):
                return {"tool": "run_bash", "args": {"command": cmd0}}
        # Local version known, but user also asked to search the web for "latest".
        if (
            ran_bash
            and _utterance_wants_web_search(text)
            and _web_search_count(history) == 0
            and last_tool in {"run_bash", "_goal_check", "respond"}
        ):
            return {
                "tool": "web_search",
                "args": {"query": _hybrid_web_search_query(text)},
            }
        # Prefer executing the command the model already suggested in prose.
        if not ran_bash and last_tool in {
            "web_search",
            "respond",
            "_goal_check",
            "run_bash",
        }:
            # After a failed curl||echo bash, still need the real local check.
            if last_tool == "run_bash" and _needs_local_machine_check(text):
                local_cmd = _preferred_local_check_command(text)
                if local_cmd and not _history_has_command_containing(history, local_cmd):
                    return {"tool": "run_bash", "args": {"command": local_cmd}}
            local_cmd = _preferred_local_check_command(text)
            if local_cmd:
                return {"tool": "run_bash", "args": {"command": local_cmd}}
            if last_tool == "web_search" or _needs_local_machine_check(text):
                return {"tool": "__write_and_run_bash__", "args": {}}
            if last_tool in {"respond", "_goal_check"} and _candidate_is_shell_advice(
                str(
                    (history[-1].get("result") or {}).get("text")
                    or (history[-1].get("result") or {}).get("candidate")
                    or ""
                )
            ):
                return {"tool": "__write_and_run_bash__", "args": {}}
        # Discovery-only so far on a create/mutate ask → force the real command.
        if ran_bash and last_tool in {"run_bash", "_goal_check"}:
            only_discovery = True
            for item in history:
                call = item.get("call") or {}
                if call.get("tool") != "run_bash":
                    continue
                cmd = str((call.get("args") or {}).get("command") or "")
                if not _is_discovery_bash(cmd):
                    only_discovery = False
                    break
            if only_discovery:
                try:
                    from llm.inference import _deterministic_create_command

                    create_cmd = _deterministic_create_command(text)
                except Exception:  # noqa: BLE001
                    create_cmd = ""
                if create_cmd:
                    return {"tool": "run_bash", "args": {"command": create_cmd}}
                return {"tool": "__write_and_run_bash__", "args": {}}

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
        # Only open a path we already discovered — never invent "latest download".
        path = _first_path_from_history(history)
        if path:
            q = shlex.quote(path)
            return {
                "tool": "run_bash",
                "args": {
                    "command": f"open {q} && echo Opened: {q}",
                },
            }
        return None

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
        and not (h.get("result") or {}).get("continued")
    )


def _last_search_unread(
    history: list[dict[str, Any]],
) -> tuple[list[str], int, str]:
    """Return (unread_urls, pages_read, query) from the latest search chain."""
    unread: list[str] = []
    pages_read = 0
    query = ""
    for item in reversed(history or []):
        call = item.get("call") or {}
        result = item.get("result") or {}
        if call.get("tool") != "web_search":
            if call.get("tool") in {"_search_retry", "_goal_check", "respond"}:
                continue
            break
        if not query:
            query = str((call.get("args") or {}).get("query") or "").strip()
        # Prefer the newest unread list / page count in the chain.
        if not unread:
            raw = result.get("unread_urls") or (call.get("args") or {}).get(
                "unread_urls"
            )
            if isinstance(raw, list):
                unread = [str(u).strip() for u in raw if str(u).strip()]
        pr = result.get("pages_read")
        if pr is not None:
            try:
                pages_read = max(pages_read, int(pr))
            except (TypeError, ValueError):
                pass
        # Stop after the first non-continued search root if we have data.
        if not result.get("continued"):
            break
    return unread, pages_read, query


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
    # Critic only needs recent steps + a count of earlier ones.
    lines: list[str] = []
    if len(history) > 4:
        lines.append(f"(earlier {len(history) - 3} steps omitted)")
        slice_ = history[-3:]
    else:
        slice_ = history[-6:]
    for i, item in enumerate(slice_, 1):
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
    # Keep open+type+read as one multi-step GUI goal (don't split into fake opens).
    if _needs_gui_control(text):
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
    # "open Calculator and type …" is GUI follow-up, not two apps.
    if _needs_gui_control(cleaned) or re.search(
        r"(?i)\b(type|click|press|tell|delete|move|remove)\b", right
    ):
        return None
    if re.search(r"(?i)\b(type|click|press|tell|delete|move|remove)\b", left):
        return None
    return [f"open {left}", f"open {right}"]



def _is_open_action(utterance: str) -> bool:
    """True only when the user asks to open/launch/reveal something on the Mac."""
    text = (utterance or "").strip()
    if not text:
        return False
    cleaned = _scrub_open_compounds(text)
    return bool(_OPEN_RE.search(cleaned))


def _is_factual_lookup(utterance: str) -> bool:
    """True when the answer needs live/world knowledge, not local invention."""
    text = (utterance or "").strip()
    if not text or not _is_info_question(text):
        return False
    # Explaining pasted notes / definitions — local model can use the paste.
    if re.search(r"(?i)\b(explain|define|describe)\b.+\b(these|this|above|following)\b", text):
        return False
    if len(text) > 280 and re.search(r"(?i)\b(explain|define|describe)\b", text):
        return False
    return bool(
        re.search(
            r"(?i)\b("
            r"how many|how much|how (big|large|old|tall|wide)|"
            r"who (made|created|developed|founded|owns|wrote|invented)|"
            r"when (was|were|did|is|are)|"
            r"where (is|was|are)|"
            r"parameter|params?|billion|million|"
            r"gpt-?\s*\d|chatgpt|llama|claude|gemini|qwen|mistral|"
            r"population|capital of|released|founded|"
            r"current (time|price|weather|score|news)|"
            r"latest|who won|what time"
            r")\b",
            text,
        )
    )


def _is_info_question(utterance: str) -> bool:
    """True for factual / conversational questions that should not mutate the Mac."""
    text = (utterance or "").strip()
    if not text:
        return False
    if _CHAT_RE.match(text) or _META_RE.search(text):
        return False
    # Explicit Mac toggles are not "info questions".
    if _control_mac_heuristic(text) or _close_app_heuristic(text):
        return False
    if re.search(
        r"(?i)^\s*(what|whats|what's|why|who|when|where|which|whose|whom|"
        r"how\s+(much|many|long|far|old|come)|"
        r"is\s+there|are\s+there|tell\s+me|explain|define|"
        r"what\s+time|whats?\s+time|current\s+time)\b",
        text,
    ):
        return True
    if text.endswith("?") and not _is_action_request(text):
        return True
    return False


def _is_action_request(utterance: str) -> bool:
    """True when the user wants something done on the Mac, not just answered."""
    text = (utterance or "").strip()
    if not text:
        return False
    if _META_RE.search(text):
        return False
    cleaned = _scrub_open_compounds(text)
    if _needs_gui_control(text) or _needs_local_machine_check(text):
        return True
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

"""Tool registry for the MacAgent tool-calling loop."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from memory.sqlite import ContextMemory
from memory.user_context import (
    context_payload,
    load_user_notes,
    save_user_notes,
)
from tools.duckduckgo import build_grounded_context
from tools.run_code import run_python

logger = logging.getLogger(__name__)

_HOME = Path.home()
_MAX_FIND = 15

# Whitelisted System Settings / Preferences panes (open only — no toggles).
_SYSTEM_SETTINGS_PANES: dict[str, str] = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.universalaccess",
    "privacy": "x-apple.systempreferences:com.apple.preference.security?Privacy",
    "security": "x-apple.systempreferences:com.apple.preference.security",
    "network": "x-apple.systempreferences:com.apple.preference.network",
    "wifi": "x-apple.systempreferences:com.apple.preference.network",
    "bluetooth": "x-apple.systempreferences:com.apple.preference.bluetooth",
    "displays": "x-apple.systempreferences:com.apple.preference.displays",
    "sound": "x-apple.systempreferences:com.apple.preference.sound",
    "keyboard": "x-apple.systempreferences:com.apple.preference.keyboard",
    "trackpad": "x-apple.systempreferences:com.apple.preference.trackpad",
    "mouse": "x-apple.systempreferences:com.apple.preference.mouse",
    "battery": "x-apple.systempreferences:com.apple.preference.battery",
    "general": "x-apple.systempreferences:com.apple.preference.general",
    "notifications": "x-apple.systempreferences:com.apple.preference.notifications",
    "siri": "x-apple.systempreferences:com.apple.preference.speech",
    "spotlight": "x-apple.systempreferences:com.apple.preference.spotlight",
    "users": "x-apple.systempreferences:com.apple.preferences.users",
    "date": "x-apple.systempreferences:com.apple.preference.datetime",
    "time": "x-apple.systempreferences:com.apple.preference.datetime",
}


TOOL_CATALOG = """
Available tools (reply with ONE JSON object: {"tool":"...","args":{...}}):
- find_files: {"query":"invoice pdf","limit":10} — Spotlight search under user home
- get_user_context: {} — read MacAgent user notes / runtime context
- update_user_context: {"notes":"..."} — replace user notes (facts/preferences)
- list_apps: {} — list known app aliases
- open_app: {"name":"Safari"} — launch a native app or alias
- list_sites: {} — list purpose sites
- open_url: {"url":"https://..."} — open URL in Chrome (only when user wants to open)
- web_search: {"query":"..."} — DuckDuckGo search + page extract; does NOT open browser
- open_system_settings: {"pane":"wifi|bluetooth|accessibility|..."} — open a Settings pane
- run_python: {"code":"print(2+4)"} — write & run short Python; use for math, transforms, quick scripts
- respond: {"text":"..."} — final user-visible answer (always end with this)
""".strip()


class ToolRegistry:
    def __init__(self, memory: Optional[ContextMemory] = None, router=None):
        self.memory = memory or ContextMemory()
        self.router = router
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "find_files": self._find_files,
            "get_user_context": self._get_user_context,
            "update_user_context": self._update_user_context,
            "list_apps": self._list_apps,
            "open_app": self._open_app,
            "list_sites": self._list_sites,
            "open_url": self._open_url,
            "web_search": self._web_search,
            "open_system_settings": self._open_system_settings,
            "run_python": self._run_python,
            "respond": self._respond,
        }

    def names(self) -> list[str]:
        return sorted(self._handlers.keys())

    def run(self, tool: str, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        name = (tool or "").strip()
        payload = args if isinstance(args, dict) else {}
        handler = self._handlers.get(name)
        if handler is None:
            return {"ok": False, "error": f"unknown tool: {name}"}
        try:
            result = handler(payload)
            if not isinstance(result, dict):
                return {"ok": True, "result": result}
            if "ok" not in result:
                result = {"ok": True, **result}
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool %s failed", name)
            return {"ok": False, "error": str(exc)}

    def _find_files(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query required"}
        limit = min(int(args.get("limit") or 10), _MAX_FIND)
        paths = self._mdfind(query, limit=limit)
        if not paths:
            paths = self._fallback_find(query, limit=limit)
        return {"ok": True, "count": len(paths), "paths": paths}

    def _mdfind(self, query: str, limit: int) -> list[str]:
        # Prefer Spotlight scoped to home.
        cmd = [
            "mdfind",
            "-onlyin",
            str(_HOME),
            query,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("mdfind failed: %s", exc)
            return []
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        # Filter out Library caches / junk lightly
        cleaned = []
        for p in lines:
            lower = p.lower()
            if "/library/caches/" in lower or "/.trash/" in lower:
                continue
            cleaned.append(p)
            if len(cleaned) >= limit:
                break
        return cleaned

    def _fallback_find(self, query: str, limit: int) -> list[str]:
        # Limited find under home for name fragments (no System).
        safe = "".join(c for c in query if c.isalnum() or c in " ._-+")[:80].strip()
        if not safe:
            return []
        cmd = [
            "find",
            str(_HOME),
            "-maxdepth",
            "5",
            "-iname",
            f"*{safe}*",
            "-type",
            "f",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        paths = []
        for ln in (proc.stdout or "").splitlines():
            p = ln.strip()
            if not p:
                continue
            lower = p.lower()
            if "/library/" in lower or "/.git/" in lower:
                continue
            paths.append(p)
            if len(paths) >= limit:
                break
        return paths

    def _get_user_context(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **context_payload()}

    def _update_user_context(self, args: dict[str, Any]) -> dict[str, Any]:
        notes = args.get("notes")
        if notes is None:
            return {"ok": False, "error": "notes required"}
        saved = save_user_notes(str(notes))
        return {"ok": True, "notes": saved, "chars": len(saved)}

    def _list_apps(self, _args: dict[str, Any]) -> dict[str, Any]:
        apps = self.memory.list_app_aliases()
        return {"ok": True, "apps": apps[:40]}

    def _open_app(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or args.get("app") or args.get("target") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        if self.router is not None:
            msg = self.router.execute(
                {"action": "open_app", "target": name, "raw_query": f"open {name}"}
            )
            return {"ok": True, "message": msg}
        subprocess.run(["open", "-a", name], check=False)
        return {"ok": True, "message": f"Launched {name}"}

    def _list_sites(self, _args: dict[str, Any]) -> dict[str, Any]:
        sites = self.memory.list_purpose_sites()
        return {"ok": True, "sites": sites[:40]}

    def _open_url(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or args.get("target") or "").strip()
        if not url:
            return {"ok": False, "error": "url required"}
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if self.router is not None:
            msg = self.router.execute(
                {"action": "open_site", "target": url, "raw_query": f"open {url}"}
            )
            return {"ok": True, "message": msg, "url": url}
        subprocess.run(["open", "-a", "Google Chrome", url], check=False)
        return {"ok": True, "message": f"Opened {url}", "url": url}

    def _web_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query required"}
        context, sources = build_grounded_context(
            query, max_results=4, pages_to_read=2, max_chars_per_page=2400
        )
        return {
            "ok": bool(context.strip()),
            "query": query,
            "context": (context or "")[:6000],
            "sources": sources[:5],
        }

    def _open_system_settings(self, args: dict[str, Any]) -> dict[str, Any]:
        pane = str(args.get("pane") or args.get("name") or "").strip().lower()
        if not pane:
            return {
                "ok": False,
                "error": "pane required",
                "allowed": sorted(_SYSTEM_SETTINGS_PANES.keys()),
            }
        key = pane.replace(" ", "").replace("_", "")
        # normalize wifi / wi-fi
        aliases = {
            "wi-fi": "wifi",
            "wifisettings": "wifi",
            "systemsettings": "general",
            "preferences": "general",
        }
        key = aliases.get(key, key)
        url = _SYSTEM_SETTINGS_PANES.get(key) or _SYSTEM_SETTINGS_PANES.get(pane)
        if not url:
            # fuzzy contains
            for k, v in _SYSTEM_SETTINGS_PANES.items():
                if k in pane or pane in k:
                    url = v
                    key = k
                    break
        if not url:
            return {
                "ok": False,
                "error": f"pane not whitelisted: {pane}",
                "allowed": sorted(_SYSTEM_SETTINGS_PANES.keys()),
            }
        subprocess.run(["open", url], check=False)
        return {"ok": True, "pane": key, "opened": url}

    def _run_python(self, args: dict[str, Any]) -> dict[str, Any]:
        code = str(args.get("code") or args.get("source") or args.get("script") or "")
        return run_python(code)

    def _respond(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or args.get("message") or "").strip()
        return {"ok": True, "text": text, "final": True}

"""Tool registry for the MacAgent tool-calling loop."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from memory.sqlite import ContextMemory
from memory.user_context import (
    context_payload,
    save_user_notes,
)
from tools.duckduckgo import build_grounded_context
from tools.mac_alerts import trigger_native_notification_from_args
from tools.mac_diagnostics import manage_system_resources_from_args
from tools.mac_system import (
    control_mac_from_args,
    control_power_management_from_args,
    modify_system_setting_from_args,
)
from tools.run_bash import run_bash
from tools.run_code import run_python
from automation import ui_control

logger = logging.getLogger(__name__)

_HOME = Path.home()

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
Tools (ONE JSON: {"tool":"...","args":{...}}):
- respond {"text"} — Q&A / done
- web_search {"query"} — facts / live data; scrapes one page first, more only if thin
- open_app {"name"} / open_url {"url"} / open_folder {"query"} / open_system_settings {"pane"} — only when user asked to open
- control_mac {"feature":"wifi|bluetooth|volume|appearance","state":"on|off|toggle|mute|unmute|dark|light"}
- manage_system_resources {"action":"kill","target_process"} or {"action":"list"}
- modify_system_setting — defaults prefs only (prefer control_mac for wifi/dark)
- control_power_management — pmset sleep/display/battery ONLY (never quit apps / wifi)
- trigger_native_notification {"title","message"}
- run_bash {"command"} — ls/find(-maxdepth)/mv/rm/open; no mdfind
- run_python {"code"} — print(...) for calculations only
- ui_snapshot {"limit":40} — read on-screen UI (Accessibility)
- ui_click {"name":"5","role":"button"} — click a control by name
- ui_type {"text":"154*8"} — type into the focused app
- ui_key {"key":"return"} — press a key (return, escape, …)
- ui_menu {"app":"…","menu_path":"…"} — choose a menu item
- get_user_context / update_user_context / search_past_interactions
Wifi/bt/mute/dark → control_mac. Quit app → manage_system_resources kill.
Open+type/click/read screen → open_app then ui_type/ui_click/ui_key then ui_snapshot; do not stop after launch.
Compound asks → one tool per step. After find/ls, act if they asked open/delete/move.
Never invent shut down / restart / empty trash / rm unless clearly asked.
""".strip()


class ToolRegistry:
    def __init__(self, memory: Optional[ContextMemory] = None, router=None):
        self.memory = memory or ContextMemory()
        self.router = router
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "open_folder": self._open_folder,
            "get_user_context": self._get_user_context,
            "update_user_context": self._update_user_context,
            "search_past_interactions": self._search_past_interactions,
            "open_app": self._open_app,
            "open_url": self._open_url,
            "web_search": self._web_search,
            "open_system_settings": self._open_system_settings,
            "control_mac": self._control_mac,
            "modify_system_setting": self._modify_system_setting,
            "control_power_management": self._control_power_management,
            "manage_system_resources": self._manage_system_resources,
            "trigger_native_notification": self._trigger_native_notification,
            "run_python": self._run_python,
            "run_bash": self._run_bash,
            "ui_snapshot": self._ui_snapshot,
            "ui_click": self._ui_click,
            "ui_type": self._ui_type,
            "ui_key": self._ui_key,
            "ui_menu": self._ui_menu,
            "respond": self._respond,
        }

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

    def _open_folder(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(
            args.get("query") or args.get("name") or args.get("path") or ""
        ).strip()
        if not query:
            return {"ok": False, "error": "query required"}

        direct = Path(query).expanduser()
        if direct.is_dir():
            subprocess.run(["open", str(direct)], check=False)
            return {
                "ok": True,
                "path": str(direct),
                "message": f"Opened folder {direct}",
            }

        folders = self._fallback_find_dirs(query, limit=8)
        if not folders:
            return {"ok": False, "error": f"no folder matching {query!r}", "paths": []}

        exact = [p for p in folders if Path(p).name.lower() == query.lower()]
        chosen = sorted(exact or folders, key=lambda p: (len(p), p.lower()))[0]
        subprocess.run(["open", chosen], check=False)
        return {
            "ok": True,
            "path": chosen,
            "message": f"Opened folder {chosen}",
            "candidates": folders[:5],
        }

    def _fallback_find_dirs(self, query: str, limit: int) -> list[str]:
        safe = "".join(c for c in query if c.isalnum() or c in " ._-+")[:80].strip()
        if not safe:
            return []
        # Search common locations only — full $HOME find can exceed shell timeout.
        roots = [
            _HOME / "Desktop",
            _HOME / "Documents",
            _HOME / "Downloads",
            _HOME / "Projects",
            _HOME,
        ]
        seen: set[str] = set()
        out: list[str] = []
        for root in roots:
            if not root.is_dir():
                continue
            cmd = [
                "find",
                str(root),
                "-maxdepth",
                "8",
                "-iname",
                f"*{safe}*",
                "-type",
                "d",
            ]
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=8, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            for ln in (proc.stdout or "").splitlines():
                p = ln.strip()
                if not p or p in seen or not Path(p).is_dir():
                    continue
                lower = p.lower()
                if "/library/caches/" in lower or "/.trash/" in lower:
                    continue
                seen.add(p)
                out.append(p)
                if len(out) >= limit:
                    return out
        return out

    def _get_user_context(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, **context_payload()}

    def _update_user_context(self, args: dict[str, Any]) -> dict[str, Any]:
        notes = args.get("notes")
        if notes is None:
            return {"ok": False, "error": "notes required"}
        saved = save_user_notes(str(notes))
        return {"ok": True, "notes": saved, "chars": len(saved)}

    def _search_past_interactions(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or args.get("q") or "").strip()
        limit = int(args.get("limit") or 5)
        if query:
            items = self.memory.search_interactions(query, limit=limit)
        else:
            items = self.memory.recent_interactions(limit=limit)
        return {"ok": True, "items": items, "count": len(items)}

    def _open_app(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or args.get("app") or args.get("target") or "").strip()
        if not name:
            return {"ok": False, "error": "name required"}
        if self.router is not None:
            msg = self.router.execute(
                {"action": "open_app", "target": name, "raw_query": f"open {name}"}
            )
            if isinstance(msg, str) and msg.startswith("__NOT_FOUND__:"):
                missing = msg.split(":", 1)[-1]
                return {
                    "ok": False,
                    "not_found": True,
                    "name": missing,
                    "message": (
                        f"I couldn't find an app named “{missing}” on this Mac. "
                        "Which app did you mean, or try another name?"
                    ),
                }
            if isinstance(msg, str) and msg.startswith("Failed to open"):
                return {"ok": False, "error": msg}
            return {"ok": True, "message": msg}
        result = subprocess.run(
            ["open", "-a", name], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "not_found": True,
                "name": name,
                "message": (
                    f"I couldn't find an app named “{name}” on this Mac. "
                    "Which app did you mean, or try another name?"
                ),
            }
        return {"ok": True, "message": f"Launched {name}"}

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
            if not msg or (isinstance(msg, str) and msg.startswith("Failed")):
                return {
                    "ok": False,
                    "error": msg or "Failed to open URL in Chrome",
                    "url": url,
                }
            return {"ok": True, "message": msg, "url": url}
        result = subprocess.run(
            ["open", "-a", "Google Chrome", url],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "error": (result.stderr or "Chrome open failed").strip(),
                "url": url,
            }
        return {"ok": True, "message": f"Opened {url}", "url": url}

    def _web_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        unread_raw = args.get("unread_urls")
        unread_urls: list[str] = []
        if isinstance(unread_raw, list):
            unread_urls = [str(u).strip() for u in unread_raw if str(u).strip()]
        pages_already = int(args.get("pages_already_read") or 0)
        # Always scrape one page at a time; more pages only on explicit continue.
        pages_to_read = 1
        if not query and not unread_urls:
            return {"ok": False, "error": "query required"}
        packed = build_grounded_context(
            query or "(continue)",
            max_results=5,
            pages_to_read=pages_to_read,
            max_chars_per_page=2400,
            unread_urls=unread_urls or None,
            pages_already_read=pages_already,
        )
        context = str(packed.get("context") or "")
        sources = list(packed.get("sources") or [])
        still_unread = list(packed.get("unread_urls") or [])
        pages_read = int(packed.get("pages_read") or 0)
        scraped = bool(packed.get("scraped"))
        return {
            "ok": bool(context.strip()),
            "query": query,
            "context": context[:6000],
            "sources": sources[:5],
            "unread_urls": still_unread[:8],
            "pages_read": pages_read,
            "scraped": scraped,
            "continued": bool(packed.get("continued")),
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

    def _control_mac(self, args: dict[str, Any]) -> dict[str, Any]:
        return control_mac_from_args(args)

    def _modify_system_setting(self, args: dict[str, Any]) -> dict[str, Any]:
        return modify_system_setting_from_args(args)

    def _control_power_management(self, args: dict[str, Any]) -> dict[str, Any]:
        return control_power_management_from_args(args)

    def _manage_system_resources(self, args: dict[str, Any]) -> dict[str, Any]:
        return manage_system_resources_from_args(args)

    def _trigger_native_notification(self, args: dict[str, Any]) -> dict[str, Any]:
        return trigger_native_notification_from_args(args)

    def _run_python(self, args: dict[str, Any]) -> dict[str, Any]:
        code = str(args.get("code") or args.get("source") or args.get("script") or "")
        return run_python(code)

    def _run_bash(self, args: dict[str, Any]) -> dict[str, Any]:
        cmd = str(args.get("command") or args.get("cmd") or args.get("bash") or "")
        confirmed = bool(args.get("confirmed") or args.get("approved"))
        return run_bash(cmd, confirmed=confirmed)

    def _ui_snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        return ui_control.ui_snapshot(max_elements=int(args.get("limit") or 40))

    def _ui_click(self, args: dict[str, Any]) -> dict[str, Any]:
        return ui_control.ui_click(
            name=str(args.get("name") or args.get("label") or ""),
            role=str(args.get("role") or "button"),
            index=int(args.get("index") or 1),
        )

    def _ui_type(self, args: dict[str, Any]) -> dict[str, Any]:
        return ui_control.ui_type(
            str(args.get("text") or args.get("string") or ""),
            app=str(args.get("app") or args.get("application") or ""),
        )

    def _ui_key(self, args: dict[str, Any]) -> dict[str, Any]:
        return ui_control.ui_key(
            key=str(args.get("key") or "return"),
            modifiers=str(args.get("modifiers") or ""),
        )

    def _ui_menu(self, args: dict[str, Any]) -> dict[str, Any]:
        return ui_control.ui_menu(
            app=str(args.get("app") or ""),
            menu_path=str(args.get("menu_path") or args.get("path") or ""),
        )

    def _respond(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or args.get("message") or "").strip()
        return {"ok": True, "text": text, "final": True}

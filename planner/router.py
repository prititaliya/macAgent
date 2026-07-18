import json
import logging
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Optional, Tuple

from AppKit import NSWorkspace
from Foundation import NSURL

from memory.sqlite import ContextMemory

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SETTINGS_PATH = _PROJECT_ROOT / "config" / "settings.json"


def _load_settings() -> dict:
    if _SETTINGS_PATH.exists():
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


class CoreRouter:
    def __init__(self, parser=None):
        self.memory = ContextMemory()
        self.parser = parser
        self.settings = _load_settings()

    def execute(self, intent: dict[str, Any]) -> str:
        """Run a command intent. Returns a short human-readable result for the HUD."""
        action = intent.get("action")
        target = (intent.get("target") or "").strip()
        raw_query = (intent.get("raw_query") or "").strip()
        utterance = raw_query or target

        logger.info("Routing action=%s target=%r raw_query=%r", action, target, raw_query)

        # 1) Keyword soft alias (sites + apps)
        soft = self._soft_resolve(target, raw_query)
        if soft:
            resolved, target_type, matched = soft
            logger.info(
                "Soft-matched alias=%r → %s (%s)", matched, resolved, target_type
            )
            if target_type == "url":
                self._open_url_in_browser(resolved)
                return self._record(
                    utterance,
                    "alias_site",
                    f"alias={matched}",
                    f"Opened {resolved}",
                )
            if target_type == "app":
                self._launch_native_app(resolved)
                return self._record(
                    utterance,
                    "alias_app",
                    f"alias={matched}",
                    f"Launched app {resolved}",
                )

        # 2) Purpose-site semantic match (local LLM)
        purpose_hit = self._resolve_purpose(utterance)
        if purpose_hit:
            url, purpose = purpose_hit
            self._open_url_in_browser(url)
            return self._record(
                utterance,
                "purpose_site",
                f"purpose={purpose}",
                f"Opened {url}",
            )

        # 3) Explicit open_app / open_site BEFORE history (history was stealing "code")
        if action == "open_app":
            result = self._handle_open_app(target, utterance, raw_query)
            if result:
                return result

        if action == "open_site":
            result = self._handle_open_site(target, utterance, raw_query)
            if result:
                return result

        # 4) Browser history soft match
        history_url = self.memory.resolve_history(utterance)
        if history_url:
            logger.info("History soft-match → %s", history_url)
            self._open_url_in_browser(history_url)
            return self._record(
                utterance,
                "history",
                "browser_history_cache",
                f"Opened {history_url}",
            )

        if action == "workflow":
            logger.warning("workflow action not implemented yet; falling back to search")
            return self._search_fallback(
                raw_query if raw_query else target, utterance
            )

        if action in {"browse", "search_fallback"}:
            return self._search_fallback(
                raw_query if raw_query else target, utterance
            )

        return self._search_fallback(raw_query if raw_query else target, utterance)

    def _handle_open_site(
        self, target: str, utterance: str, raw_query: str
    ) -> Optional[str]:
        resolved_url, target_type = self.memory.resolve_alias(target)
        if resolved_url and target_type == "url":
            self._open_url_in_browser(resolved_url)
            return self._record(
                utterance, "open_site", f"target={target}", f"Opened {resolved_url}"
            )
        if not target:
            return None
        url = (
            target
            if target.startswith(("http://", "https://"))
            else f"https://{target}"
        )
        self._open_url_in_browser(url)
        return self._record(
            utterance, "open_site", f"target={target}", f"Opened {url}"
        )

    def _handle_open_app(
        self, target: str, utterance: str, raw_query: str
    ) -> Optional[str]:
        # Alias lookup on target (e.g. "VS Code" → Visual Studio Code)
        resolved, rtype = self.memory.resolve_alias(target)
        if resolved and rtype == "app":
            self._launch_native_app(resolved)
            return self._record(
                utterance, "open_app", f"target={target}", f"Launched app {resolved}"
            )
        if resolved and rtype == "url":
            self._open_url_in_browser(resolved)
            return self._record(
                utterance,
                "open_app_as_site",
                f"target={target}",
                f"Opened {resolved}",
            )

        # Soft-match utterance for known app aliases
        soft = self.memory.resolve_from_utterance(utterance)
        if soft[0] and soft[1] == "app":
            self._launch_native_app(soft[0])
            return self._record(
                utterance,
                "open_app",
                f"alias={soft[2]}",
                f"Launched app {soft[0]}",
            )

        if not target:
            return None

        # Try launching the spoken name directly (macOS open -a)
        app_name = target
        # Common cleanups
        lower = target.lower().replace(".", " ")
        if "visual" in lower and "code" in lower:
            app_name = "Visual Studio Code"
        elif "vs code" in lower or lower in {"vscode", "vs-code", "code"}:
            app_name = "Visual Studio Code"

        self._launch_native_app(app_name)
        return self._record(
            utterance, "open_app", f"target={target}", f"Launched app {app_name}"
        )

    def _record(
        self, utterance: str, action: str, detail: str, result: str
    ) -> str:
        try:
            self.memory.log_activity(utterance, action, detail, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to write activity log: %s", exc)
        return result

    def _resolve_purpose(self, utterance: str) -> Optional[Tuple[str, str]]:
        if not utterance or self.parser is None:
            return None
        sites = self.memory.list_purpose_sites()
        if not sites:
            return None
        site_id = self.parser.match_purpose_site(utterance, sites)
        if site_id is None:
            return None
        site = self.memory.get_purpose_site(site_id)
        if not site:
            return None
        self.memory.bump_purpose_site(site_id)
        logger.info(
            "Purpose-matched id=%s purpose=%r → %s",
            site_id,
            site.get("purpose"),
            site.get("url"),
        )
        return site["url"], site.get("purpose") or ""

    def _soft_resolve(
        self, target: str, raw_query: str
    ) -> Optional[Tuple[str, str, str]]:
        for text in (target, raw_query):
            if not text:
                continue
            resolved, target_type, matched = self.memory.resolve_from_utterance(text)
            if resolved and target_type and matched:
                return resolved, target_type, matched
        return None

    def _search_fallback(self, query: str, utterance: str = "") -> str:
        encoded = urllib.parse.quote(query or "")
        fallback_url = f"https://www.google.com/search?q={encoded}"
        self._open_url_in_browser(fallback_url)
        return self._record(
            utterance or query,
            "search_fallback",
            f"query={query}",
            f"Opened {fallback_url}",
        )

    def _open_url_in_browser(self, url_str: str) -> None:
        if self.settings.get("default_browser") == "chrome":
            try:
                from automation.applescript import activate_chrome

                if activate_chrome(url_str):
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("activate_chrome failed: %s", exc)

        ns_url = NSURL.URLWithString_(url_str)
        if ns_url is None:
            logger.error("Invalid URL: %s", url_str)
            return
        opened = NSWorkspace.sharedWorkspace().openURL_(ns_url)
        if not opened:
            subprocess.run(["open", url_str], capture_output=True, check=False)

    def _launch_native_app(self, app_name: str) -> None:
        # Prefer `open -a` — more reliable for names like "Visual Studio Code"
        result = subprocess.run(
            ["open", "-a", app_name], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            workspace = NSWorkspace.sharedWorkspace()
            workspace.launchApplication_(app_name)
            logger.warning(
                "open -a %r failed (%s); tried NSWorkspace",
                app_name,
                (result.stderr or "").strip(),
            )

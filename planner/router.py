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

        # Explicit absolute URL → open in Chrome, skip soft-alias / purpose hijack.
        if action == "open_site" and target.startswith(("http://", "https://")):
            result = self._handle_open_site(target, utterance, raw_query)
            if result:
                return result
            return f"Failed to open {target} in Chrome"

        # 1) Keyword soft alias (sites + apps)
        soft = self._soft_resolve(target, raw_query)
        if soft:
            resolved, target_type, matched = soft
            logger.info(
                "Soft-matched alias=%r → %s (%s)", matched, resolved, target_type
            )
            if target_type == "url":
                ok = self._open_url_in_browser(resolved)
                if not ok:
                    return f"Failed to open {resolved} in Chrome"
                return self._record(
                    utterance,
                    "alias_site",
                    f"alias={matched}",
                    f"Opened {resolved}",
                )
            if target_type == "app":
                if not self._launch_native_app(resolved):
                    return f"__NOT_FOUND__:{resolved}"
                return self._record(
                    utterance,
                    "alias_app",
                    f"alias={matched}",
                    f"Launched app {resolved}",
                )

        # 2) Purpose-site semantic match (local LLM) — skip for explicit open_app
        if action != "open_app":
            purpose_hit = self._resolve_purpose(utterance)
            if purpose_hit:
                url, purpose = purpose_hit
                ok = self._open_url_in_browser(url)
                if not ok:
                    return f"Failed to open {url} in Chrome"
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
            return f"Failed to open {target} in Chrome"

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
        # Absolute URLs: open as-is (no soft-alias hijack).
        if target.startswith(("http://", "https://")):
            ok = self._open_url_in_browser(target)
            if not ok:
                return None
            return self._record(
                utterance, "open_site", f"target={target}", f"Opened {target}"
            )
        resolved_url, target_type = self.memory.resolve_alias(target)
        if resolved_url and target_type == "url":
            ok = self._open_url_in_browser(resolved_url)
            if not ok:
                return None
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
        ok = self._open_url_in_browser(url)
        if not ok:
            return None
        return self._record(
            utterance, "open_site", f"target={target}", f"Opened {url}"
        )

    def _handle_open_app(
        self, target: str, utterance: str, raw_query: str
    ) -> Optional[str]:
        # Alias lookup on target (e.g. "VS Code" → Visual Studio Code)
        resolved, rtype = self.memory.resolve_alias(target)
        if resolved and rtype == "app":
            if not self._launch_native_app(resolved):
                return f"__NOT_FOUND__:{resolved}"
            return self._record(
                utterance, "open_app", f"target={target}", f"Launched app {resolved}"
            )
        if resolved and rtype == "url":
            ok = self._open_url_in_browser(resolved)
            if not ok:
                return None
            return self._record(
                utterance,
                "open_app_as_site",
                f"target={target}",
                f"Opened {resolved}",
            )

        # Soft-match utterance for known app aliases
        soft = self.memory.resolve_from_utterance(utterance)
        if soft[0] and soft[1] == "app":
            if not self._launch_native_app(soft[0]):
                return f"__NOT_FOUND__:{soft[0]}"
            return self._record(
                utterance,
                "open_app",
                f"alias={soft[2]}",
                f"Launched app {soft[0]}",
            )

        if not target:
            return None

        app_name = target
        lower = target.lower().replace(".", " ")
        if "visual" in lower and "code" in lower:
            app_name = "Visual Studio Code"
        elif "vs code" in lower or lower in {"vscode", "vs-code", "code"}:
            app_name = "Visual Studio Code"

        if not self.app_exists(app_name) and not self.app_exists(target):
            # Still try once — name variants; otherwise report missing.
            if not self._launch_native_app(app_name):
                return f"__NOT_FOUND__:{app_name}"
        elif not self._launch_native_app(app_name):
            return f"__NOT_FOUND__:{app_name}"
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

    def _open_url_in_browser(self, url_str: str) -> bool:
        """Open URL; prefer Chrome when configured. Returns True on success."""
        prefer_chrome = self.settings.get("default_browser") == "chrome"
        if prefer_chrome:
            try:
                from automation.applescript import activate_chrome, chrome_installed

                if not chrome_installed():
                    logger.error("Google Chrome is not installed")
                    return False
                if activate_chrome(url_str):
                    return True
                logger.error("Failed to open URL in Chrome: %s", url_str)
                return False
            except Exception as exc:  # noqa: BLE001
                logger.error("Chrome open failed: %s", exc)
                return False

        ns_url = NSURL.URLWithString_(url_str)
        if ns_url is None:
            logger.error("Invalid URL: %s", url_str)
            return False
        opened = NSWorkspace.sharedWorkspace().openURL_(ns_url)
        if opened:
            return True
        result = subprocess.run(
            ["open", url_str], capture_output=True, text=True, check=False
        )
        return result.returncode == 0

    def app_exists(self, app_name: str) -> bool:
        """True if macOS can resolve the app name."""
        name = (app_name or "").strip()
        if not name:
            return False
        # open -a with dry check via mdfind / Applications
        result = subprocess.run(
            ["mdfind", f"kMDItemKind == 'Application' && kMDItemDisplayName == '{name}'c"],
            capture_output=True,
            text=True,
            check=False,
        )
        if (result.stdout or "").strip():
            return True
        from pathlib import Path

        for base in (Path("/Applications"), Path.home() / "Applications"):
            if (base / f"{name}.app").exists():
                return True
        # Last resort: try `open -a` with a non-existent file to see if app resolves
        # Actually use LS via osascript
        safe = name.replace('"', '\\"')
        probe = subprocess.run(
            [
                "osascript",
                "-e",
                f'exists application "{safe}"',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return (probe.stdout or "").strip().lower() == "true"

    def _launch_native_app(self, app_name: str) -> bool:
        """Launch app; return True if launch appears to succeed."""
        if not self.app_exists(app_name):
            # Still try open -a in case name differs slightly
            pass
        result = subprocess.run(
            ["open", "-a", app_name], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            return True
        workspace = NSWorkspace.sharedWorkspace()
        launched = bool(workspace.launchApplication_(app_name))
        if not launched:
            logger.warning(
                "Failed to launch %r (%s)",
                app_name,
                (result.stderr or "").strip(),
            )
        return launched

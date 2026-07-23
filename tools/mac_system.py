"""Native macOS system preference and power-management tools via defaults/pmset."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

_VALUE_TYPE_FLAGS: dict[str, str] = {
    "string": "-string",
    "int": "-int",
    "float": "-float",
    "bool": "-bool",
    "date": "-date",
}

_PMSET_SETTINGS = frozenset(
    {
        "sleep",
        "displaysleep",
        "disksleep",
        "hibernatemode",
        "lowpowermode",
        "powernap",
        "womp",
        "ttyskeepawake",
    }
)

_TIMEOUT = 15


class ModifySystemSettingArgs(BaseModel):
    """Arguments for mutating a macOS defaults domain key without opening System Settings."""

    domain: str = Field(
        ...,
        description=(
            "defaults domain, e.g. NSGlobalDomain, -g, or a bundle id like "
            "com.apple.dock"
        ),
    )
    key: str = Field(..., description="Preference key to write, e.g. AppleInterfaceStyle")
    value: str = Field(..., description="Value to write (always passed as a string to defaults)")
    value_type: Literal["string", "int", "float", "bool", "date"] = Field(
        ...,
        description="defaults type flag: string|int|float|bool|date",
    )

    @field_validator("domain", "key", "value")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("must be a non-empty string")
        return s


class ControlPowerManagementArgs(BaseModel):
    """Arguments for pmset power-management tweaks (sleep, display timeout, etc.)."""

    setting: str = Field(
        ...,
        description=(
            "pmset key: sleep|displaysleep|disksleep|hibernatemode|"
            "lowpowermode|powernap|womp|ttyskeepawake"
        ),
    )
    value: int = Field(..., description="Integer value for the pmset setting (minutes or mode)")

    @field_validator("setting")
    @classmethod
    def _allowed_setting(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if s not in _PMSET_SETTINGS:
            raise ValueError(
                f"unsupported setting '{v}'; allowed: {sorted(_PMSET_SETTINGS)}"
            )
        return s


def modify_system_setting(
    domain: str,
    key: str,
    value: str,
    value_type: str,
) -> dict[str, Any]:
    """Write a macOS preference via the native `defaults` CLI."""
    try:
        args = ModifySystemSettingArgs(
            domain=domain,
            key=key,
            value=value,
            value_type=value_type,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    flag = _VALUE_TYPE_FLAGS[args.value_type]
    # Normalize common aliases for the global domain.
    domain_arg = args.domain
    if domain_arg in {"-g", "NSGlobalDomain", "Apple Global Domain"}:
        domain_arg = "NSGlobalDomain"

    cmd = ["defaults", "write", domain_arg, args.key, flag, args.value]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("defaults write failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "domain": domain_arg,
            "key": args.key,
            "command": cmd,
        }

    ok = proc.returncode == 0
    out: dict[str, Any] = {
        "ok": ok,
        "domain": domain_arg,
        "key": args.key,
        "value": args.value,
        "value_type": args.value_type,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command": cmd,
    }
    if not ok:
        out["error"] = out["stderr"] or f"defaults write exited {proc.returncode}"
    return out


def control_power_management(setting: str, value: int) -> dict[str, Any]:
    """Adjust sleep / display / power modes via the native `pmset` binary."""
    try:
        args = ControlPowerManagementArgs(setting=setting, value=value)
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    cmd = ["pmset", args.setting, str(args.value)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("pmset failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "setting": args.setting,
            "value": args.value,
            "command": cmd,
        }

    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    ok = proc.returncode == 0
    out: dict[str, Any] = {
        "ok": ok,
        "setting": args.setting,
        "value": args.value,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "command": cmd,
    }
    if not ok:
        hint = ""
        lower = (stderr + " " + stdout).lower()
        if "permission" in lower or "not privileged" in lower or proc.returncode == 1:
            hint = (
                " (pmset often requires admin privileges; try running MacAgent "
                "with elevated rights or adjust via System Settings → Battery/Energy)"
            )
        out["error"] = (stderr or stdout or f"pmset exited {proc.returncode}") + hint
    return out


def modify_system_setting_from_args(payload: dict[str, Any]) -> dict[str, Any]:
    """Registry adapter: accept a free-form args dict."""
    return modify_system_setting(
        domain=str(payload.get("domain") or ""),
        key=str(payload.get("key") or ""),
        value=str(payload.get("value") if payload.get("value") is not None else ""),
        value_type=str(payload.get("value_type") or payload.get("type") or "string"),
    )


def control_power_management_from_args(payload: dict[str, Any]) -> dict[str, Any]:
    """Registry adapter: accept a free-form args dict."""
    raw_value = payload.get("value")
    try:
        value = int(raw_value) if raw_value is not None else 0
    except (TypeError, ValueError):
        return {"ok": False, "error": f"value must be an integer, got {raw_value!r}"}
    return control_power_management(
        setting=str(payload.get("setting") or payload.get("key") or ""),
        value=value,
    )


_CONTROL_FEATURES = frozenset({"wifi", "bluetooth", "volume", "appearance"})


class ControlMacArgs(BaseModel):
    """Toggle common Mac features without opening System Settings."""

    feature: str = Field(
        ...,
        description="wifi | bluetooth | volume | appearance",
    )
    state: str = Field(
        ...,
        description=(
            "on|off|toggle for wifi/bluetooth; mute|unmute|0-100 for volume; "
            "dark|light|toggle for appearance"
        ),
    )

    @field_validator("feature")
    @classmethod
    def _feature_ok(cls, v: str) -> str:
        s = (v or "").strip().lower()
        aliases = {
            "wi-fi": "wifi",
            "wi fi": "wifi",
            "airport": "wifi",
            "bt": "bluetooth",
            "sound": "volume",
            "mute": "volume",
            "theme": "appearance",
            "darkmode": "appearance",
            "dark mode": "appearance",
            "light mode": "appearance",
        }
        s = aliases.get(s, s)
        if s not in _CONTROL_FEATURES:
            raise ValueError(
                f"unsupported feature '{v}'; allowed: {sorted(_CONTROL_FEATURES)}"
            )
        return s

    @field_validator("state")
    @classmethod
    def _state_nonempty(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if not s:
            raise ValueError("state must be non-empty")
        return s


def _run(cmd: list[str], *, timeout: int = _TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _wifi_device() -> str:
    try:
        proc = _run(["networksetup", "-listallhardwareports"])
    except (OSError, subprocess.TimeoutExpired):
        return "en0"
    lines = (proc.stdout or "").splitlines()
    for i, line in enumerate(lines):
        if re.search(r"(?i)wi-?fi|airport", line):
            for j in range(i + 1, min(i + 4, len(lines))):
                m = re.match(r"(?i)device:\s*(\S+)", lines[j])
                if m:
                    return m.group(1)
    return "en0"


def _normalize_on_off(state: str) -> Optional[str]:
    s = (state or "").strip().lower()
    if s in {"on", "enable", "enabled", "true", "1"}:
        return "on"
    if s in {"off", "disable", "disabled", "false", "0"}:
        return "off"
    if s == "toggle":
        return "toggle"
    return None


def _set_wifi(state: str) -> dict[str, Any]:
    device = _wifi_device()
    power = state
    if power == "toggle":
        try:
            cur = _run(["networksetup", "-getairportpower", device])
            out = (cur.stdout or "") + (cur.stderr or "")
            power = "off" if re.search(r"(?i):\s*on\b", out) else "on"
        except (OSError, subprocess.TimeoutExpired):
            power = "off"
    cmd = ["networksetup", "-setairportpower", device, power]
    try:
        proc = _run(cmd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc), "feature": "wifi", "command": cmd}
    ok = proc.returncode == 0
    message = f"Wi‑Fi turned {power}."
    err = (proc.stderr or "").strip()
    if not ok:
        message = err or f"networksetup failed ({proc.returncode})"
        # Common on locked-down Macs — open the pane so the user can finish.
        try:
            subprocess.run(
                ["open", "x-apple.systempreferences:com.apple.preference.network"],
                check=False,
            )
            message += " Opened Network settings so you can toggle Wi‑Fi there."
        except OSError:
            pass
    return {
        "ok": ok,
        "feature": "wifi",
        "state": power,
        "device": device,
        "message": message,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": err,
        "command": cmd,
    }


def _blueutil_path() -> Optional[str]:
    for candidate in (
        shutil.which("blueutil"),
        "/opt/homebrew/bin/blueutil",
        "/usr/local/bin/blueutil",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _set_bluetooth(state: str) -> dict[str, Any]:
    power = state
    blueutil = _blueutil_path()
    if blueutil:
        if power == "toggle":
            try:
                cur = _run([blueutil, "--power"])
                out = (cur.stdout or "").strip()
                power = "off" if out.startswith("1") else "on"
            except (OSError, subprocess.TimeoutExpired):
                power = "off"
        want = "1" if power == "on" else "0"
        cmd = [blueutil, "--power", want]
        try:
            proc = _run(cmd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "error": str(exc),
                "feature": "bluetooth",
                "command": cmd,
            }
        ok = proc.returncode == 0
        return {
            "ok": ok,
            "feature": "bluetooth",
            "state": power,
            "message": f"Bluetooth turned {power}." if ok else (
                (proc.stderr or "").strip() or "blueutil failed"
            ),
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "command": cmd,
        }

    # No blueutil — open Bluetooth settings so the user can finish in one click.
    try:
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.bluetooth"],
            check=False,
        )
    except OSError as exc:
        return {"ok": False, "error": str(exc), "feature": "bluetooth"}
    want = "on" if power == "on" else ("off" if power == "off" else "toggle")
    return {
        "ok": True,
        "feature": "bluetooth",
        "state": want,
        "message": (
            f"Opened Bluetooth settings — tap to turn it {want}. "
            "(Install `brew install blueutil` for one-shot toggles.)"
        ),
        "opened_settings": True,
    }


def _set_volume(state: str) -> dict[str, Any]:
    s = (state or "").strip().lower()
    if s in {"mute", "off", "disable", "disabled"}:
        script = "set volume with output muted"
        label = "muted"
    elif s in {"unmute", "on", "enable", "enabled"}:
        script = "set volume without output muted"
        label = "unmuted"
    elif s.isdigit():
        level = max(0, min(100, int(s)))
        script = f"set volume output volume {level}"
        label = f"set to {level}%"
    else:
        return {
            "ok": False,
            "error": "volume state must be mute|unmute|0-100",
            "feature": "volume",
        }
    cmd = ["osascript", "-e", script]
    try:
        proc = _run(cmd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc), "feature": "volume", "command": cmd}
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "feature": "volume",
        "state": s,
        "message": f"Volume {label}." if ok else (
            (proc.stderr or "").strip() or "osascript volume failed"
        ),
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command": cmd,
    }


def _set_appearance(state: str) -> dict[str, Any]:
    s = (state or "").strip().lower()
    if s in {"toggle"}:
        # Read current via defaults, then flip.
        try:
            cur = _run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
            )
            is_dark = (cur.returncode == 0) and "dark" in (cur.stdout or "").lower()
        except (OSError, subprocess.TimeoutExpired):
            is_dark = False
        s = "light" if is_dark else "dark"
    if s in {"dark", "on", "enable", "enabled"}:
        script = (
            'tell application "System Events" to tell appearance preferences '
            "to set dark mode to true"
        )
        label = "dark"
    elif s in {"light", "off", "disable", "disabled"}:
        script = (
            'tell application "System Events" to tell appearance preferences '
            "to set dark mode to false"
        )
        label = "light"
    else:
        return {
            "ok": False,
            "error": "appearance state must be dark|light|toggle",
            "feature": "appearance",
        }
    cmd = ["osascript", "-e", script]
    try:
        proc = _run(cmd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc), "feature": "appearance", "command": cmd}
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "feature": "appearance",
        "state": label,
        "message": f"Appearance set to {label} mode." if ok else (
            (proc.stderr or "").strip()
            or "Could not change appearance (Accessibility may be required)."
        ),
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command": cmd,
    }


def control_mac(feature: str, state: str) -> dict[str, Any]:
    """Toggle Wi‑Fi, Bluetooth, volume, or appearance without inventing bash."""
    try:
        args = ControlMacArgs(feature=feature, state=state)
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    if args.feature == "wifi":
        power = _normalize_on_off(args.state)
        if power is None:
            return {"ok": False, "error": "wifi state must be on|off|toggle"}
        return _set_wifi(power)
    if args.feature == "bluetooth":
        power = _normalize_on_off(args.state)
        if power is None:
            return {"ok": False, "error": "bluetooth state must be on|off|toggle"}
        return _set_bluetooth(power)
    if args.feature == "volume":
        return _set_volume(args.state)
    if args.feature == "appearance":
        return _set_appearance(args.state)
    return {"ok": False, "error": f"unsupported feature: {args.feature}"}


def control_mac_from_args(payload: dict[str, Any]) -> dict[str, Any]:
    """Registry adapter: accept a free-form args dict."""
    feature = str(
        payload.get("feature")
        or payload.get("target")
        or payload.get("name")
        or ""
    )
    state = str(
        payload.get("state")
        or payload.get("value")
        or payload.get("action")
        or payload.get("power")
        or ""
    )
    # Tolerate planner variants: {"wifi_state":"off"}, {"wifi":"off"}, …
    if not feature or not state:
        for key, val in payload.items():
            k = str(key).lower()
            v = str(val).strip().lower() if val is not None else ""
            if not v:
                continue
            if k in {"wifi", "wi-fi", "bluetooth", "volume", "appearance"}:
                feature = feature or k.replace("wi-fi", "wifi")
                state = state or v
            elif k.endswith("_state") or k.endswith("_power") or k.endswith("_mode"):
                feature = feature or k.split("_", 1)[0]
                state = state or v
            elif k in {"mute", "unmute"} and not state:
                feature = feature or "volume"
                state = k
            elif k in {"dark", "light"} and not state:
                feature = feature or "appearance"
                state = k
    return control_mac(feature=feature, state=state)

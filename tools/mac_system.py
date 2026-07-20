"""Native macOS system preference and power-management tools via defaults/pmset."""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Literal

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

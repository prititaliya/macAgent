"""Diagnostic & process monitor tools using psutil."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal, Optional

import psutil
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

_TOP_N = 5
_TERMINATE_WAIT_S = 1.5


class ManageSystemResourcesArgs(BaseModel):
    """Arguments for listing top resource consumers or terminating a process tree."""

    action: Literal["list", "kill"] = Field(
        ...,
        description='Use "list" for top CPU/memory processes, or "kill" to terminate a process tree',
    )
    target_process: Optional[str] = Field(
        None,
        description=(
            'Required for action="kill": exact process name (case-insensitive) '
            "or numeric PID string"
        ),
    )

    @field_validator("target_process")
    @classmethod
    def _strip_target(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None


def manage_system_resources(
    action: str,
    target_process: Optional[str] = None,
) -> dict[str, Any]:
    """List top CPU/memory processes or safely kill a matching process tree."""
    try:
        args = ManageSystemResourcesArgs(
            action=action,  # type: ignore[arg-type]
            target_process=target_process,
        )
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    if args.action == "list":
        return _list_top_processes()
    return _kill_process_tree(args.target_process)


def _list_top_processes() -> dict[str, Any]:
    # Prime cpu_percent so the next sample is meaningful.
    procs: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc.cpu_percent(interval=None)
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    time.sleep(0.15)

    rows: list[dict[str, Any]] = []
    for proc in procs:
        try:
            with proc.oneshot():
                cpu = float(proc.cpu_percent(interval=None) or 0.0)
                mem = proc.memory_info()
                rss_mb = round(float(mem.rss) / (1024 * 1024), 1)
                name = proc.name() or ""
                pid = int(proc.pid)
            rows.append(
                {
                    "pid": pid,
                    "name": name,
                    "cpu_percent": round(cpu, 1),
                    "memory_mb": rss_mb,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    rows.sort(key=lambda r: (r["cpu_percent"], r["memory_mb"]), reverse=True)
    top = rows[:_TOP_N]
    return {
        "ok": True,
        "action": "list",
        "processes": top,
        "count": len(top),
    }


def _is_protected_pid(pid: int) -> bool:
    """Refuse to terminate PID <= 1 or the current agent process."""
    if pid <= 1:
        return True
    if pid == os.getpid():
        return True
    return False


def _kill_process_tree(target: Optional[str]) -> dict[str, Any]:
    if not target:
        return {
            "ok": False,
            "action": "kill",
            "error": 'target_process is required when action is "kill"',
        }

    # Explicit protected PID checks before resolution.
    if target.isdigit():
        pid = int(target)
        if _is_protected_pid(pid):
            return {
                "ok": False,
                "action": "kill",
                "error": (
                    f"refused to kill protected process pid={pid} "
                    "(PID <= 1 or current agent process)"
                ),
                "target_process": target,
            }

    matches = _resolve_targets(target)
    if not matches:
        return {
            "ok": False,
            "action": "kill",
            "error": f"no running process matched {target!r}",
            "target_process": target,
        }

    terminated: list[dict[str, Any]] = []
    errors: list[str] = []

    for proc in matches:
        try:
            pid = int(proc.pid)
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        if _is_protected_pid(pid):
            errors.append(
                f"refused to kill protected process pid={pid} name={name!r} "
                "(PID <= 1 or current agent process)"
            )
            continue

        try:
            children = proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            errors.append(f"pid={pid}: {exc}")
            continue

        # Terminate children first, then the parent.
        tree = list(reversed(children)) + [proc]
        for node in tree:
            try:
                node_pid = int(node.pid)
                node_name = node.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if _is_protected_pid(node_pid):
                errors.append(
                    f"refused to kill protected process pid={node_pid} name={node_name!r}"
                )
                continue
            try:
                node.terminate()
                terminated.append({"pid": node_pid, "name": node_name, "signal": "SIGTERM"})
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                errors.append(f"pid={node_pid}: {exc}")

        # Escalate survivors to SIGKILL.
        _, alive = psutil.wait_procs(tree, timeout=_TERMINATE_WAIT_S)
        for node in alive:
            try:
                node_pid = int(node.pid)
                node_name = node.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if _is_protected_pid(node_pid):
                continue
            try:
                node.kill()
                terminated.append({"pid": node_pid, "name": node_name, "signal": "SIGKILL"})
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                errors.append(f"pid={node_pid} kill: {exc}")

    ok = bool(terminated) and not errors
    # Partial success still reports terminated list.
    if terminated and errors:
        ok = True
    out: dict[str, Any] = {
        "ok": ok if terminated else False,
        "action": "kill",
        "target_process": target,
        "terminated": terminated,
    }
    if errors:
        out["errors"] = errors
    if not terminated:
        out["error"] = errors[0] if errors else f"failed to terminate {target!r}"
    return out


def _resolve_targets(target: str) -> list[psutil.Process]:
    """Match by numeric PID or exact process name (case-insensitive)."""
    if target.isdigit():
        pid = int(target)
        if _is_protected_pid(pid):
            return []
        try:
            return [psutil.Process(pid)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

    needle = target.lower()
    found: list[psutil.Process] = []
    # Prefer exact name matches; fall back to substring (e.g. "chrome" → "Google Chrome").
    exact: list[psutil.Process] = []
    partial: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name:
                continue
            if name == needle:
                exact.append(proc)
            elif len(needle) >= 4 and (needle in name or name in needle):
                partial.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    found = exact or partial
    return found


def manage_system_resources_from_args(payload: dict[str, Any]) -> dict[str, Any]:
    """Registry adapter: accept a free-form args dict."""
    target = payload.get("target_process")
    if target is None:
        target = payload.get("target") or payload.get("process") or payload.get("name")
    return manage_system_resources(
        action=str(payload.get("action") or "list"),
        target_process=str(target) if target is not None else None,
    )

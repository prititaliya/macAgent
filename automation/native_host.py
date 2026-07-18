#!/usr/bin/env python3
"""Chrome Native Messaging host for MacAgent.

Credentials: always invoke the macOS security CLI so Touch ID / Keychain
authorization is prompted on every request. No in-process password cache.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
from typing import Any, Optional


def get_message() -> Optional[dict[str, Any]]:
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        return None
    length = struct.unpack("@I", raw_length)[0]
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send_message(message: dict[str, Any]) -> None:
    encoded = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def query_keychain_password(domain: str) -> str:
    """Fetch password for service `domain`. Triggers Keychain/Touch ID every call."""
    cmd = ["security", "find-generic-password", "-s", domain, "-w"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def query_keychain_account(domain: str) -> str:
    """Parse account name from Keychain item metadata (may also prompt)."""
    cmd = ["security", "find-generic-password", "-s", domain, "-g"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    blob = (result.stderr or "") + (result.stdout or "")
    match = re.search(r'"acct"<blob>="([^"]*)"', blob)
    if match:
        return match.group(1)
    match = re.search(r'"acct"<blob>=<NULL>', blob)
    if match:
        return ""
    return ""


def handle_credentials(msg: dict[str, Any]) -> dict[str, Any]:
    domain = (msg.get("domain") or "").strip()
    if not domain:
        return {"ok": False, "error": "domain required"}

    # Always hit Keychain fresh — no caching across messages.
    password = query_keychain_password(domain)
    username = (msg.get("username") or "").strip()
    if not username:
        username = query_keychain_account(domain)

    return {
        "ok": bool(password or username),
        "credentials": {
            "username": username,
            "password": password,
        },
    }


def main() -> int:
    while True:
        msg = get_message()
        if msg is None:
            break
        action = msg.get("action")
        if action == "request_credentials":
            send_message(handle_credentials(msg))
        elif action == "ping":
            send_message({"ok": True, "pong": True})
        else:
            send_message({"ok": False, "error": f"unknown action: {action}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

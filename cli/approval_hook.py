"""Single-shot Claude/Codex approval hook entry point."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .approval import ApprovalHook, ApprovalPolicy

MAX_HOOK_INPUT_BYTES = 256 * 1024


def main() -> int:
    raw = sys.stdin.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(raw.encode("utf-8", errors="replace")) > MAX_HOOK_INPUT_BYTES:
        return 0
    try:
        payload: Any = json.loads(raw)
    except TypeError, ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0

    try:
        policy = ApprovalPolicy.from_environment()
    except TypeError, ValueError:
        return 0

    output = ApprovalHook(policy).handle_payload(payload)
    _record_activity(payload, output)
    if output is not None:
        json.dump(output, sys.stdout, ensure_ascii=True, separators=(",", ":"))
        sys.stdout.write("\n")
    return 0


def _record_activity(payload: dict[str, Any], output: dict[str, Any] | None) -> None:
    """Record optional hook health without leaking command text or authorizing it."""
    health_file = os.environ.get("FCC_APPROVAL_HEALTH_FILE")
    event_name = payload.get("hook_event_name") or payload.get("hookEventName")
    if not health_file or event_name not in {"PreToolUse", "PermissionRequest"}:
        return
    try:
        path = Path(health_file)
        if not path.is_absolute():
            return
        backend = "codex" if payload.get("turn_id") or payload.get("turnId") else "claude"
        decision = "deny" if output is not None else "silent"
        event = {
            "backend": backend,
            "event": event_name,
            "generation": os.environ.get("FCC_APPROVAL_GENERATION"),
            "decision": decision,
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")))
            stream.write("\n")
    except OSError:
        # Hook telemetry must never become another approval or execution gate.
        return


if __name__ == "__main__":
    raise SystemExit(main())

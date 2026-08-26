"""Single-shot Claude/Codex approval hook entry point."""

from __future__ import annotations

import json
import sys
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
    if output is not None:
        json.dump(output, sys.stdout, ensure_ascii=True, separators=(",", ":"))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

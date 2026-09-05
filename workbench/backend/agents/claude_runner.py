"""Real Claude CLI runner for the SOP Planner (S0-R P3).

Thin, testable wrapper around the actual WSL Claude Code CLI (2.1.220).
Security model: the Planner's tool surface is a HARD whitelist —
``--tools=Read,Grep,Glob`` removes every other tool from the schema
(``--permission-mode plan`` is a behavior constraint, NOT the boundary).
``--safe-mode`` / ``--strict-mcp-config`` disable CLAUDE.md/skills/plugins/
hooks/MCP.  Errors are typed so the SOP layer never swallows a failure into
an empty PLAN.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from workbench.backend.agents.claude_plan import (
    PlanParseError,
    parse_plan,
)

PLANNER_TOOLS = "Read,Grep,Glob"
PLANNER_MODEL_WHITELIST_ARGV = (
    "--safe-mode",
    "--strict-mcp-config",
    "--permission-mode",
    "plan",
    f"--tools={PLANNER_TOOLS}",
)


class ClaudeRunError(RuntimeError):
    """Base class for typed Claude CLI failures."""


class ClaudeCliMissing(ClaudeRunError):
    """The claude binary is not installed / not executable."""


class ClaudeTimeout(ClaudeRunError):
    """The CLI did not finish within the timeout."""


class ClaudeNonZeroExit(ClaudeRunError):
    """The CLI exited non-zero (and no usable stream-json was produced)."""

    def __init__(self, exit_code: int, stderr: str):
        super().__init__(f"claude exited {exit_code}: {stderr[-300:]}")
        self.exit_code = exit_code
        self.stderr = stderr


class ClaudeMalformedOutput(ClaudeRunError):
    """No usable stream-json events were produced."""


@dataclass
class ClaudeRunResult:
    session_id: str | None
    text: str
    tools: set[str] = field(default_factory=set)
    is_error: bool | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    num_turns: int | None = None


def build_planner_argv(
    *,
    prompt: str,
    session_id: str | None = None,
    claude_bin: str = "claude",
    extra_flags: tuple[str, ...] = (),
) -> list[str]:
    """Assemble the hard-whitelisted planner argv (--print stream-json)."""
    argv = [
        claude_bin,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        *PLANNER_MODEL_WHITELIST_ARGV,
        *extra_flags,
    ]
    if session_id is not None:
        argv += ["--resume", session_id]
    argv.append(prompt)
    return argv


def run_claude_once(
    *,
    prompt: str,
    cwd: str,
    claude_bin: str = "claude",
    session_id: str | None = None,
    timeout: float = 240.0,
    env: dict[str, str] | None = None,
    extra_flags: tuple[str, ...] = (),
) -> ClaudeRunResult:
    """Run one real Claude CLI invocation; parse stream-json events."""
    argv = build_planner_argv(
        prompt=prompt, session_id=session_id, claude_bin=claude_bin, extra_flags=extra_flags
    )
    child_env = dict(os.environ if env is None else env)
    # the agent runtime poisons node CLIs with these two vars
    child_env.pop("NODE_OPTIONS", None)
    child_env.pop("ELECTRON_RUN_AS_NODE", None)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise ClaudeCliMissing(f"claude binary not found: {claude_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeTimeout(f"claude timed out after {timeout}s") from exc

    return parse_claude_stream_json(
        stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode
    )


def parse_claude_stream_json(
    *, stdout: str, stderr: str, returncode: int
) -> ClaudeRunResult:
    """Parse ``--print --output-format stream-json`` output into a result.

    Shared by the SOP planner runner and the chat runtime (which assembles
    its own conversational argv but must interpret events identically).
    """
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue

    if returncode != 0 and not events:
        raise ClaudeNonZeroExit(returncode, stderr)

    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    result = next((e for e in events if e.get("type") == "result"), None)
    if init is None and result is None:
        raise ClaudeMalformedOutput(f"no system/init or result event; stderr: {stderr[-200:]}")

    texts: list[str] = []
    for e in events:
        if e.get("type") == "assistant":
            for block in e.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))

    return ClaudeRunResult(
        session_id=(result or init or {}).get("session_id"),
        text="\n".join(texts),
        tools=set((init or {}).get("tools") or []),
        is_error=bool(result.get("is_error")) if result else None,
        stop_reason=result.get("stop_reason") if result else None,
        usage=dict(result.get("usage") or {}) if result else {},
        model=init.get("model") if init else None,
        num_turns=result.get("num_turns") if result else None,
    )


def run_plan(
    *,
    prompt: str,
    cwd: str,
    claude_bin: str = "claude",
    session_id: str | None = None,
    timeout: float = 240.0,
    env: dict[str, str] | None = None,
    extra_flags: tuple[str, ...] = (),
) -> tuple[ClaudeRunResult, dict[str, Any]]:
    """Run the real Claude CLI AND produce a validated PLAN.  FAIL CLOSED.

    The returned plan is guaranteed to pass ``validate_plan()`` — it is the
    only result that may become an ``ArtifactType.PLAN`` and trigger a
    PLAN_READY handoff.  Every failure path raises a typed error instead:

      * ClaudeCliMissing / ClaudeTimeout / ClaudeNonZeroExit /
        ClaudeMalformedOutput  — CLI/transport failures
      * result.is_error        -> PlanParseError (model flagged an error)
      * empty / garbage text   -> PlanParseError / PlanValidationError
    """
    result = run_claude_once(
        prompt=prompt,
        cwd=cwd,
        claude_bin=claude_bin,
        session_id=session_id,
        timeout=timeout,
        env=env,
        extra_flags=extra_flags,
    )
    if result.is_error:
        raise PlanParseError(
            f"claude result reported is_error={result.is_error}; refusing to build a PLAN"
        )
    if not result.text.strip():
        raise PlanParseError("claude produced no assistant text; refusing to build a PLAN")
    plan = parse_plan(result.text)  # raises PlanParseError / PlanValidationError on failure
    return result, plan

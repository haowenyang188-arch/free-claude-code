from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_policy_allows_known_read_only_command_once(tmp_path: Path) -> None:
    from cli.approval import (
        ApprovalDecision,
        ApprovalPolicy,
        ApprovalRequest,
        ApprovalScope,
    )

    policy = ApprovalPolicy.low_risk(
        enabled=True,
        allowed_workspaces=[tmp_path],
    )
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="git status --short",
        workspace=str(tmp_path),
        scope=ApprovalScope.ONCE,
    )

    result = policy.evaluate(request)

    assert result.decision is ApprovalDecision.ALLOW
    assert result.scope is ApprovalScope.ONCE
    assert result.matched_rule == "git status"


def test_policy_never_auto_approves_permanent_scope_by_default(tmp_path: Path) -> None:
    from cli.approval import (
        ApprovalDecision,
        ApprovalPolicy,
        ApprovalRequest,
        ApprovalScope,
    )

    policy = ApprovalPolicy.low_risk(
        enabled=True,
        allowed_workspaces=[tmp_path],
    )
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="git status",
        workspace=str(tmp_path),
        scope=ApprovalScope.PERMANENT,
    )

    result = policy.evaluate(request)

    assert result.decision is ApprovalDecision.ASK
    assert result.scope is None
    assert "permanent" in result.reason


def test_policy_denies_dangerous_command_even_when_prefix_is_allowed(
    tmp_path: Path,
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy(
        enabled=True,
        allowed_workspaces=[tmp_path],
        allowed_command_prefixes=["git"],
    )
    request = ApprovalRequest(
        backend="claude",
        tool_name="Bash",
        command="git reset --hard HEAD",
        workspace=str(tmp_path),
    )

    result = policy.evaluate(request)

    assert result.decision is ApprovalDecision.DENY
    assert result.scope is None
    assert "destructive" in result.reason


def test_policy_falls_back_to_ask_for_shell_compounds_and_unknown_commands(
    tmp_path: Path,
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
    compound = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="git status && rm -rf build",
        workspace=str(tmp_path),
    )
    unknown = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="custom-tool --safe",
        workspace=str(tmp_path),
    )

    assert policy.evaluate(compound).decision is ApprovalDecision.DENY
    assert policy.evaluate(unknown).decision is ApprovalDecision.ASK


def test_policy_requires_workspace_containment(tmp_path: Path) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="git status",
        workspace=str(tmp_path.parent / "outside"),
    )

    result = policy.evaluate(request)

    assert result.decision is ApprovalDecision.ASK
    assert "workspace" in result.reason


def test_hook_output_uses_codex_permission_request_schema(tmp_path: Path) -> None:
    from cli.approval import ApprovalHook

    hook = ApprovalHook(
        ApprovalHook.policy_for(
            enabled=True,
            allowed_workspaces=[tmp_path],
        )
    )
    payload = {
        "hook_event_name": "PermissionRequest",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    }

    output = hook.handle_payload(payload)

    assert output is not None
    assert output["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert output["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    json.dumps(output)


def test_hook_leaves_unknown_pre_tool_use_requests_unanswered(tmp_path: Path) -> None:
    from cli.approval import ApprovalHook

    hook = ApprovalHook(
        ApprovalHook.policy_for(
            enabled=True,
            allowed_workspaces=[tmp_path],
        )
    )
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-1",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "custom-tool --safe"},
    }

    assert hook.handle_payload(payload) is None


def test_prompt_parser_distinguishes_codex_scopes_and_strips_ansi() -> None:
    from cli.approval import ApprovalDecision, ApprovalPromptParser, ApprovalScope

    prompt = (
        "\x1b[2K\x1b[1;36m1. Yes, proceed (y)\x1b[0m\n"
        "2. Yes, and don't ask again for commands that start with `rg` (p)\n"
        "3. No, and tell Codex what to do differently (esc)"
    )

    parsed = ApprovalPromptParser().parse(prompt)

    assert parsed is not None
    assert parsed.kind == "command"
    assert parsed.command_prefix == "rg"
    assert parsed.options[0].decision is ApprovalDecision.ALLOW
    assert parsed.options[0].scope is ApprovalScope.ONCE
    assert parsed.options[1].scope is ApprovalScope.PERMANENT
    assert parsed.options[2].decision is ApprovalDecision.DENY
    assert parsed.options[2].key == "esc"


def test_prompt_parser_requires_a_complete_menu() -> None:
    from cli.approval import ApprovalPromptParser

    assert ApprovalPromptParser().parse("Allow for this session") is None


@pytest.mark.parametrize(
    ("line", "scope"),
    [
        ("Allow for this session", "session"),
        ("Always allow", "permanent"),
        ("Yes, proceed", "once"),
    ],
)
def test_prompt_parser_recognizes_common_scope_labels(line: str, scope: str) -> None:
    from cli.approval import ApprovalPromptParser

    parsed = ApprovalPromptParser().parse(f"{line}\nCancel")

    assert parsed is not None
    assert any(option.scope.value == scope for option in parsed.options)

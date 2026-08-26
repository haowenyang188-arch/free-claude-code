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


def test_safe_tool_payload_cannot_smuggle_a_shell_command(tmp_path: Path) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
    request = ApprovalRequest(
        backend="codex",
        tool_name="Read",
        command="rm -rf /",
        workspace=str(tmp_path),
        tool_input={"file_path": "README.md", "command": "rm -rf /"},
    )

    assert policy.evaluate(request).decision is ApprovalDecision.DENY


def test_safe_tool_requires_the_expected_input_shape(tmp_path: Path) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
    request = ApprovalRequest(
        backend="codex",
        tool_name="Read",
        workspace=str(tmp_path),
        tool_input={},
    )

    assert policy.evaluate(request).decision is ApprovalDecision.ASK


@pytest.mark.parametrize(
    "command",
    [
        "find . -exec rm -rf {} +",
        "xargs rm -rf /",
        "bash -c 'rm -rf /'",
        "/usr/bin/rm -rf /",
        "git push -f origin main",
        "git push --force-with-lease origin main",
    ],
)
def test_policy_denies_nested_or_force_destructive_commands(
    tmp_path: Path, command: str
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy(
        enabled=True,
        allowed_workspaces=[tmp_path],
        allowed_command_prefixes=["find", "xargs", "bash", "git", "/usr/bin/rm"],
    )
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command=command,
        workspace=str(tmp_path),
    )

    assert policy.evaluate(request).decision is ApprovalDecision.DENY


def test_enabled_policy_without_a_workspace_allowlist_fails_closed(
    tmp_path: Path,
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy.low_risk(enabled=True)
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="git status",
        workspace=str(tmp_path),
    )

    assert policy.evaluate(request).decision is ApprovalDecision.ASK


def test_hook_requires_an_explicit_event_name(tmp_path: Path) -> None:
    from cli.approval import ApprovalHook

    hook = ApprovalHook(
        ApprovalHook.policy_for(enabled=True, allowed_workspaces=[tmp_path])
    )
    payload = {
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    }

    assert hook.handle_payload(payload) is None


def test_hook_does_not_trust_scope_from_untrusted_payload(tmp_path: Path) -> None:
    from cli.approval import ApprovalPolicy, ApprovalRequest, ApprovalScope

    payload = {
        "hook_event_name": "PermissionRequest",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "approval_scope": "session",
        "tool_input": {"command": "git status"},
    }

    request = ApprovalRequest.from_mapping(payload)
    assert request.scope is ApprovalScope.ONCE
    result = ApprovalPolicy.low_risk(
        enabled=True,
        allowed_workspaces=[tmp_path],
        max_auto_scope=ApprovalScope.SESSION,
    ).evaluate(request)
    assert result.scope is ApprovalScope.ONCE


def test_policy_rejects_string_boolean_constructor_values(tmp_path: Path) -> None:
    from cli.approval import ApprovalPolicy

    with pytest.raises(ValueError, match="enabled"):
        ApprovalPolicy(enabled="false", allowed_workspaces=[tmp_path])
    with pytest.raises(ValueError, match="allow_permanent"):
        ApprovalPolicy(
            enabled=True,
            allow_permanent="false",
            allowed_workspaces=[tmp_path],
        )


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


def test_environment_policy_defaults_to_current_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    monkeypatch.setenv("FCC_APPROVAL_ENABLED", "true")
    monkeypatch.delenv("FCC_APPROVAL_WORKSPACES", raising=False)
    monkeypatch.chdir(tmp_path)

    policy = ApprovalPolicy.from_environment()
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="git status",
        workspace=str(tmp_path),
    )
    outside = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="git status",
        workspace=str(tmp_path.parent),
    )

    assert policy.evaluate(request).decision is ApprovalDecision.ALLOW
    assert policy.evaluate(outside).decision is ApprovalDecision.ASK


def test_prompt_parser_distinguishes_codex_scopes_and_strips_ansi() -> None:
    from cli.approval import ApprovalDecision, ApprovalPromptParser, ApprovalScope

    prompt = (
        "Do you want to proceed with this command?\n"
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


def test_prompt_parser_rejects_unstructured_or_negative_text() -> None:
    from cli.approval import ApprovalPromptParser

    parser = ApprovalPromptParser()

    assert parser.parse("Allow result\nCancel") is None
    assert parser.parse("Do not allow this command\n2. Cancel") is None


@pytest.mark.parametrize(
    ("line", "scope"),
    [
        ("1. Allow for this session", "session"),
        ("1. Always allow", "permanent"),
        ("1. Yes, proceed", "once"),
    ],
)
def test_prompt_parser_recognizes_common_scope_labels(line: str, scope: str) -> None:
    from cli.approval import ApprovalPromptParser

    parsed = ApprovalPromptParser().parse(f"Do you want to proceed?\n{line}\n2. Cancel")

    assert parsed is not None
    assert any(option.scope.value == scope for option in parsed.options)

from __future__ import annotations

import json
import os
import subprocess
import sys
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
        command="pwd",
        workspace=str(tmp_path),
        scope=ApprovalScope.ONCE,
    )

    result = policy.evaluate(request)

    assert result.decision is ApprovalDecision.ALLOW
    assert result.scope is ApprovalScope.ONCE
    assert result.matched_rule == "pwd"


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
        command="pwd",
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
        command="pwd",
        workspace=str(tmp_path.parent / "outside"),
    )

    result = policy.evaluate(request)

    assert result.decision is ApprovalDecision.ASK
    assert "workspace" in result.reason


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "rg API_KEY .env.local",
        "git show HEAD:.env",
        "cat ~/.ssh/id_ed25519",
    ],
)
def test_policy_denies_sensitive_path_reads(tmp_path: Path, command: str) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command=command,
        workspace=str(tmp_path),
    )

    assert policy.evaluate(request).decision is ApprovalDecision.DENY


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.netrc",
        "cat $HOME/.netrc",
        "cat $PWD/../outside/secret",
        "cat /tmp/outside/credential.txt",
        r"cat C:\Users\someone\credential.txt",
        "cat {src,../../outside}/secret",
        "rg token ../outside",
        "git diff --no-index /tmp/a /tmp/b",
    ],
)
def test_policy_requires_command_file_operands_to_stay_in_workspace(
    tmp_path: Path, command: str
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command=command,
        workspace=str(tmp_path),
    )

    assert policy.evaluate(request).decision is ApprovalDecision.ASK


def test_policy_allows_existing_command_paths_inside_workspace(tmp_path: Path) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    source = tmp_path / "src"
    source.mkdir()
    source.joinpath("main.py").write_text("print('ok')\n", encoding="utf-8")
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="ls src",
        workspace=str(tmp_path),
    )

    assert (
        ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
        .evaluate(request)
        .decision
        is ApprovalDecision.ALLOW
    )


def test_read_and_glob_tools_reject_outside_workspace_paths(tmp_path: Path) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
    requests = (
        ApprovalRequest(
            backend="claude",
            tool_name="Read",
            workspace=str(tmp_path),
            tool_input={"file_path": "/tmp/outside/credential.txt"},
        ),
        ApprovalRequest(
            backend="claude",
            tool_name="Glob",
            workspace=str(tmp_path),
            tool_input={"pattern": "../../outside/*"},
        ),
        ApprovalRequest(
            backend="claude",
            tool_name="Glob",
            workspace=str(tmp_path),
            tool_input={"path": "src", "pattern": "../../outside/*"},
        ),
        ApprovalRequest(
            backend="claude",
            tool_name="Read",
            workspace=str(tmp_path),
            tool_input={"file_path": "$HOME/.netrc"},
        ),
        ApprovalRequest(
            backend="claude",
            tool_name="Grep",
            workspace=str(tmp_path),
            tool_input={"pattern": "API_KEY", "path": "."},
        ),
    )

    assert all(
        policy.evaluate(request).decision is ApprovalDecision.ASK
        for request in requests
    )


def test_low_risk_policy_does_not_auto_approve_file_contents(tmp_path: Path) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    source = tmp_path / "source.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    requests = (
        ApprovalRequest(
            backend="codex",
            tool_name="Bash",
            command="cat source.py",
            workspace=str(tmp_path),
        ),
        ApprovalRequest(
            backend="codex",
            tool_name="Read",
            workspace=str(tmp_path),
            tool_input={"file_path": "source.py"},
        ),
    )

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
    assert all(
        policy.evaluate(request).decision is ApprovalDecision.ASK
        for request in requests
    )


@pytest.mark.parametrize(
    "command",
    ["grep -r API_KEY .", "grep -R token src-link"],
)
def test_low_risk_policy_does_not_auto_approve_recursive_searches(
    tmp_path: Path, command: str
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command=command,
        workspace=str(tmp_path),
    )

    assert (
        ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
        .evaluate(request)
        .decision
        is ApprovalDecision.ASK
    )


@pytest.mark.parametrize(
    "command",
    ["grep -r API_KEY .", "grep -rn API_KEY .", "rg --pre=cat token src", "rg TOKEN ."],
)
def test_explicit_search_prefix_still_rejects_dynamic_execution(
    tmp_path: Path, command: str
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command=command,
        workspace=str(tmp_path),
    )
    policy = ApprovalPolicy(
        enabled=True,
        allowed_workspaces=[tmp_path],
        allowed_command_prefixes=[command.split()[0]],
    )

    assert policy.evaluate(request).decision is ApprovalDecision.ASK


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
        "git branch -D stale-branch",
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


def test_low_risk_policy_does_not_allow_find_by_default(tmp_path: Path) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="find . -name '*.py'",
        workspace=str(tmp_path),
    )

    result = ApprovalPolicy.low_risk(
        enabled=True, allowed_workspaces=[tmp_path]
    ).evaluate(request)

    assert result.decision is ApprovalDecision.ASK


def test_low_risk_policy_does_not_allow_git_by_default(tmp_path: Path) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="git status",
        workspace=str(tmp_path),
    )

    assert (
        ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
        .evaluate(request)
        .decision
        is ApprovalDecision.ASK
    )


@pytest.mark.parametrize(
    "command",
    [
        "find . -delete",
        "xargs echo safe",
        "bash -c 'git status'",
        "git config --global user.name example",
    ],
)
def test_dynamic_execution_wrappers_never_auto_approve(
    tmp_path: Path, command: str
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    policy = ApprovalPolicy(
        enabled=True,
        allowed_workspaces=[tmp_path],
        allowed_command_prefixes=["find", "xargs", "bash", "git"],
    )
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command=command,
        workspace=str(tmp_path),
    )

    assert policy.evaluate(request).decision is ApprovalDecision.ASK


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
        "tool_input": {"command": "pwd"},
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
        "tool_input": {"command": "pwd"},
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


def test_policy_rejects_scalar_iterables_and_preserves_empty_collections(
    tmp_path: Path,
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    with pytest.raises(ValueError, match="allowed_command_prefixes"):
        ApprovalPolicy(enabled=True, allowed_command_prefixes="pwd")
    with pytest.raises(ValueError, match="allowed_workspaces"):
        ApprovalPolicy(enabled=True, allowed_workspaces=str(tmp_path))

    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="pwd",
        workspace=str(tmp_path),
    )
    policy = ApprovalPolicy(
        enabled=True,
        allowed_workspaces=[tmp_path],
        allowed_command_prefixes=(),
        safe_tools=(),
        backends=(),
    )

    assert policy.evaluate(request).decision is ApprovalDecision.ASK


@pytest.mark.parametrize("identity_field", ["process_id", "generation"])
def test_session_rejects_stale_approval_identity(
    tmp_path: Path, identity_field: str
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest
    from cli.codex_session import CodexSession

    session = CodexSession(
        str(tmp_path),
        approval_policy=ApprovalPolicy.low_risk(
            enabled=True, allowed_workspaces=[tmp_path]
        ),
    )
    process = type("Process", (), {"pid": 42})()
    session.process = process
    session.generation = "current-generation"
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="git status",
        workspace=str(tmp_path),
        **{identity_field: 41 if identity_field == "process_id" else "old-generation"},
    )

    result = session.evaluate_approval(request)

    assert result.decision is ApprovalDecision.ASK
    assert "identity" in result.reason or "generation" in result.reason


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
        "tool_input": {"command": "pwd"},
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


def test_hook_subprocess_uses_application_settings_and_fails_closed(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[2]
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("FCC_APPROVAL_") or key.startswith("CLI_AUTO_APPROVAL_"):
            environment.pop(key)
    environment.update(
        {
            "CLI_AUTO_APPROVAL_ENABLED": "true",
            "CLI_AUTO_APPROVAL_WORKSPACES": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    def run(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "cli.approval_hook"],
            cwd=project_root,
            env=environment,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )

    safe = run(
        {
            "hook_event_name": "PreToolUse",
            "turn_id": "turn-1",
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "pwd"},
        }
    )
    dangerous = run(
        {
            "hook_event_name": "PreToolUse",
            "turn_id": "turn-1",
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "git reset --hard HEAD"},
        }
    )
    malformed = run(
        {
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": "pwd"},
        }
    )

    assert safe.returncode == dangerous.returncode == malformed.returncode == 0
    assert (
        json.loads(safe.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    )
    assert (
        json.loads(dangerous.stdout)["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )
    assert malformed.stdout == ""


def test_environment_policy_requires_an_explicit_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest

    monkeypatch.setenv("FCC_APPROVAL_ENABLED", "true")
    monkeypatch.delenv("FCC_APPROVAL_WORKSPACES", raising=False)
    monkeypatch.delenv("FCC_APPROVAL_WORKSPACES_JSON", raising=False)
    monkeypatch.delenv("CLI_AUTO_APPROVAL_WORKSPACES", raising=False)
    monkeypatch.chdir(tmp_path)

    policy = ApprovalPolicy.from_environment()
    request = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="pwd",
        workspace=str(tmp_path),
    )
    outside = ApprovalRequest(
        backend="codex",
        tool_name="Bash",
        command="pwd",
        workspace=str(tmp_path.parent),
    )

    assert policy.evaluate(request).decision is ApprovalDecision.ASK
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


def test_projected_hook_commands_use_the_running_python() -> None:
    from cli.approval import ApprovalPolicy

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=["/tmp"])
    claude_command = policy.claude_hook_settings()["hooks"]["PreToolUse"][0]["hooks"][
        0
    ]["command"]
    codex_overrides = policy.codex_hook_config_overrides()

    assert claude_command.endswith(" -m cli.approval_hook")
    assert sys.executable in claude_command
    assert all("fcc-approval-hook" not in override for override in codex_overrides)
    assert all("cli.approval_hook" in override for override in codex_overrides)


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

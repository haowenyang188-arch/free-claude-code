from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_approval_settings_are_disabled_and_once_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import Settings

    for key in (
        "CLI_AUTO_APPROVAL_ENABLED",
        "CLI_AUTO_APPROVAL_SCOPE",
        "CLI_AUTO_APPROVAL_COMMANDS",
        "CLI_AUTO_APPROVAL_WORKSPACES",
        "CLI_AUTO_APPROVAL_ALLOW_PERMANENT",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings()

    assert settings.cli_auto_approval_enabled is False
    assert settings.cli_auto_approval_scope == "once"
    assert settings.cli_auto_approval_commands == ""
    assert settings.cli_auto_approval_workspaces == ""
    assert settings.cli_auto_approval_allow_permanent is False


@pytest.mark.parametrize("scope", ["invalid", "always", "global"])
def test_approval_settings_reject_unknown_scope(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    from config.settings import Settings

    monkeypatch.setenv("CLI_AUTO_APPROVAL_SCOPE", scope)

    with pytest.raises(ValidationError, match="CLI_AUTO_APPROVAL_SCOPE"):
        Settings()


def test_approval_settings_accept_explicit_session_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config.settings import Settings

    monkeypatch.setenv("CLI_AUTO_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("CLI_AUTO_APPROVAL_SCOPE", "session")
    monkeypatch.setenv("CLI_AUTO_APPROVAL_COMMANDS", "git status, uv run pytest")
    monkeypatch.setenv("CLI_AUTO_APPROVAL_WORKSPACES", "/tmp/project")

    settings = Settings()

    assert settings.cli_auto_approval_enabled is True
    assert settings.cli_auto_approval_scope == "session"
    assert settings.cli_auto_approval_commands == "git status, uv run pytest"
    assert settings.cli_auto_approval_workspaces == "/tmp/project"


@pytest.mark.asyncio
async def test_manager_passes_approval_policy_to_new_session(tmp_path) -> None:
    from cli.approval import ApprovalPolicy
    from cli.codex_session import CodexSession
    from cli.manager import CLISessionManager

    policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
    manager = CLISessionManager(
        workspace_path=str(tmp_path),
        api_url="http://localhost:8082/v1",
        agent_backend="codex",
        approval_policy=policy,
    )

    session, _session_id, _is_new = await manager.get_or_create_session()

    assert isinstance(session, CodexSession)
    assert session.approval_policy is policy
    assert session.get_stats()["auto_approval_enabled"] is True

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_agent_backend_defaults_to_claude_and_permission_mode_is_plan(monkeypatch):
    from config.settings import Settings

    monkeypatch.delenv("AGENT_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_PERMISSION_MODE", raising=False)

    settings = Settings()

    assert settings.agent_backend == "claude"
    assert settings.agent_permission_mode == "plan"


def test_agent_backend_rejects_unknown_values(monkeypatch):
    from config.settings import Settings

    monkeypatch.setenv("AGENT_BACKEND", "shell")

    with pytest.raises(ValidationError, match="AGENT_BACKEND"):
        Settings()


def test_agent_permission_mode_rejects_dangerous_unknown_values(monkeypatch):
    from config.settings import Settings

    monkeypatch.setenv("AGENT_PERMISSION_MODE", "unrestricted")

    with pytest.raises(ValidationError, match="AGENT_PERMISSION_MODE"):
        Settings()


def test_claude_auth_mode_defaults_to_proxy_and_accepts_local(monkeypatch):
    from config.settings import Settings

    monkeypatch.delenv("CLAUDE_AUTH_MODE", raising=False)
    assert Settings().claude_auth_mode == "proxy"

    monkeypatch.setenv("CLAUDE_AUTH_MODE", "local")
    assert Settings().claude_auth_mode == "local"


def test_claude_auth_mode_rejects_unknown_values(monkeypatch):
    from config.settings import Settings

    monkeypatch.setenv("CLAUDE_AUTH_MODE", "magic")

    with pytest.raises(ValidationError, match="CLAUDE_AUTH_MODE"):
        Settings()


def test_cli_isolation_defaults_safe_and_rejects_unknown(monkeypatch):
    from config.settings import Settings

    monkeypatch.delenv("CLI_ISOLATION_MODE", raising=False)
    assert Settings().cli_isolation_mode == "safe"

    monkeypatch.setenv("CLI_ISOLATION_MODE", "custom")
    with pytest.raises(ValidationError, match="CLI_ISOLATION_MODE"):
        Settings()


def test_codex_write_mode_requires_approval_by_default(monkeypatch):
    from config.settings import Settings

    monkeypatch.setenv("CODEX_SANDBOX", "workspace-write")
    monkeypatch.delenv("CODEX_APPROVAL_REQUIRED", raising=False)

    settings = Settings()

    assert settings.codex_sandbox == "workspace-write"
    assert settings.codex_approval_required is True


@pytest.mark.asyncio
async def test_manager_selects_codex_session(monkeypatch):
    from cli.codex_session import CodexSession
    from cli.manager import CLISessionManager

    manager = CLISessionManager(
        workspace_path="/tmp/project",
        api_url="http://localhost:8082/v1",
        agent_backend="codex",
    )

    session, _session_id, is_new = await manager.get_or_create_session()

    assert is_new is True
    assert isinstance(session, CodexSession)


@pytest.mark.asyncio
async def test_manager_exposes_cached_runtime_status(monkeypatch):
    from cli.manager import CLISessionManager
    from cli.runtime_registry import RuntimeBackend, RuntimeRegistry

    async def runner(argv, _timeout):
        assert argv == ("codex", "--version")
        return 0, b"codex 0.149.1", b""

    monkeypatch.setattr("cli.runtime_registry.shutil.which", lambda _: "/bin/codex")
    manager = CLISessionManager(
        workspace_path="/tmp/project",
        api_url="http://localhost:8082/v1",
        agent_backend="codex",
        runtime_registry=RuntimeRegistry(
            executables={RuntimeBackend.CODEX: "codex"},
            runner=runner,
        ),
    )

    status = await manager.runtime_status()

    assert status["backend"] == "codex"
    assert status["runtime"]["available"] is True
    assert status["runtime"]["version"] == "0.149.1"


@pytest.mark.asyncio
async def test_manager_passes_preflight_policy_to_codex_session():
    from cli.codex_session import CodexSession
    from cli.manager import CLISessionManager

    manager = CLISessionManager(
        workspace_path="/tmp/project",
        api_url="http://localhost:8082/v1",
        agent_backend="codex",
        preflight_runtime=False,
    )

    session, _session_id, _is_new = await manager.get_or_create_session()

    assert isinstance(session, CodexSession)
    assert session.preflight_runtime is False

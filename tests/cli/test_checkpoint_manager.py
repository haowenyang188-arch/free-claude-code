from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_manager_builds_checkpoint_for_safe_idle_session(monkeypatch) -> None:
    from cli.manager import CLISessionManager
    from cli.runtime_registry import RuntimeBackend, RuntimeRegistry

    async def runner(_argv, _timeout):
        return 0, b"codex 0.149.1", b""

    monkeypatch.setattr("cli.runtime_registry.shutil.which", lambda _: "/bin/codex")
    manager = CLISessionManager(
        workspace_path="/tmp/project",
        api_url="http://localhost:8082/v1",
        agent_backend="codex",
        runtime_registry=RuntimeRegistry(
            executables={RuntimeBackend.CODEX: "codex"}, runner=runner
        ),
    )
    session = MagicMock()
    session.current_session_id = "thread-1"
    session.generation = "generation-1"
    session.is_busy = False
    session.last_run_id = "run-1"
    manager._sessions["thread-1"] = session

    manifest = await manager.build_checkpoint(
        "thread-1",
        run_id="run-1",
        step_id="step-1",
        input_digest="a" * 64,
    )

    assert manifest.backend == "codex"
    assert manifest.runtime_version == "0.149.1"
    assert manifest.session_id == "thread-1"
    assert manifest.generation == "generation-1"
    assert manifest.side_effects_allowed is False


@pytest.mark.asyncio
async def test_manager_rejects_checkpoint_for_busy_or_unsafe_session(
    monkeypatch,
) -> None:
    from cli.manager import CLISessionManager
    from cli.runtime_registry import RuntimeBackend, RuntimeRegistry

    async def runner(_argv, _timeout):
        return 0, b"codex 0.149.1", b""

    monkeypatch.setattr("cli.runtime_registry.shutil.which", lambda _: "/bin/codex")
    registry = RuntimeRegistry(
        executables={RuntimeBackend.CODEX: "codex"}, runner=runner
    )
    manager = CLISessionManager(
        workspace_path="/tmp/project",
        api_url="http://localhost:8082/v1",
        agent_backend="codex",
        runtime_registry=registry,
    )
    session = MagicMock()
    session.current_session_id = "thread-1"
    session.generation = "generation-1"
    session.is_busy = True
    manager._sessions["thread-1"] = session

    with pytest.raises(RuntimeError, match="active"):
        await manager.build_checkpoint(
            "thread-1", run_id="run-1", step_id="step-1", input_digest="a" * 64
        )

    session.is_busy = False
    manager.codex_sandbox = "workspace-write"
    with pytest.raises(RuntimeError, match="safe read-only"):
        await manager.build_checkpoint(
            "thread-1", run_id="run-1", step_id="step-1", input_digest="a" * 64
        )


@pytest.mark.asyncio
async def test_manager_writes_checkpoint_atomically(monkeypatch, tmp_path) -> None:
    from cli.checkpoint import CheckpointStore
    from cli.manager import CLISessionManager
    from cli.runtime_registry import RuntimeBackend, RuntimeRegistry

    async def runner(_argv, _timeout):
        return 0, b"claude 2.1.220", b""

    monkeypatch.setattr("cli.runtime_registry.shutil.which", lambda _: "/bin/claude")
    manager = CLISessionManager(
        workspace_path="/tmp/project",
        api_url="http://localhost:8082/v1",
        agent_backend="claude",
        runtime_registry=RuntimeRegistry(
            executables={RuntimeBackend.CLAUDE: "claude"}, runner=runner
        ),
    )
    session = MagicMock()
    session.current_session_id = "session-1"
    session.generation = "generation-1"
    session.is_busy = False
    session.last_run_id = "run-1"
    manager._sessions["session-1"] = session
    store = CheckpointStore(tmp_path / "checkpoint.json")

    manifest = await manager.write_checkpoint(
        store,
        "session-1",
        run_id="run-1",
        step_id="step-1",
        input_digest="b" * 64,
    )

    assert store.load() == manifest

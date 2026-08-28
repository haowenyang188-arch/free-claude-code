from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(json.loads(data.decode()))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._lines = [
            (json.dumps(message, separators=(",", ":")) + "\n").encode()
            for message in messages
        ]

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        return self._lines.pop(0) if self._lines else b""


class _FakeProcess:
    pid = 53123

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(messages)
        self.returncode: int | None = None

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _init_message() -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": "claude-session-1",
        "permissionMode": "auto",
    }


def _result_message() -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "claude-session-1",
    }


@pytest.mark.asyncio
async def test_claude_compatibility_session_auto_allows_level_a_without_prompt(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.claude_compatibility import (
        ClaudeCompatibilitySession,
    )

    process = _FakeProcess(
        [
            _init_message(),
            {
                "type": "control_request",
                "request_id": "request-a",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": {"command": "git status"},
                    "tool_use_id": "tool-a",
                },
            },
            _result_message(),
        ]
    )
    pending: list[Any] = []
    from workbench.backend.runtime.approval import ApprovalManager

    async def on_pending(record: Any) -> None:
        pending.append(record)

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = ClaudeCompatibilitySession(
            tmp_path,
            approval_manager=ApprovalManager(),
            on_approval_pending=on_pending,
        )
        events = [event async for event in session.start_task("inspect")]

    assert pending == []
    response = [
        item for item in process.stdin.writes if item.get("type") == "control_response"
    ]
    assert response[0]["response"]["response"]["behavior"] == "allow"
    assert events[0] == {"type": "session_info", "session_id": "claude-session-1"}
    assert events[-1] == {"type": "exit", "code": 0, "stderr": None}


@pytest.mark.asyncio
async def test_claude_compatibility_session_waits_for_level_b_allow_once(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.claude_compatibility import (
        ClaudeCompatibilitySession,
    )
    from workbench.backend.runtime.approval import ApprovalManager, ApprovalState

    process = _FakeProcess(
        [
            _init_message(),
            {
                "type": "control_request",
                "request_id": "request-b",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": {"command": "python task.py"},
                    "tool_use_id": "tool-b",
                },
            },
            _result_message(),
        ]
    )
    approvals = ApprovalManager()
    pending: list[Any] = []
    pending_event = asyncio.Event()

    async def on_pending(record: Any) -> None:
        pending.append(record)
        pending_event.set()

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = ClaudeCompatibilitySession(
            tmp_path,
            approval_manager=approvals,
            on_approval_pending=on_pending,
            approval_timeout_seconds=5,
        )
        task = asyncio.create_task(_collect_events(session, "run task"))
        await asyncio.wait_for(pending_event.wait(), timeout=1)

        assert pending[0].provider == "claude_cli"
        assert pending[0].status is ApprovalState.PENDING
        await approvals.approve(
            provider="claude_cli",
            session_id=pending[0].session_id,
            call_id=pending[0].call_id,
            command_hash=pending[0].command_hash,
        )
        events = await asyncio.wait_for(task, timeout=1)

    response = [
        item for item in process.stdin.writes if item.get("type") == "control_response"
    ]
    assert response[0]["response"]["response"]["behavior"] == "allow"
    assert events[-1]["type"] == "exit"


@pytest.mark.asyncio
async def test_claude_compatibility_session_denies_level_c_without_workbench_prompt(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.claude_compatibility import (
        ClaudeCompatibilitySession,
    )
    from workbench.backend.runtime.approval import ApprovalManager

    process = _FakeProcess(
        [
            _init_message(),
            {
                "type": "control_request",
                "request_id": "request-c",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": {"command": "rm -rf /"},
                    "tool_use_id": "tool-c",
                },
            },
            _result_message(),
        ]
    )
    pending: list[Any] = []

    async def on_pending(record: Any) -> None:
        pending.append(record)

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = ClaudeCompatibilitySession(
            tmp_path,
            approval_manager=ApprovalManager(),
            on_approval_pending=on_pending,
        )
        events = [event async for event in session.start_task("danger")]

    response = [
        item for item in process.stdin.writes if item.get("type") == "control_response"
    ]
    assert response[0]["response"]["response"]["behavior"] == "deny"
    assert pending == []
    assert events[-1]["type"] == "exit"


@pytest.mark.asyncio
async def test_claude_compatibility_session_denies_compound_command_as_one_call(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.claude_compatibility import (
        ClaudeCompatibilitySession,
    )
    from workbench.backend.runtime.approval import ApprovalManager

    process = _FakeProcess(
        [
            _init_message(),
            {
                "type": "control_request",
                "request_id": "request-compound",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": {"command": "git status && rm -rf ./build"},
                    "tool_use_id": "tool-compound",
                },
            },
            _result_message(),
        ]
    )

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = ClaudeCompatibilitySession(
            tmp_path,
            approval_manager=ApprovalManager(),
        )
        events = [event async for event in session.start_task("compound")]

    response = [
        item for item in process.stdin.writes if item.get("type") == "control_response"
    ]
    assert response[0]["response"]["response"]["behavior"] == "deny"
    assert response[0]["response"]["response"]["message"] == "approval_unavailable"
    assert events[-1]["type"] == "exit"


@pytest.mark.asyncio
async def test_claude_compatibility_external_file_write_requires_allow_once(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.claude_compatibility import ClaudeCompatibilitySession
    from workbench.backend.runtime.approval import ApprovalManager, ApprovalState

    process = _FakeProcess(
        [
            _init_message(),
            {
                "type": "control_request",
                "request_id": "request-outside",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Edit",
                    "input": {
                        "file_path": "/tmp/outside-project.py",
                        "new_string": "x",
                    },
                    "tool_use_id": "tool-outside",
                },
            },
            _result_message(),
        ]
    )
    approvals = ApprovalManager()
    pending: list[Any] = []
    pending_event = asyncio.Event()

    async def on_pending(record: Any) -> None:
        pending.append(record)
        pending_event.set()

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = ClaudeCompatibilitySession(
            tmp_path,
            approval_manager=approvals,
            on_approval_pending=on_pending,
            approval_timeout_seconds=5,
        )
        task = asyncio.create_task(_collect_events(session, "edit outside"))
        await asyncio.wait_for(pending_event.wait(), timeout=1)
        assert pending[0].status is ApprovalState.PENDING
        assert pending[0].permission_scope == "filesystem:write:/tmp/outside-project.py"
        await approvals.approve(
            provider="claude_cli",
            session_id=pending[0].session_id,
            call_id=pending[0].call_id,
            command_hash=pending[0].command_hash,
        )
        await asyncio.wait_for(task, timeout=1)

    response = [
        item for item in process.stdin.writes if item.get("type") == "control_response"
    ]
    assert response[0]["response"]["response"]["behavior"] == "allow"


@pytest.mark.asyncio
async def test_claude_compatibility_sensitive_read_is_level_c(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.claude_compatibility import ClaudeCompatibilitySession
    from workbench.backend.runtime.approval import ApprovalManager

    process = _FakeProcess(
        [
            _init_message(),
            {
                "type": "control_request",
                "request_id": "request-secret",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Read",
                    "input": {"file_path": "/home/gnen/.ssh/id_ed25519"},
                    "tool_use_id": "tool-secret",
                },
            },
            _result_message(),
        ]
    )
    pending: list[Any] = []

    async def on_pending(record: Any) -> None:
        pending.append(record)

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = ClaudeCompatibilitySession(
            tmp_path,
            approval_manager=ApprovalManager(),
            on_approval_pending=on_pending,
        )
        await _collect_events(session, "read secret")

    response = [
        item for item in process.stdin.writes if item.get("type") == "control_response"
    ]
    assert response[0]["response"]["response"]["behavior"] == "deny"
    assert pending == []


@pytest.mark.asyncio
async def test_claude_adapter_uses_compatibility_session_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workbench.backend.agents import claude_adapter as claude_module
    from workbench.backend.agents.claude_adapter import ClaudeCodeAdapter
    from workbench.backend.models import EventType
    from workbench.backend.runtime.approval import ApprovalManager

    class _SessionStub:
        is_busy = False
        process = None

        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def start_task(self, *_args: Any, **_kwargs: Any):
            yield {"type": "session_info", "session_id": "claude-session-1"}
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "done"}]},
            }
            yield {"type": "exit", "code": 0, "stderr": None}

        async def stop(self) -> bool:
            return True

        async def pause(self) -> bool:
            return True

        async def resume(self) -> bool:
            return True

    captured: dict[str, Any] = {}
    monkeypatch.setattr(claude_module, "ClaudeCompatibilitySession", _SessionStub)
    adapter = ClaudeCodeAdapter(
        "claude-1",
        use_compatibility_bridge=True,
        approval_manager=ApprovalManager(),
    )
    assert adapter.use_compatibility_bridge is True
    assert claude_module.ClaudeCompatibilitySession is _SessionStub
    events: list[Any] = []

    async def callback(event: Any) -> None:
        events.append(event)

    adapter.set_event_callback(callback)
    started = await adapter.start_task("run-1", "inspect", str(tmp_path))
    assert started is True, (
        events,
        captured,
        adapter.compatibility_session,
        adapter.status,
        adapter.current_run_id,
    )
    assert adapter.monitor_task is not None
    await adapter.monitor_task

    assert captured["approval_manager"] is adapter.approval_manager
    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.AGENT_MESSAGE,
        EventType.RUN_FINISHED,
    ]
    assert adapter.session_id == "claude-session-1"


@pytest.mark.asyncio
async def test_claude_adapter_recreates_compatibility_session_for_new_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workbench.backend.agents import claude_adapter as claude_module
    from workbench.backend.agents.claude_adapter import ClaudeCodeAdapter
    from workbench.backend.runtime.approval import ApprovalManager

    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    sessions: list[Any] = []

    class _SessionStub:
        is_busy = False
        process = None

        def __init__(self, **kwargs: Any) -> None:
            self.workspace = Path(kwargs["workspace_path"]).resolve()
            sessions.append(self)

        async def start_task(self, *_args: Any, **_kwargs: Any):
            yield {"type": "session_info", "session_id": "claude-session"}
            yield {"type": "exit", "code": 0, "stderr": None}

        async def stop(self) -> bool:
            return True

    monkeypatch.setattr(claude_module, "ClaudeCompatibilitySession", _SessionStub)
    adapter = ClaudeCodeAdapter(
        "claude-1",
        use_compatibility_bridge=True,
        approval_manager=ApprovalManager(),
    )

    assert await adapter.start_task("run-1", "first", str(first_workspace))
    assert adapter.monitor_task is not None
    await adapter.monitor_task
    first_session = adapter.compatibility_session

    assert await adapter.start_task("run-2", "second", str(second_workspace))
    assert adapter.monitor_task is not None
    await adapter.monitor_task

    assert len(sessions) == 2
    assert first_session is not sessions[1]
    assert sessions[1].workspace == second_workspace.resolve()


@pytest.mark.asyncio
async def test_claude_compatibility_cancel_request_cancels_pending_approval(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.claude_compatibility import ClaudeCompatibilitySession
    from workbench.backend.runtime.approval import ApprovalManager, ApprovalState

    process = _FakeProcess(
        [
            _init_message(),
            {
                "type": "control_request",
                "request_id": "request-cancel",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": {"command": "python task.py"},
                    "tool_use_id": "tool-cancel",
                },
            },
            {"type": "control_cancel_request", "request_id": "request-cancel"},
            _result_message(),
        ]
    )
    approvals = ApprovalManager()
    pending_event = asyncio.Event()
    pending: list[Any] = []

    async def on_pending(record: Any) -> None:
        pending.append(record)
        pending_event.set()

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = ClaudeCompatibilitySession(
            tmp_path,
            approval_manager=approvals,
            on_approval_pending=on_pending,
            approval_timeout_seconds=5,
        )
        task = asyncio.create_task(_collect_events(session, "cancel"))
        await asyncio.wait_for(pending_event.wait(), timeout=1)
        events = await asyncio.wait_for(task, timeout=1)

    record = await approvals.get(
        provider="claude_cli",
        session_id=pending[0].session_id,
        call_id=pending[0].call_id,
    )
    assert record.status is ApprovalState.CANCELLED
    assert events[-1]["type"] == "exit"
    responses = [
        item for item in process.stdin.writes if item.get("type") == "control_response"
    ]
    assert responses[0]["response"]["subtype"] == "error"
    assert responses[0]["response"]["error"] == "approval_cancelled"


@pytest.mark.asyncio
async def test_claude_compatibility_allow_response_preserves_updated_input(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.claude_compatibility import ClaudeCompatibilitySession
    from workbench.backend.runtime.approval import ApprovalManager

    tool_input = {"command": "git status", "description": "inspect"}
    process = _FakeProcess(
        [
            _init_message(),
            {
                "type": "control_request",
                "request_id": "request-input",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": tool_input,
                    "tool_use_id": "tool-input",
                },
            },
            _result_message(),
        ]
    )

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = ClaudeCompatibilitySession(
            tmp_path,
            approval_manager=ApprovalManager(),
        )
        await _collect_events(session, "allow")

    response = next(
        item for item in process.stdin.writes if item.get("type") == "control_response"
    )
    inner = response["response"]["response"]
    assert inner["behavior"] == "allow"
    assert inner["updatedInput"] == tool_input


def test_claude_compatibility_command_uses_auto_mode_and_stdio_prompt(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.claude_compatibility import (
        ClaudeCompatibilitySession,
    )

    command = ClaudeCompatibilitySession(tmp_path).build_command(session_id="session-1")

    assert "--permission-mode" in command
    assert command[command.index("--permission-mode") + 1] == "auto"
    assert "--permission-prompt-tool" in command
    assert command[command.index("--permission-prompt-tool") + 1] == "stdio"
    assert "--setting-sources" in command
    assert command[command.index("--setting-sources") + 1] == "user"
    assert "--dangerously-skip-permissions" not in command


async def _collect_events(session: Any, prompt: str) -> list[dict[str, Any]]:
    return [event async for event in session.start_task(prompt)]

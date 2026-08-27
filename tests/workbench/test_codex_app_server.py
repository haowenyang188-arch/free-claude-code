from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
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
    pid = 42123

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(messages)
        self.returncode: int | None = None

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _init_messages(*, turn_status: str = "completed") -> list[dict[str, Any]]:
    return [
        {
            "id": 1,
            "result": {
                "userAgent": "codex-test",
                "codexHome": "/tmp/codex",
                "platformFamily": "unix",
                "platformOs": "linux",
            },
        },
        {"id": 2, "result": {"thread": {"id": "thread-1"}}},
        {"id": 3, "result": {"turn": {"id": "turn-1"}}},
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "delta": "ready",
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": turn_status},
            },
        },
    ]


@pytest.mark.asyncio
async def test_app_server_session_uses_native_sandbox_and_normalizes_events(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession

    process = _FakeProcess(_init_messages())
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = CodexAppServerSession(tmp_path, sandbox_mode="read-only")
        events = [event async for event in session.start_task("inspect")]

    assert events[0] == {"type": "session_info", "session_id": "thread-1"}
    assert events[1]["type"] == "assistant"
    assert events[1]["message"]["content"][0]["text"] == "ready"
    assert events[-1] == {"type": "exit", "code": 0, "stderr": None}
    assert spawn.await_args is not None
    assert spawn.await_args.args[:3] == ("codex", "app-server", "--stdio")
    methods = [message.get("method") for message in process.stdin.writes]
    assert methods == ["initialize", "initialized", "thread/start", "turn/start"]
    thread_params = process.stdin.writes[2]["params"]
    assert thread_params["approvalPolicy"] == "on-request"
    assert thread_params["approvalsReviewer"] == "user"
    assert thread_params["sandbox"] == "read-only"
    assert "sandboxPolicy" not in process.stdin.writes[3]["params"]
    await session.stop()


@pytest.mark.asyncio
async def test_request_defers_unrelated_notifications_without_replaying_forever(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession

    notification = {"method": "thread/status/changed", "params": {"status": "idle"}}
    process = _FakeProcess([notification, {"id": 1, "result": {"ok": True}}])
    session = CodexAppServerSession(tmp_path)
    cast(Any, session).process = process

    result = await asyncio.wait_for(session._request("initialize", {}), timeout=1)

    assert result == {"ok": True}
    assert session._early_notifications == [notification]


@pytest.mark.asyncio
async def test_initialize_failure_releases_owned_process(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession

    process = _FakeProcess([{"id": 1, "error": {"code": -1}}])
    with (
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
        patch("workbench.backend.agents.codex_app_server.register_process") as register,
        patch("workbench.backend.agents.codex_app_server.unregister_process") as unregister,
    ):
        spawn.return_value = process
        session = CodexAppServerSession(tmp_path)

        with pytest.raises(RuntimeError, match="request failed"):
            await session._ensure_started()

    assert session.process is None
    assert process.returncode == -15
    register.assert_called_once()
    unregister.assert_called_once()


@pytest.mark.asyncio
async def test_native_command_approval_waits_for_matching_one_shot_decision(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession
    from workbench.backend.runtime.approval import ApprovalManager, ApprovalState

    messages = [
        *_init_messages()[:3],
        {
            "id": 4,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-command-1",
                "startedAtMs": 1,
                "environmentId": None,
                "command": "python task.py",
                "cwd": str(tmp_path),
            },
        },
        _init_messages()[-1],
    ]
    process = _FakeProcess(messages)
    approvals = ApprovalManager()
    pending: list[Any] = []
    pending_event = asyncio.Event()

    async def on_pending(record: Any) -> None:
        pending.append(record)
        pending_event.set()

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = CodexAppServerSession(
            tmp_path,
            approval_manager=approvals,
            on_approval_pending=on_pending,
            approval_timeout_seconds=5,
        )

        async def collect() -> list[dict[str, Any]]:
            return [event async for event in session.start_task("run task")]

        task = asyncio.create_task(collect())
        await asyncio.wait_for(pending_event.wait(), timeout=1)
        assert pending[0].status is ApprovalState.PENDING
        assert not hasattr(pending[0], "pid")
        await approvals.approve(
            session_id=pending[0].session_id,
            call_id=pending[0].call_id,
            command_hash=pending[0].command_hash,
        )
        events = await asyncio.wait_for(task, timeout=1)

    responses = [message for message in process.stdin.writes if message.get("id") == 4]
    assert responses == [{"id": 4, "result": {"decision": "accept"}}]
    assert events[-1] == {"type": "exit", "code": 0, "stderr": None}
    await session.stop()


@pytest.mark.asyncio
async def test_native_dangerous_command_is_declined_without_workbench_approval(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession
    from workbench.backend.runtime.approval import ApprovalManager

    messages = [
        *_init_messages()[:3],
        {
            "id": 4,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-danger-1",
                "startedAtMs": 1,
                "environmentId": None,
                "command": "rm -rf /",
                "cwd": str(tmp_path),
            },
        },
        _init_messages()[-1],
    ]
    process = _FakeProcess(messages)
    approvals = ApprovalManager()
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = CodexAppServerSession(tmp_path, approval_manager=approvals)
        events = [event async for event in session.start_task("danger")]

    responses = [message for message in process.stdin.writes if message.get("id") == 4]
    assert responses == [{"id": 4, "result": {"decision": "decline"}}]
    assert events[-1]["type"] == "exit"
    await session.stop()


def test_native_compound_command_is_declined_without_partial_approval(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession

    session = CodexAppServerSession(tmp_path)

    intent = session._intent_from_approval(
        "item/commandExecution/requestApproval",
        {
            "threadId": "thread-1",
            "itemId": "item-1",
            "command": "npm test && rm -rf ./build",
            "cwd": str(tmp_path),
        },
    )

    assert intent is None


def test_native_file_change_without_patch_identity_is_declined(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession

    session = CodexAppServerSession(tmp_path)

    intent = session._intent_from_approval(
        "item/fileChange/requestApproval",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-file-1",
        },
    )

    assert intent is None


def test_native_apply_patch_approval_binds_changes_and_rejects_escape(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession

    session = CodexAppServerSession(tmp_path)
    intent = session._intent_from_approval(
        "applyPatchApproval",
        {
            "conversationId": "thread-1",
            "callId": "call-patch-1",
            "fileChanges": {
                "new.txt": {"type": "add", "content": "hello\n"}
            },
        },
    )
    escaped = session._intent_from_approval(
        "applyPatchApproval",
        {
            "conversationId": "thread-1",
            "callId": "call-patch-2",
            "fileChanges": {
                "../outside.txt": {"type": "add", "content": "nope\n"}
            },
        },
    )

    assert intent is not None
    assert intent.argv[0:2] == ("codex-file-change", "call-patch-1")
    assert intent.argv[-1]
    assert escaped is None


@pytest.mark.asyncio
async def test_native_permission_request_grants_only_requested_turn_scope(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession
    from workbench.backend.runtime.approval import ApprovalManager, ApprovalState

    approvals = ApprovalManager()
    pending: list[Any] = []
    pending_event = asyncio.Event()

    async def on_pending(record: Any) -> None:
        pending.append(record)
        pending_event.set()

    session = CodexAppServerSession(
        tmp_path,
        approval_manager=approvals,
        on_approval_pending=on_pending,
        approval_timeout_seconds=5,
    )
    params = {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "itemId": "permission-1",
        "cwd": str(tmp_path),
        "permissions": {
            "network": {"enabled": True},
            "fileSystem": {"entries": [{"path": str(tmp_path)}]},
            "unexpected": {"allow": True},
        },
    }
    task = asyncio.create_task(session._permissions_decision(params))
    await asyncio.wait_for(pending_event.wait(), timeout=1)
    assert pending[0].status is ApprovalState.PENDING
    await approvals.approve(
        session_id=pending[0].session_id,
        call_id=pending[0].call_id,
        command_hash=pending[0].command_hash,
    )

    granted, decision = await asyncio.wait_for(task, timeout=1)

    assert decision == "accept"
    assert granted == {
        "network": {"enabled": True},
        "fileSystem": {"entries": [{"path": str(tmp_path)}]},
    }


@pytest.mark.asyncio
async def test_native_file_change_approval_binds_cached_patch_content(
    tmp_path: Path,
) -> None:
    from workbench.backend.agents.codex_app_server import CodexAppServerSession
    from workbench.backend.runtime.approval import ApprovalManager, ApprovalState

    changes = [
        {
            "path": "new.txt",
            "kind": {"type": "add"},
            "diff": "+hello\n",
        }
    ]
    messages = [
        *_init_messages()[:3],
        {
            "method": "item/fileChange/patchUpdated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-file-1",
                "changes": changes,
            },
        },
        {
            "id": 4,
            "method": "item/fileChange/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-file-1",
                "startedAtMs": 1,
            },
        },
        _init_messages()[-1],
    ]
    process = _FakeProcess(messages)
    approvals = ApprovalManager()
    pending: list[Any] = []
    pending_event = asyncio.Event()

    async def on_pending(record: Any) -> None:
        pending.append(record)
        pending_event.set()

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        session = CodexAppServerSession(
            tmp_path,
            approval_manager=approvals,
            on_approval_pending=on_pending,
            approval_timeout_seconds=5,
        )
        task = asyncio.create_task(
            _collect_events(session, "apply file change")
        )
        await asyncio.wait_for(pending_event.wait(), timeout=1)
        assert pending[0].status is ApprovalState.PENDING
        assert pending[0].argv[0] == "codex-file-change"
        assert pending[0].argv[-1]
        await approvals.approve(
            session_id=pending[0].session_id,
            call_id=pending[0].call_id,
            command_hash=pending[0].command_hash,
        )
        events = await asyncio.wait_for(task, timeout=1)

    responses = [message for message in process.stdin.writes if message.get("id") == 4]
    assert responses == [{"id": 4, "result": {"decision": "accept"}}]
    assert events[-1]["type"] == "exit"
    await session.stop()


async def _collect_events(
    session: Any, prompt: str
) -> list[dict[str, Any]]:
    return [event async for event in session.start_task(prompt)]

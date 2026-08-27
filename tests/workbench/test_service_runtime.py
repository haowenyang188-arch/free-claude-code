from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from cli.runtime_registry import RuntimeBackend, RuntimeProbe, RuntimeProfileProbe
from harness.config import HarnessConfig
from workbench.backend import main as main_module
from workbench.backend.agents import claude_adapter as claude_module
from workbench.backend.agents import codex_adapter as codex_module
from workbench.backend.agents.base import BaseAgentAdapter
from workbench.backend.agents.claude_adapter import ClaudeCodeAdapter
from workbench.backend.agents.codex_adapter import CodexAdapter
from workbench.backend.agents.dsh_adapter import DeepSeekHarnessAdapter
from workbench.backend.main import WorkbenchService
from workbench.backend.models import (
    Agent,
    AgentStatus,
    AgentType,
    CreateTaskRequest,
    Event,
    EventType,
    Run,
    RunStatus,
    Task,
    TaskStatus,
)
from workbench.backend.runtime.events import EventLog
from workbench.backend.runtime.state import StateStore


class FakeAdapter:
    agent_id = "agent-1"
    agent_type = AgentType.CLAUDE_CODE
    status = AgentStatus.ONLINE
    current_run_id: str | None = None

    def __init__(self) -> None:
        self._callback = None

    def set_event_callback(self, callback) -> None:
        self._callback = callback

    async def start_task(
        self, run_id: str, task_description: str, workspace_path: str
    ) -> bool:
        self.current_run_id = run_id
        assert Path(workspace_path).is_absolute()
        callback = self._callback
        assert callback is not None
        await callback(
            Event(
                id="event-finished",
                run_id=run_id,
                type=EventType.RUN_FINISHED,
                data={"message": "done", "api_token": "do-not-leak"},
            )
        )
        return True

    async def send_message(self, message: str) -> bool:
        return True

    async def pause(self) -> bool:
        return True

    async def resume(self) -> bool:
        return True

    async def cancel(self) -> bool:
        return True

    async def cleanup(self) -> None:
        return None


class _EmptyStdout:
    async def readline(self) -> bytes:
        return b""


class _EmptyStderr:
    async def read(self) -> bytes:
        return b""


class _FinishedProcess:
    stdout = _EmptyStdout()
    stderr = _EmptyStderr()
    pid = 0
    returncode = 0

    async def wait(self) -> int:
        return 0


def _service(tmp_path: Path) -> WorkbenchService:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "project").mkdir()
    service = WorkbenchService(
        workspace_root=root,
        event_log_path=tmp_path / "events.jsonl",
        state_path=tmp_path / "state.json",
    )
    adapter = FakeAdapter()
    adapter.set_event_callback(service._handle_event)
    service.agents[adapter.agent_id] = cast(BaseAgentAdapter, adapter)
    return service


@pytest.mark.asyncio
async def test_create_task_canonicalizes_workspace_and_rejects_escape(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)

    task = await service.create_task(
        CreateTaskRequest(
            title="demo",
            description="run it",
            agent_type=AgentType.CLAUDE_CODE,
            workspace_path="project",
        )
    )

    assert task.workspace_path == str((tmp_path / "workspace" / "project").resolve())

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(HTTPException, match="inside workspace root"):
        await service.create_task(
            CreateTaskRequest(
                title="escape",
                description="do not run",
                agent_type=AgentType.CLAUDE_CODE,
                workspace_path=str(outside),
            )
        )


@pytest.mark.asyncio
async def test_event_callback_persists_redacted_legacy_compatible_event(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    run = Run(id="run-1", task_id="task-1", agent_id="agent-1")
    task = Task(
        id="task-1",
        title="demo",
        description="run it",
        agent_id="agent-1",
        agent_type=AgentType.CLAUDE_CODE,
        workspace_path=str(tmp_path / "workspace"),
    )
    service.runs[run.id] = run
    service.tasks[task.id] = task

    event = Event(
        id="event-1",
        run_id=run.id,
        type=EventType.AGENT_MESSAGE,
        data={"message": "hello", "api_token": "do-not-leak"},
    )
    await service._handle_event(event)

    assert event.data["api_token"] == "[redacted]"
    serialized = service._serialize_event(event)
    assert serialized["type"] == EventType.AGENT_MESSAGE.value
    assert serialized["data"]["api_token"] == "[redacted]"
    assert serialized["sequence"] == 1
    assert serialized["runtime_kind"] == AgentType.CLAUDE_CODE.value
    assert serialized["agent_profile_id"] == "agent-1"
    assert EventLog(tmp_path / "events.jsonl").replay(run.id)[0].sequence == 1


@pytest.mark.asyncio
async def test_user_input_required_marks_run_and_task_waiting_human(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    run = Run(id="run-waiting", task_id="task-waiting", agent_id="agent-1")
    task = Task(
        id="task-waiting",
        title="demo",
        description="run it",
        agent_id="agent-1",
        agent_type=AgentType.CLAUDE_CODE,
        workspace_path=str(tmp_path / "workspace"),
        status=TaskStatus.RUNNING,
    )
    service.runs[run.id] = run
    service.tasks[task.id] = task

    await service._handle_event(
        Event(
            id="event-waiting",
            run_id=run.id,
            type=EventType.USER_INPUT_REQUIRED,
            data={"awaiting_approval": True},
        )
    )

    assert service.runs[run.id].status is RunStatus.WAITING_HUMAN
    assert service.tasks[task.id].status is TaskStatus.WAITING_HUMAN


@pytest.mark.asyncio
async def test_event_provenance_stays_bound_to_the_started_generation(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    run = Run(id="run-1", task_id="task-1", agent_id="agent-1")
    service.runs[run.id] = run
    adapter = service.agents["agent-1"]
    adapter.generation = "generation-1"

    await service._handle_event(
        Event(
            id="event-started",
            run_id=run.id,
            type=EventType.RUN_STARTED,
            data={"message": "started"},
        )
    )
    adapter.generation = "generation-2"
    await service._handle_event(
        Event(
            id="event-message",
            run_id=run.id,
            type=EventType.AGENT_MESSAGE,
            data={"message": "still first generation"},
        )
    )

    replay = service.replay_events(run.id)
    assert [event["generation"] for event in replay] == [
        "generation-1",
        "generation-1",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_type,backend",
    [(CodexAdapter, RuntimeBackend.CODEX), (ClaudeCodeAdapter, RuntimeBackend.CLAUDE)],
)
async def test_workbench_adapter_availability_requires_safe_profile(
    adapter_type, backend
) -> None:
    adapter = adapter_type("agent-1")
    adapter.runtime_registry.probe = AsyncMock(
        return_value=RuntimeProbe(
            backend=backend,
            executable=backend.value,
            available=True,
            version="1.0.0",
            capabilities=(),
        )
    )
    adapter.runtime_registry.probe_safe_profile = AsyncMock(
        return_value=RuntimeProfileProbe(
            backend=backend,
            available=False,
            required_flags=("--safe",),
            missing_flags=("--safe",),
            reason="required_flags_missing",
        )
    )

    assert await adapter.check_availability() is False


@pytest.mark.asyncio
async def test_start_task_does_not_overwrite_immediate_terminal_event(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    task = await service.create_task(
        CreateTaskRequest(
            title="demo",
            description="run it",
            agent_type=AgentType.CLAUDE_CODE,
        )
    )

    run = await service.start_task(task.id)

    assert run.status is RunStatus.COMPLETED
    assert service.tasks[task.id].status is TaskStatus.COMPLETED
    assert [item.event_type for item in service.event_log.replay(run.id)] == [
        EventType.RUN_FINISHED.value
    ]


def test_persisted_events_are_available_for_replay_after_service_restart(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "events.jsonl"
    EventLog(log_path).append(
        run_id="run-restored",
        event_type=EventType.RUN_STARTED.value,
        payload={"message": "restored"},
        backend="claude_code",
    )
    service = WorkbenchService(
        workspace_root=tmp_path,
        event_log_path=log_path,
        state_path=tmp_path / "state.json",
    )

    replay = service.replay_events("run-restored")

    assert replay[0]["run_id"] == "run-restored"
    assert replay[0]["event_type"] == EventType.RUN_STARTED.value
    assert replay[0]["sequence"] == 1


@pytest.mark.asyncio
async def test_replay_endpoint_exposes_cursor_and_rejects_invalid_cursor(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "events.jsonl"
    log = EventLog(log_path)
    log.append(
        run_id="run-api",
        event_type=EventType.RUN_STARTED.value,
        payload={"message": "one"},
        backend="codex",
    )
    log.append(
        run_id="run-api",
        event_type=EventType.AGENT_MESSAGE.value,
        payload={"message": "two"},
        backend="codex",
    )
    service = WorkbenchService(
        workspace_root=tmp_path,
        event_log_path=log_path,
        state_path=tmp_path / "state.json",
    )
    previous_service = main_module.service
    main_module.service = service
    try:
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/runs/run-api/events?after=1")
            invalid = await client.get("/api/runs/run-api/events?after=-1")
    finally:
        main_module.service = previous_service

    assert response.status_code == 200
    assert [item["sequence"] for item in response.json()["events"]] == [2]
    assert invalid.status_code == 400


def test_websocket_init_serializes_datetime_fields() -> None:
    class _WebSocketServiceStub:
        def __init__(self) -> None:
            self.tasks: dict[str, Task] = {}
            self.websocket_connections: list[Any] = []

        async def initialize(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

        def get_agents(self) -> list[Agent]:
            return [
                Agent(
                    id="agent-1",
                    name="Codex",
                    type=AgentType.CODEX,
                )
            ]

    previous_service = main_module.service
    previous_auth = main_module.auth
    main_module.service = _WebSocketServiceStub()
    main_module.auth = main_module.WorkbenchAuth("secret-token")
    try:
        with (
            TestClient(main_module.app) as client,
            client.websocket_connect(
                "/ws",
                headers={"Cookie": "workbench_session=secret-token"},
            ) as websocket,
        ):
            message = websocket.receive_json()
    finally:
        main_module.service = previous_service
        main_module.auth = previous_auth

    assert message["type"] == "init"
    assert message["data"]["agents"][0]["created_at"]


@pytest.mark.asyncio
async def test_codex_adapter_emits_terminal_event_before_clearing_run_id() -> None:
    adapter = CodexAdapter("agent-1")
    adapter.current_run_id = "run-1"
    events: list[Event] = []

    class _Session:
        async def start_task(self, *_args, **_kwargs):
            yield {"type": "exit", "code": 0, "stderr": None}

    async def callback(event: Event) -> None:
        events.append(event)

    adapter.session = _Session()
    adapter.set_event_callback(callback)
    await adapter._run_codex_task("task")

    assert [event.type for event in events] == [EventType.RUN_FINISHED]
    assert events[0].run_id == "run-1"
    assert adapter.current_run_id is None


@pytest.mark.asyncio
async def test_claude_adapter_emits_terminal_event_before_clearing_run_id() -> None:
    adapter = ClaudeCodeAdapter("agent-1")
    adapter.current_run_id = "run-1"
    adapter.process = _FinishedProcess()
    events: list[Event] = []

    async def callback(event: Event) -> None:
        events.append(event)

    adapter.set_event_callback(callback)
    await adapter._monitor_output()

    assert [event.type for event in events] == [EventType.RUN_FINISHED]
    assert events[0].run_id == "run-1"
    assert adapter.current_run_id is None


@pytest.mark.asyncio
async def test_codex_adapter_cleanup_closes_shared_session() -> None:
    adapter = CodexAdapter("agent-1")

    class _Session:
        async def stop(self) -> bool:
            stopped.append(True)
            return True

        def reject(self) -> None:
            rejected.append(True)

    stopped: list[bool] = []
    rejected: list[bool] = []
    adapter.session = _Session()

    await adapter.cleanup()

    assert stopped == [True]
    assert rejected == [True]
    assert adapter.session is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "module"),
    [
        (
            CodexAdapter,
            codex_module,
        ),
        (
            ClaudeCodeAdapter,
            claude_module,
        ),
    ],
)
async def test_adapters_use_supported_noninteractive_cli_argv(
    adapter_type,
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FinishedProcess()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_spawn)
    adapter = adapter_type("agent-1")
    await adapter.start_task("run-1", "do the task", str(tmp_path))
    assert adapter.monitor_task is not None
    await adapter.monitor_task

    if adapter_type is CodexAdapter:
        assert captured["args"][:3] == ("codex", "exec", "--json")
        assert "--ignore-user-config" in captured["args"]
        assert "--ignore-rules" in captured["args"]
        assert "--strict-config" in captured["args"]
        assert "--cd" in captured["args"]
    else:
        assert captured["args"][:4] == (
            "claude",
            "--print",
            "--output-format",
            "text",
        )
        assert "--safe-mode" in captured["args"]
        assert "--strict-mcp-config" in captured["args"]
        assert (
            captured["args"][captured["args"].index("--permission-mode") + 1] == "plan"
        )
        assert captured["kwargs"]["env"]["TERM"] == "dumb"
        assert captured["kwargs"]["stderr"] is module.asyncio.subprocess.DEVNULL
    assert captured["args"][-1] == "do the task"
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["stdin"] is module.asyncio.subprocess.DEVNULL
    assert await adapter.send_message("follow-up") is True
    assert adapter.monitor_task is not None
    await adapter.monitor_task


def test_state_store_round_trips_task_and_run_snapshots(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(
        [{"id": "task-1", "metadata": {"api_token": "hidden"}}],
        [{"id": "run-1", "task_id": "task-1", "metadata": {}}],
    )

    snapshot = store.load()

    assert snapshot["tasks"][0]["id"] == "task-1"
    assert snapshot["tasks"][0]["metadata"]["api_token"] == "[redacted]"
    assert snapshot["runs"][0]["id"] == "run-1"


@pytest.mark.asyncio
async def test_service_restores_task_and_run_state_after_restart(
    tmp_path: Path,
) -> None:
    first = _service(tmp_path)
    task = await first.create_task(
        CreateTaskRequest(
            title="persisted",
            description="run it",
            agent_type=AgentType.CLAUDE_CODE,
        )
    )
    run = await first.start_task(task.id)

    restored = WorkbenchService(
        workspace_root=tmp_path / "workspace",
        event_log_path=tmp_path / "events.jsonl",
        state_path=tmp_path / "state.json",
    )

    assert restored.tasks[task.id].title == "persisted"
    assert restored.runs[run.id].status is RunStatus.COMPLETED
    assert (
        restored.replay_events(run.id)[0]["event_type"] == EventType.RUN_FINISHED.value
    )

    replacement = FakeAdapter()
    restored.agents[replacement.agent_id] = cast(BaseAgentAdapter, replacement)
    await restored._rebind_restored_agents()
    assert restored.tasks[task.id].agent_id == replacement.agent_id
    assert restored.runs[run.id].agent_id == replacement.agent_id


def test_service_marks_interrupted_runs_paused_and_stale_on_restart(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = Task(
        id="task-inflight",
        title="in flight",
        description="resume after restart",
        agent_id="agent-before-restart",
        agent_type=AgentType.CODEX,
        status=TaskStatus.RUNNING,
        workspace_path=str(workspace),
        runs=["run-inflight"],
        metadata={"session_id": "thread-1"},
    )
    run = Run(
        id="run-inflight",
        task_id=task.id,
        agent_id=task.agent_id or "",
        status=RunStatus.RUNNING,
        metadata={"session_id": "thread-1"},
    )
    state_path = tmp_path / "state.json"
    StateStore(state_path).save(
        [task.model_dump(mode="json")],
        [run.model_dump(mode="json")],
    )
    event_log_path = tmp_path / "events.jsonl"
    log = EventLog(event_log_path)
    log.append(
        run_id=run.id,
        event_type=EventType.RUN_STARTED.value,
        payload={"message": "started"},
        backend="codex",
    )

    restored = WorkbenchService(
        workspace_root=workspace,
        event_log_path=event_log_path,
        state_path=state_path,
    )

    restored_task = restored.tasks[task.id]
    restored_run = restored.runs[run.id]
    assert restored_task.status is TaskStatus.PAUSED
    assert restored_run.status is RunStatus.PAUSED
    assert restored_task.metadata["session_id"] == "thread-1"
    assert restored_run.metadata["session_id"] == "thread-1"
    assert restored_task.metadata["recovery_state"] == "stale"
    assert restored_run.metadata["recovery_state"] == "stale"
    assert restored_run.metadata["recovery_reason"] == "service_restart"

    persisted = StateStore(state_path).load()
    assert persisted["tasks"][0]["status"] == TaskStatus.PAUSED.value
    assert persisted["runs"][0]["status"] == RunStatus.PAUSED.value
    # Recovery must not add an event or change the replay cursor.
    replay = restored.replay_events(run.id)
    assert [item["sequence"] for item in replay] == [1]
    assert replay[0]["event_type"] == EventType.RUN_STARTED.value


def test_service_leaves_terminal_runs_and_events_unchanged_on_restart(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = Task(
        id="task-completed",
        title="completed",
        description="already done",
        agent_id="agent-1",
        agent_type=AgentType.CLAUDE_CODE,
        status=TaskStatus.COMPLETED,
        workspace_path=str(workspace),
        runs=["run-completed"],
    )
    run = Run(
        id="run-completed",
        task_id=task.id,
        agent_id="agent-1",
        status=RunStatus.COMPLETED,
        metadata={"session_id": "thread-completed"},
    )
    state_path = tmp_path / "state.json"
    StateStore(state_path).save(
        [task.model_dump(mode="json")],
        [run.model_dump(mode="json")],
    )
    event_log_path = tmp_path / "events.jsonl"
    EventLog(event_log_path).append(
        run_id=run.id,
        event_type=EventType.RUN_FINISHED.value,
        payload={"message": "done"},
        backend="claude_code",
    )

    restored = WorkbenchService(
        workspace_root=workspace,
        event_log_path=event_log_path,
        state_path=state_path,
    )

    assert restored.tasks[task.id].status is TaskStatus.COMPLETED
    assert restored.runs[run.id].status is RunStatus.COMPLETED
    assert "recovery_state" not in restored.runs[run.id].metadata
    assert (
        restored.replay_events(run.id)[0]["event_type"] == EventType.RUN_FINISHED.value
    )


@pytest.mark.asyncio
async def test_dsh_adapter_is_opt_in_and_projects_bridge_notifications(
    tmp_path: Path,
) -> None:
    disabled = DeepSeekHarnessAdapter("dsh-disabled", config=HarnessConfig())
    assert await disabled.check_availability() is False
    await disabled.cleanup()

    adapter = DeepSeekHarnessAdapter(
        "dsh-1", config=HarnessConfig(enabled=True, plugin_allowlist=())
    )

    class FakeTurn:
        def __init__(self) -> None:
            self.notifications = [
                {"type": "session_event", "event_type": "assistant/message"}
            ]

    async def fake_run(*args, **kwargs):
        return FakeTurn()

    cast(Any, adapter.bridge).run = fake_run
    events: list[Event] = []

    async def callback(event: Event) -> None:
        events.append(event)

    adapter.set_event_callback(callback)
    await adapter.start_task("dsh-run", "hello", str(tmp_path))
    assert adapter.monitor_task is not None
    await adapter.monitor_task

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.AGENT_MESSAGE,
        EventType.RUN_FINISHED,
    ]
    assert "payload" not in events[1].data["notification"]
    assert events[1].data["notification"]["event_type"] == "assistant/message"
    assert adapter.current_run_id is None

    assert await adapter.send_message("follow-up") is True
    assert adapter.monitor_task is not None
    await adapter.monitor_task
    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.AGENT_MESSAGE,
        EventType.RUN_FINISHED,
        EventType.RUN_STARTED,
        EventType.AGENT_MESSAGE,
        EventType.RUN_FINISHED,
    ]

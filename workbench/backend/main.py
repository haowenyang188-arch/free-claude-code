"""智能体协作工作台后端主服务"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast

import uvicorn
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

if __package__:
    from .agents.base import BaseAgentAdapter
    from .agents.claude_adapter import ClaudeCodeAdapter
    from .agents.codex_adapter import CodexAdapter
    from .agents.dsh_adapter import DeepSeekHarnessAdapter
    from .models import (
        Agent,
        AgentStatus,
        ControlRequest,
        CreateTaskRequest,
        Event,
        EventType,
        Run,
        RunStatus,
        SendMessageRequest,
        Task,
        TaskStatus,
    )
    from .runtime.approval import (
        ApprovalIntegrityError,
        ApprovalManager,
        ApprovalRecord,
        CommandIntent,
        CommandSyntaxError,
    )
    from .runtime.auth import WorkbenchAuth
    from .runtime.events import EventEnvelope, EventLog
    from .runtime.jobs import ApprovalExecutor, JobRecord, JobRuntime, JobRuntimeError
    from .runtime.state import StateStore, StateStoreError
    from .runtime.workspace import WorkspacePolicy, WorkspacePolicyError
else:  # Support ``python workbench/backend/main.py`` as a local entry point.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from workbench.backend.agents.base import BaseAgentAdapter
    from workbench.backend.agents.claude_adapter import ClaudeCodeAdapter
    from workbench.backend.agents.codex_adapter import CodexAdapter
    from workbench.backend.agents.dsh_adapter import DeepSeekHarnessAdapter
    from workbench.backend.models import (
        Agent,
        AgentStatus,
        ControlRequest,
        CreateTaskRequest,
        Event,
        EventType,
        Run,
        RunStatus,
        SendMessageRequest,
        Task,
        TaskStatus,
    )
    from workbench.backend.runtime.approval import (
        ApprovalIntegrityError,
        ApprovalManager,
        ApprovalRecord,
        CommandIntent,
        CommandSyntaxError,
    )
    from workbench.backend.runtime.auth import WorkbenchAuth
    from workbench.backend.runtime.events import EventEnvelope, EventLog
    from workbench.backend.runtime.jobs import (
        ApprovalExecutor,
        JobRecord,
        JobRuntime,
        JobRuntimeError,
    )
    from workbench.backend.runtime.state import StateStore, StateStoreError
    from workbench.backend.runtime.workspace import (
        WorkspacePolicy,
        WorkspacePolicyError,
    )


class _SessionAwareAdapter(Protocol):
    session_id: str | None


class WorkbenchService:
    """工作台服务"""

    # A process restart cannot reattach an adapter subprocess. Keep these runs
    # addressable, but expose them as paused/stale instead of pretending they
    # are still executing.
    _INTERRUPTED_RUN_STATUSES = frozenset(
        {
            RunStatus.STARTED,
            RunStatus.RUNNING,
            RunStatus.WAITING_HUMAN,
        }
    )

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        event_log_path: str | Path | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        default_root = Path(__file__).resolve().parents[2]
        self.workspace_policy = WorkspacePolicy(
            workspace_root
            or os.environ.get("WORKBENCH_WORKSPACE_ROOT", str(default_root))
        )
        self.event_log = EventLog(
            event_log_path
            or os.environ.get(
                "WORKBENCH_EVENT_LOG",
                str(self.workspace_policy.root / ".workbench" / "events.jsonl"),
            )
        )
        self.state_store = StateStore(
            state_path
            or os.environ.get(
                "WORKBENCH_STATE",
                str(
                    Path.home()
                    / ".local"
                    / "state"
                    / "free-claude-code"
                    / "workbench"
                    / "state.json"
                ),
            )
        )
        # Approval and job ownership are process-local. Grants do not survive
        # a restart, and PIDs are registered only after an approved spawn.
        self.approvals = ApprovalManager()
        self.jobs = JobRuntime()
        self.executor = ApprovalExecutor(self.approvals, self.jobs)
        self.agents: dict[str, BaseAgentAdapter] = {}
        self.tasks: dict[str, Task] = {}
        self.runs: dict[str, Run] = {}
        self.events: dict[str, Event] = {}
        self.event_envelopes: dict[str, EventEnvelope] = {}
        self._run_provenance: dict[str, tuple[str | None, str | None, str | None]] = {}
        self.websocket_connections: list[WebSocket] = []
        self._restore_state()

    def _restore_state(self) -> None:
        try:
            snapshot = self.state_store.load()
            self.tasks = {
                task.id: task
                for task in (Task.model_validate(item) for item in snapshot["tasks"])
            }
            self.runs = {
                run.id: run
                for run in (Run.model_validate(item) for item in snapshot["runs"])
            }
            changed = self._mark_interrupted_runs_stale()
            if changed:
                self._persist_state_sync()
        except (StateStoreError, ValueError, TypeError) as exc:
            raise StateStoreError("workbench state snapshot failed validation") from exc

    def _mark_interrupted_runs_stale(self) -> bool:
        """Mark in-flight runs as paused after a process restart.

        The event log remains append-only: recovery metadata belongs to the
        durable run snapshot and does not create a synthetic event or consume
        a replay sequence number.
        """
        recovered_at = datetime.now().isoformat()
        changed = False
        interrupted_task_ids: set[str] = set()

        for run in self.runs.values():
            if run.status not in self._INTERRUPTED_RUN_STATUSES:
                continue
            run.status = RunStatus.PAUSED
            run_metadata = dict(run.metadata)
            run_metadata.update(
                {
                    "stale": True,
                    "recovery_state": "stale",
                    "recovery_reason": "service_restart",
                    "recovered_at": recovered_at,
                }
            )
            run.metadata = run_metadata
            interrupted_task_ids.add(run.task_id)
            changed = True

        for task in self.tasks.values():
            has_interrupted_run = task.id in interrupted_task_ids
            task_is_active = task.status in {
                TaskStatus.RUNNING,
                TaskStatus.WAITING_HUMAN,
            }
            if not (has_interrupted_run or task_is_active):
                continue
            if task.status is not TaskStatus.PAUSED:
                task.status = TaskStatus.PAUSED
                changed = True
            metadata = dict(task.metadata)
            metadata.update(
                {
                    "stale": True,
                    "recovery_state": "stale",
                    "recovery_reason": "service_restart",
                    "recovered_at": recovered_at,
                }
            )
            if metadata != task.metadata:
                task.metadata = metadata
                changed = True
            task.updated_at = datetime.now()

        return changed

    async def _persist_state(self) -> None:
        tasks = [task.model_dump(mode="json") for task in self.tasks.values()]
        runs = [run.model_dump(mode="json") for run in self.runs.values()]
        await asyncio.to_thread(self.state_store.save, tasks, runs)

    def _persist_state_sync(self) -> None:
        tasks = [task.model_dump(mode="json") for task in self.tasks.values()]
        runs = [run.model_dump(mode="json") for run in self.runs.values()]
        self.state_store.save(tasks, runs)

    async def _rebind_restored_agents(self) -> None:
        available: dict[Any, str] = {}
        for agent_id, adapter in self.agents.items():
            if adapter.status is AgentStatus.ONLINE:
                available.setdefault(adapter.agent_type, agent_id)
        changed = False
        for task in self.tasks.values():
            if task.agent_type is None:
                continue
            agent_id = available.get(task.agent_type)
            if agent_id is None:
                if task.status is TaskStatus.RUNNING:
                    task.status = TaskStatus.PAUSED
                    changed = True
                continue
            if task.agent_id != agent_id:
                task.agent_id = agent_id
                changed = True
            for run_id in task.runs:
                run = self.runs.get(run_id)
                if run is not None and run.agent_id != agent_id:
                    run.agent_id = agent_id
                    changed = True
        if changed:
            await self._persist_state()

    async def initialize(self):
        """初始化服务"""
        for adapter_type in (
            ClaudeCodeAdapter,
            CodexAdapter,
            DeepSeekHarnessAdapter,
        ):
            agent_id = str(uuid.uuid4())
            try:
                if adapter_type is CodexAdapter:
                    adapter = adapter_type(
                        agent_id,
                        use_app_server=True,
                        approval_manager=self.approvals,
                    )
                else:
                    adapter = adapter_type(agent_id)
            except Exception as exc:
                print(f"✗ {adapter_type.__name__} initialization failed: {exc!s}")
                continue
            adapter.set_event_callback(self._handle_event)
            if await adapter.initialize():
                self.agents[agent_id] = adapter
                print(f"✓ {adapter.agent_type.value} Agent initialized: {agent_id}")
            else:
                print(f"✗ {adapter.agent_type.value} Agent initialization failed")
        await self._rebind_restored_agents()

    async def _handle_event(self, event: Event):
        """处理Agent事件"""
        # 从 adapter 获取 session_id
        session_id = self._session_id_for_run(event.run_id)
        if event.type is EventType.RUN_STARTED:
            self._run_provenance.setdefault(
                event.run_id, self._current_provenance_for_run(event.run_id)
            )
        runtime_kind, agent_profile_id, generation = self._run_provenance.get(
            event.run_id, self._current_provenance_for_run(event.run_id)
        )

        # 如果事件中包含 session_id，提取并持久化
        if event.run_id in self.runs:
            run = self.runs[event.run_id]
            adapter = self.agents.get(run.agent_id)
            adapter_session_id = getattr(adapter, "session_id", None)
            if isinstance(adapter_session_id, str) and adapter_session_id:
                session_id = adapter_session_id
                run.metadata["session_id"] = session_id

        envelope = await asyncio.to_thread(
            self.event_log.append,
            run_id=event.run_id,
            event_type=event.type.value,
            payload=event.data,
            backend=self._backend_for_event(event),
            session_id=session_id,
            runtime_kind=runtime_kind,
            agent_profile_id=agent_profile_id,
            generation=generation,
        )
        event.data = dict(envelope.payload)
        message = event.data.get("message")
        event.message = message if isinstance(message, str) else None
        self.events[event.id] = event
        self.event_envelopes[event.id] = envelope

        # 更新Run
        if event.run_id in self.runs:
            run = self.runs[event.run_id]
            if event.id not in run.events:
                run.events.append(event.id)

            # 更新Run状态
            if event.type is EventType.RUN_STARTED:
                run.status = RunStatus.RUNNING
            elif event.type is EventType.RUN_FINISHED:
                run.status = RunStatus.COMPLETED
                run.completed_at = datetime.now()
            elif event.type is EventType.RUN_FAILED:
                run.status = RunStatus.FAILED
                run.completed_at = datetime.now()
                run.error = event.data.get("error")
            elif event.type is EventType.RUN_PAUSED:
                run.status = RunStatus.PAUSED
            elif event.type is EventType.RUN_CANCELLED:
                run.status = RunStatus.CANCELLED
                run.completed_at = datetime.now()
            elif event.type is EventType.USER_INPUT_REQUIRED:
                run.status = RunStatus.WAITING_HUMAN

            # 更新Task状态
            task_id = run.task_id
            if task_id in self.tasks:
                task = self.tasks[task_id]
                if run.status == RunStatus.COMPLETED:
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = datetime.now()
                elif run.status == RunStatus.FAILED:
                    task.status = TaskStatus.FAILED
                elif run.status == RunStatus.PAUSED:
                    task.status = TaskStatus.PAUSED
                elif run.status == RunStatus.CANCELLED:
                    task.status = TaskStatus.CANCELLED
                elif run.status == RunStatus.WAITING_HUMAN:
                    task.status = TaskStatus.WAITING_HUMAN
                task.updated_at = datetime.now()

        await self._persist_state()

        # 广播事件到所有WebSocket连接
        await self._broadcast_event(event)

    def _backend_for_event(self, event: Event) -> str:
        run = self.runs.get(event.run_id)
        if run:
            adapter = self.agents.get(run.agent_id)
            if adapter:
                return adapter.agent_type.value
        return "workbench"

    def _session_id_for_run(self, run_id: str) -> str | None:
        run = self.runs.get(run_id)
        session_id = run.metadata.get("session_id") if run else None
        return session_id if isinstance(session_id, str) and session_id else None

    def _provenance_for_run(
        self, run_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Return the immutable provenance captured for a run."""
        return self._run_provenance.get(
            run_id, self._current_provenance_for_run(run_id)
        )

    def _current_provenance_for_run(
        self, run_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Read current adapter identity when a run has no captured provenance."""
        run = self.runs.get(run_id)
        if run is None:
            return None, None, None
        adapter = self.agents.get(run.agent_id)
        if adapter is None:
            return None, run.agent_id, None
        generation = getattr(adapter, "generation", None)
        if not isinstance(generation, str) or not generation:
            session = getattr(adapter, "session", None)
            generation = getattr(session, "generation", None)
        return (
            adapter.agent_type.value,
            adapter.agent_id,
            generation if isinstance(generation, str) and generation else None,
        )

    def _serialize_event(self, event: Event) -> dict[str, Any]:
        result = event.model_dump(mode="json")
        envelope = self.event_envelopes.get(event.id)
        if envelope:
            result.update(
                {
                    "sequence": envelope.sequence,
                    "backend": envelope.backend,
                    "session_id": envelope.session_id,
                    "runtime_kind": envelope.runtime_kind,
                    "agent_profile_id": envelope.agent_profile_id,
                    "generation": envelope.generation,
                }
            )
        return result

    @staticmethod
    def _legacy_event_from_envelope(envelope: EventEnvelope) -> dict[str, Any]:
        payload = dict(envelope.payload)
        message = payload.get("message")
        return {
            "id": envelope.id,
            "run_id": envelope.run_id,
            "type": envelope.event_type,
            "timestamp": envelope.timestamp.isoformat(),
            "data": payload,
            "message": message if isinstance(message, str) else None,
            "sequence": envelope.sequence,
            "backend": envelope.backend,
            "session_id": envelope.session_id,
            "runtime_kind": envelope.runtime_kind,
            "agent_profile_id": envelope.agent_profile_id,
            "generation": envelope.generation,
        }

    def replay_events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        """Return durable events using a monotonic per-run cursor."""
        return [
            event.to_mapping() for event in self.event_log.replay(run_id, after=after)
        ]

    async def _broadcast_event(self, event: Event):
        """广播事件到所有WebSocket连接"""
        message = {"type": "event", "data": self._serialize_event(event)}

        disconnected = []
        for ws in self.websocket_connections:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(ws)

        # 移除断开的连接
        for ws in disconnected:
            if ws in self.websocket_connections:
                self.websocket_connections.remove(ws)

    def get_agents(self) -> list[Agent]:
        """获取所有Agent"""
        agents = []
        for agent_id, adapter in self.agents.items():
            agent = Agent(
                id=agent_id,
                name=f"{adapter.agent_type.value}-{agent_id[:8]}",
                type=adapter.agent_type,
                status=adapter.status,
                current_task_id=adapter.current_run_id,
            )
            agents.append(agent)
        return agents

    async def create_task(self, request: CreateTaskRequest) -> Task:
        """创建任务"""
        task_id = str(uuid.uuid4())

        # 查找可用Agent
        agent_id = None
        for aid, adapter in self.agents.items():
            if (
                adapter.agent_type == request.agent_type
                and adapter.status == AgentStatus.ONLINE
            ):
                agent_id = aid
                break

        if not agent_id:
            raise HTTPException(
                status_code=400,
                detail=f"No available agent of type {request.agent_type}",
            )

        try:
            workspace = str(
                self.workspace_policy.resolve(request.workspace_path or ".")
            )
        except WorkspacePolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        task = Task(
            id=task_id,
            title=request.title,
            description=request.description,
            agent_id=agent_id,
            agent_type=request.agent_type,
            workspace_path=workspace,
            status=TaskStatus.PENDING,
        )

        self.tasks[task_id] = task
        await self._persist_state()
        return task

    async def start_task(self, task_id: str) -> Run:
        """启动任务"""
        if task_id not in self.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        task = self.tasks[task_id]

        try:
            task.workspace_path = str(
                self.workspace_policy.resolve(task.workspace_path)
            )
        except WorkspacePolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not task.agent_id or task.agent_id not in self.agents:
            raise HTTPException(status_code=400, detail="No agent assigned to task")

        adapter = self.agents[task.agent_id]

        # 创建Run
        run_id = str(uuid.uuid4())
        run = Run(
            id=run_id, task_id=task_id, agent_id=task.agent_id, status=RunStatus.STARTED
        )

        self.runs[run_id] = run
        task.runs.append(run_id)
        task.status = TaskStatus.RUNNING
        await self._persist_state()

        # 启动Agent任务
        success = await adapter.start_task(
            run_id, task.description, task.workspace_path
        )

        if not success:
            run.status = RunStatus.FAILED
            task.status = TaskStatus.FAILED
            await self._persist_state()
            raise HTTPException(status_code=500, detail="Failed to start agent task")

        if run.status is RunStatus.STARTED:
            run.status = RunStatus.RUNNING
        await self._persist_state()
        return run

    async def send_message(self, request: SendMessageRequest) -> bool:
        """发送消息给Agent"""
        if request.run_id not in self.runs:
            raise HTTPException(status_code=404, detail="Run not found")

        run = self.runs[request.run_id]

        if run.agent_id not in self.agents:
            raise HTTPException(status_code=404, detail="Agent not found")

        adapter = self.agents[run.agent_id]

        # 恢复持久化的 session_id
        adapter_session_id = getattr(adapter, "session_id", None)
        stored_session_id = run.metadata.get("session_id")
        if (
            adapter_session_id is None
            and isinstance(stored_session_id, str)
            and stored_session_id
        ):
            cast(_SessionAwareAdapter, adapter).session_id = stored_session_id

        last_run_id = getattr(adapter, "last_run_id", None)
        if adapter.current_run_id not in (None, request.run_id) or (
            last_run_id is not None and last_run_id != request.run_id
        ):
            return False
        return await adapter.send_message(request.message)

    async def control_run(self, request: ControlRequest) -> bool:
        """控制Run执行"""
        if request.run_id not in self.runs:
            raise HTTPException(status_code=404, detail="Run not found")

        run = self.runs[request.run_id]

        if run.agent_id not in self.agents:
            raise HTTPException(status_code=404, detail="Agent not found")

        adapter = self.agents[run.agent_id]

        if request.action != "retry" and adapter.current_run_id not in (
            None,
            request.run_id,
        ):
            raise HTTPException(
                status_code=409,
                detail="Run is no longer the adapter's active run",
            )

        if request.action == "pause":
            return await adapter.pause()
        elif request.action == "resume":
            return await adapter.resume()
        elif request.action == "cancel":
            return await adapter.cancel()
        elif request.action == "retry":
            # 重新启动任务
            task = self.tasks[run.task_id]
            await self.start_task(task.id)
            return True
        else:
            raise HTTPException(
                status_code=400, detail=f"Unknown action: {request.action}"
            )

    async def cleanup(self):
        """清理服务"""
        await self.jobs.close()
        for adapter in self.agents.values():
            await adapter.cleanup()

    def _approval_intent(
        self,
        payload: ApprovalCommandRequest,
        *,
        session_id: str | None = None,
        call_id: str | None = None,
    ) -> CommandIntent:
        resolved_session_id = session_id or payload.session_id
        resolved_call_id = call_id or payload.call_id
        if (session_id is not None and payload.session_id != session_id) or (
            call_id is not None and payload.call_id != call_id
        ):
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        try:
            workspace = self.workspace_policy.resolve(payload.cwd)
        except WorkspacePolicyError as exc:
            raise ValueError("cwd must stay inside the Workbench workspace") from exc
        return CommandIntent.create(
            session_id=resolved_session_id,
            call_id=resolved_call_id,
            command=payload.command,
            argv=payload.argv,
            cwd=workspace,
            requested_permission=payload.requested_permission,
        )

    async def request_approval(self, payload: ApprovalCommandRequest) -> ApprovalRecord:
        intent = self._approval_intent(payload)
        return await self.approvals.request(
            intent, approval_timeout_seconds=payload.approval_timeout_seconds
        )

    async def get_approval(self, session_id: str, call_id: str) -> ApprovalRecord:
        return await self.approvals.get(session_id=session_id, call_id=call_id)

    async def decide_approval(
        self,
        session_id: str,
        call_id: str,
        command_hash: str,
        decision: str,
    ) -> ApprovalRecord:
        if decision == "approve":
            return await self.approvals.approve(
                session_id=session_id, call_id=call_id, command_hash=command_hash
            )
        if decision == "reject":
            return await self.approvals.reject(
                session_id=session_id, call_id=call_id, command_hash=command_hash
            )
        if decision == "cancel":
            return await self.approvals.cancel(
                session_id=session_id, call_id=call_id, command_hash=command_hash
            )
        raise ValueError("unknown approval decision")

    async def execute_approval(
        self,
        session_id: str,
        call_id: str,
        payload: ApprovalExecuteRequest,
    ) -> JobRecord:
        intent = self._approval_intent(payload, session_id=session_id, call_id=call_id)
        return await self.executor.execute(
            intent,
            background=payload.background,
            port=payload.port,
            health_url=payload.health_url,
            job_wait_timeout_seconds=payload.job_wait_timeout_seconds,
            process_timeout_seconds=payload.process_timeout_seconds,
        )


# 全局服务实例
service = WorkbenchService()
auth = WorkbenchAuth(os.environ.get("WORKBENCH_AUTH_TOKEN"))


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class ApprovalCommandRequest(BaseModel):
    """Validated command identity submitted to the Workbench approval API."""

    session_id: str = Field(min_length=1, max_length=256)
    call_id: str = Field(min_length=1, max_length=256)
    command: str | None = Field(default=None, max_length=8192)
    argv: list[str] | None = Field(default=None, min_length=1, max_length=256)
    cwd: str = Field(min_length=1, max_length=4096)
    requested_permission: str = Field(min_length=1, max_length=256)
    approval_timeout_seconds: float = Field(default=300.0, ge=0.0, le=3600.0)


class ApprovalDecisionRequest(BaseModel):
    command_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"
    )


class ApprovalExecuteRequest(ApprovalCommandRequest):
    """Execution parameters; command identity must match the approved record."""

    background: bool = False
    port: int | None = Field(default=None, ge=1, le=65535)
    health_url: str | None = Field(default=None, max_length=2048)
    job_wait_timeout_seconds: float = Field(default=5.0, ge=0.0, le=3600.0)
    process_timeout_seconds: float | None = Field(default=None, gt=0.0, le=86400.0)


def _approval_payload(record: ApprovalRecord) -> dict[str, Any]:
    """Serialize an approval without process identity or secret command output."""
    return {
        "session_id": record.session_id,
        "call_id": record.call_id,
        "normalized_command": record.normalized_command,
        "argv": list(record.argv),
        "cwd": str(record.cwd),
        "requested_permission": record.requested_permission,
        "command_hash": record.command_hash,
        "risk": record.risk.value,
        "status": record.status.value,
        "created_at": record.created_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "approved_at": record.approved_at.isoformat() if record.approved_at else None,
        "consumed_at": record.consumed_at.isoformat() if record.consumed_at else None,
        "reason": record.reason,
    }


def _job_payload(record: JobRecord) -> dict[str, Any]:
    return {
        "job_id": record.job_id,
        "call_id": record.call_id,
        "command_hash": record.command_hash,
        "pid": record.pid,
        "status": record.status.value,
        "background": record.background,
        "port": record.port,
        "health_url": record.health_url,
        "started_at": record.started_at.isoformat(),
        "ready": record.ready,
        "exit_code": record.exit_code,
    }


def _approval_http_exception(
    exc: Exception, *, default_status: int = 409
) -> HTTPException:
    code = str(exc).strip() or "approval_unavailable"
    if isinstance(exc, (CommandSyntaxError, ValueError)):
        code = "invalid_command"
        default_status = 400
    elif code in {
        "approval_integrity_mismatch",
        "approval_timeout",
        "approval_unavailable",
    }:
        default_status = 409
    elif code == "process_failed":
        default_status = 500
    return HTTPException(
        status_code=default_status,
        detail={"code": code, "message": code},
    )


def _approval_error_response(
    exc: Exception, *, default_status: int = 409
) -> JSONResponse:
    error = _approval_http_exception(exc, default_status=default_status)
    detail = (
        error.detail
        if isinstance(error.detail, dict)
        else {"message": str(error.detail)}
    )
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": detail.get("code", "approval_unavailable"),
                "message": detail.get("message", "approval_unavailable"),
            }
        },
    )


def _remote_host(request: Request) -> str | None:
    return request.client.host if request.client else None


def _request_authenticated(request: Request) -> bool:
    authorization = request.headers.get("authorization")
    cookie = request.cookies.get(auth.cookie_name)
    if auth.enabled:
        return auth.verify(authorization, cookie)
    return auth.is_request_allowed(authorization, _remote_host(request))


def _cors_origins() -> list[str]:
    raw = os.environ.get(
        "WORKBENCH_ALLOWED_ORIGINS",
        "http://127.0.0.1:3000,http://localhost:3000",
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动
    print("🚀 Initializing Workbench Service...")
    await service.initialize()
    print("✓ Workbench Service ready")

    yield

    # 关闭
    print("🛑 Shutting down Workbench Service...")
    await service.cleanup()
    print("✓ Cleanup complete")


# 创建FastAPI应用
app = FastAPI(
    title="智能体协作工作台",
    description="可视化、可操控的多智能体协作工作台",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_workbench_auth(request: Request, call_next):
    """Protect API routes while leaving login/status available anonymously."""
    path = request.url.path
    public_paths = {"/api/auth/login", "/api/auth/status"}
    if (
        path.startswith("/api/")
        and path not in public_paths
        and not _request_authenticated(request)
    ):
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "AUTH_REQUIRED", "message": "Login required"}},
        )
    return await call_next(request)


# API路由


@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {
        "authenticated": _request_authenticated(request),
        "required": auth.enabled,
    }


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response, payload: LoginRequest):
    if auth.enabled and not auth.verify(f"Bearer {payload.token}", None):
        raise HTTPException(status_code=401, detail="Invalid workbench token")
    if auth.enabled:
        response.set_cookie(
            auth.cookie_name,
            payload.token,
            max_age=12 * 60 * 60,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
    return {"authenticated": True}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(auth.cookie_name, path="/")
    return {"authenticated": False}


@app.post("/api/approvals")
async def request_approval(payload: ApprovalCommandRequest):
    """Create one concrete approval request or classify it automatically."""
    try:
        record = await service.request_approval(payload)
    except (ApprovalIntegrityError, CommandSyntaxError, ValueError) as exc:
        return _approval_error_response(exc, default_status=400)
    return _approval_payload(record)


@app.get("/api/approvals/{session_id}/{call_id}")
async def get_approval(session_id: str, call_id: str):
    try:
        record = await service.get_approval(session_id, call_id)
    except ApprovalIntegrityError as exc:
        return _approval_error_response(exc)
    return _approval_payload(record)


@app.post("/api/approvals/{session_id}/{call_id}/execute")
async def execute_approval(
    session_id: str, call_id: str, payload: ApprovalExecuteRequest
):
    try:
        job = await service.execute_approval(session_id, call_id, payload)
    except (
        ApprovalIntegrityError,
        CommandSyntaxError,
        JobRuntimeError,
        ValueError,
    ) as exc:
        return _approval_error_response(exc)
    return _job_payload(job)


@app.post("/api/approvals/{session_id}/{call_id}/{decision}")
async def decide_approval(
    session_id: str, call_id: str, decision: str, payload: ApprovalDecisionRequest
):
    if decision not in {"approve", "reject", "cancel"}:
        return JSONResponse(
            status_code=400,
            content={
                "error": {"code": "invalid_decision", "message": "invalid_decision"}
            },
        )
    try:
        record = await service.decide_approval(
            session_id, call_id, payload.command_hash, decision
        )
    except (ApprovalIntegrityError, ValueError) as exc:
        return _approval_error_response(exc)
    return _approval_payload(record)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    try:
        job = await service.jobs.get(job_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "job_unavailable", "message": "job_unavailable"}},
        ) from exc
    return _job_payload(job)


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str):
    try:
        job = await service.jobs.stop(job_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "job_unavailable", "message": "job_unavailable"}},
        ) from exc
    return _job_payload(job)


@app.get("/api/agents")
async def list_agents():
    """获取所有Agent"""
    return {"agents": [agent.model_dump() for agent in service.get_agents()]}


@app.get("/api/tasks")
async def list_tasks():
    """获取所有任务"""
    return {"tasks": [task.model_dump() for task in service.tasks.values()]}


@app.post("/api/tasks")
async def create_task(request: CreateTaskRequest):
    """创建任务"""
    task = await service.create_task(request)
    return task.model_dump()


@app.post("/api/tasks/{task_id}/start")
async def start_task(task_id: str):
    """启动任务"""
    run = await service.start_task(task_id)
    return run.model_dump()


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """获取任务详情"""
    if task_id not in service.tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return service.tasks[task_id].model_dump()


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """获取Run详情"""
    if run_id not in service.runs:
        raise HTTPException(status_code=404, detail="Run not found")

    run = service.runs[run_id]
    events = [
        service._serialize_event(service.events[eid])
        for eid in run.events
        if eid in service.events
    ]
    if not events:
        events = [
            service._legacy_event_from_envelope(item)
            for item in service.event_log.replay(run_id)
        ]

    return {**run.model_dump(), "events": events}


@app.get("/api/runs/{run_id}/events")
async def replay_run_events(run_id: str, after: int = 0):
    """Replay durable run events after a sequence cursor."""
    try:
        events = service.replay_events(run_id, after=after)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"events": events}


@app.post("/api/runs/{run_id}/message")
async def send_message(run_id: str, request: SendMessageRequest):
    """发送消息给Agent"""
    request.run_id = run_id
    success = await service.send_message(request)
    return {"success": success}


@app.post("/api/runs/{run_id}/control")
async def control_run(run_id: str, request: ControlRequest):
    """控制Run执行"""
    request.run_id = run_id
    success = await service.control_run(request)
    return {"success": success}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket连接"""
    remote_host = websocket.client.host if websocket.client else None
    authorization = websocket.headers.get("authorization")
    cookie = websocket.cookies.get(auth.cookie_name)
    if auth.enabled:
        authenticated = auth.verify(authorization, cookie)
    else:
        authenticated = auth.is_request_allowed(None, remote_host)
    if not authenticated:
        await websocket.close(code=4401, reason="Login required")
        return
    await websocket.accept()
    service.websocket_connections.append(websocket)

    try:
        # 发送当前状态
        await websocket.send_json(
            {
                "type": "init",
                "data": {
                    "agents": [
                        agent.model_dump(mode="json") for agent in service.get_agents()
                    ],
                    "tasks": [
                        task.model_dump(mode="json") for task in service.tasks.values()
                    ],
                },
            }
        )

        # 保持连接
        while True:
            await websocket.receive_text()
            # 可以处理客户端发送的消息

    except WebSocketDisconnect:
        if websocket in service.websocket_connections:
            service.websocket_connections.remove(websocket)


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, log_level="info")

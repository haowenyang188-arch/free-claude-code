"""智能体协作工作台后端主服务"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import uvicorn
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from providers.common.identity import RuntimeIdentity

if __package__:
    from .agents.base import BaseAgentAdapter
    from .agents.claude_adapter import ClaudeCodeAdapter
    from .agents.codex_adapter import CodexAdapter
    from .agents.dsh_adapter import DeepSeekHarnessAdapter
    from .artifacts.store import ArtifactStoreError, FileArtifactStore
    from .domain.models import (
        Artifact,
        Goal,
        GoalStatus,
        SopDefinition,
        SopRun,
        SopRunStatus,
        StepDefinition,
        SubagentAssignment,
    )
    from .domain.models import (
        Task as SopTask,
    )
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
    from .persistence.store import JsonWorkflowStore
    from .runtime.approval import (
        ApprovalIntegrityError,
        ApprovalManager,
        ApprovalRecord,
        ApprovalReference,
        CommandIntent,
        CommandSyntaxError,
    )
    from .runtime.auth import WorkbenchAuth
    from .runtime.events import EventEnvelope, EventLog
    from .runtime.jobs import ApprovalExecutor, JobRecord, JobRuntime, JobRuntimeError
    from .runtime.state import StateStore, StateStoreError
    from .runtime.workspace import WorkspacePolicy, WorkspacePolicyError
    from .sop_models import (
        ArtifactResponse,
        AttemptChainResponse,
        AttemptResponse,
        HandoffResponse,
        ReviewEvidenceResponse,
        SopControlRequest,
        SopEventResponse,
        SopRunStatusResponse,
        StartSopRunRequest,
        StartSopRunResponse,
        StepRunResponse,
        TaskResponse,
    )
    from .validation.always_accept import AlwaysAcceptValidator
    from .workflow.context_builder import ContextPackageBuilder
    from .workflow.engine import WorkflowEngine
    from .workflow.orchestrator import AutoOrchestrator
    from .workflow.runner_factory import create_runners
else:  # Support ``python workbench/backend/main.py`` as a local entry point.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from workbench.backend.agents.base import BaseAgentAdapter
    from workbench.backend.agents.claude_adapter import ClaudeCodeAdapter
    from workbench.backend.agents.codex_adapter import CodexAdapter
    from workbench.backend.agents.dsh_adapter import DeepSeekHarnessAdapter
    from workbench.backend.artifacts.store import ArtifactStoreError, FileArtifactStore
    from workbench.backend.domain.models import (
        Artifact,
        Goal,
        GoalStatus,
        SopDefinition,
        SopRun,
        SopRunStatus,
        StepDefinition,
        SubagentAssignment,
    )
    from workbench.backend.domain.models import (
        Task as SopTask,
    )
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
    from workbench.backend.persistence.store import JsonWorkflowStore
    from workbench.backend.runtime.approval import (
        ApprovalIntegrityError,
        ApprovalManager,
        ApprovalRecord,
        ApprovalReference,
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
    from workbench.backend.sop_models import (
        ArtifactResponse,
        HandoffResponse,
        SopControlRequest,
        SopEventResponse,
        SopRunStatusResponse,
        StartSopRunRequest,
        StartSopRunResponse,
        StepRunResponse,
        TaskResponse,
    )
    from workbench.backend.validation.always_accept import AlwaysAcceptValidator
    from workbench.backend.workflow.context_builder import ContextPackageBuilder
    from workbench.backend.workflow.engine import WorkflowEngine
    from workbench.backend.workflow.orchestrator import AutoOrchestrator
    from workbench.backend.workflow.runner_factory import create_runners


class _SessionAwareAdapter(Protocol):
    session_id: str | None


_WORKBENCH_PROVIDER_BY_RUNTIME: dict[str, str] = {
    "codex": "codex_cli",
    "claude_code": "claude_cli",
    "deepseek_harness": "deepseek-official",
}


def _canonical_workbench_provider(runtime_kind: str | None) -> str:
    """Map an adapter kind to its host-owned provider route."""
    if not isinstance(runtime_kind, str) or not runtime_kind.strip():
        return "workbench"
    normalized = runtime_kind.strip().lower()
    return _WORKBENCH_PROVIDER_BY_RUNTIME.get(normalized, normalized)


def _event_identity_for_workbench(
    identity: RuntimeIdentity | None,
    *,
    run_id: str,
    provider: str | None,
    agent_id: str | None,
    generation: str | None,
    session_id: str | None,
) -> RuntimeIdentity:
    """Keep host provenance authoritative while retaining runtime IDs."""
    explicit = identity if isinstance(identity, RuntimeIdentity) else RuntimeIdentity()
    lifecycle = RuntimeIdentity(
        runtime_id=explicit.runtime_id,
        thread_id=explicit.thread_id,
        turn_id=explicit.turn_id,
        item_id=explicit.item_id,
        approval_id=explicit.approval_id,
        one_shot_id=explicit.one_shot_id,
        tool_id=explicit.tool_id,
        call_id=explicit.call_id,
        message_id=explicit.message_id,
    )
    fallback = RuntimeIdentity(
        provider=_canonical_workbench_provider(provider),
        agent_id=agent_id,
        generation=generation,
        session_id=session_id,
        run_id=run_id,
    )
    merged = lifecycle.merge(fallback)
    values = merged.to_mapping()
    # Provider payloads cannot redirect host-owned provenance.
    values["provider"] = fallback.provider
    values["agent_id"] = fallback.agent_id
    values["generation"] = fallback.generation
    values["session_id"] = fallback.session_id
    values["run_id"] = run_id
    return RuntimeIdentity(**values)


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

        # SOP workflow components
        self._init_sop_workflow()

        self._restore_state()

    def _init_sop_workflow(self) -> None:
        """Initialize SOP workflow components."""
        sop_workspace = self.workspace_policy.root / ".workbench" / "sop"
        sop_workspace.mkdir(parents=True, exist_ok=True)

        # Workflow persistence
        self.workflow_store = JsonWorkflowStore(
            state_path=sop_workspace / "workflow_state.json",
            event_path=sop_workspace / "workflow_events.jsonl",
        )
        self.artifact_store = FileArtifactStore(
            root=sop_workspace / "artifacts",
        )

        # Runtime adapters from RUNTIME_MODE (default fake; real fails closed)
        runners = create_runners(
            store=self.workflow_store,
            artifact_store=self.artifact_store,
            workspace_path=str(sop_workspace),
        )
        validator = AlwaysAcceptValidator()

        self.workflow_engine = WorkflowEngine(
            store=self.workflow_store,
            runners=runners,
            validator=validator,
            artifact_store=self.artifact_store,
        )
        self.context_builder = ContextPackageBuilder(
            store=self.workflow_store,
            artifact_store=self.artifact_store,
        )
        self.orchestrator = AutoOrchestrator(self.workflow_engine)

        # SOP definitions cache (will be loaded from storage)
        self.sop_definitions: dict[str, SopDefinition] = {}

        # Background SOP execution tasks
        self._background_sop_tasks: dict[str, asyncio.Task] = {}

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

    def _default_assignment_resolver(
        self, task: SopTask, step: StepDefinition
    ) -> SubagentAssignment:
        """Default assignment resolver: bind role to its runtime id.

        Role -> runtime mapping mirrors the RoleBinding contract
        (planner -> Claude, executor -> DSH, reviewer -> Codex).
        """
        runtime_by_role = {
            "claude": "claude",
            "dsh": "dsh",
            "codex": "codex",
            "planner": "claude",
            "executor": "dsh",
            "reviewer": "codex",
        }
        return SubagentAssignment(
            id=str(uuid.uuid4()),
            task_id=task.id,
            role_id=step.role_id,
            agent_instance_id="fake-agent",
            runtime_id=runtime_by_role.get(step.role_id, "default"),
        )

    async def _execute_sop_run_background(
        self, sop_run_id: str, goal: Goal, sop: SopDefinition
    ) -> None:
        """Execute SOP run in background with exception handling."""
        try:
            # Build context using the existing builder
            def build_context(task: SopTask, step: StepDefinition):
                return self.context_builder.build_for_task(
                    task=task, step=step, goal=goal, sop=sop
                )

            # Execute until gate (review or terminal state)
            await self.orchestrator.run_until_gate(
                sop_run_id,
                resolve_assignment=self._default_assignment_resolver,
                build_context=build_context,
            )

        except Exception as exc:
            # Persist execution failure to SopRun status
            try:
                run = self.workflow_store.get_entity("sop_runs", sop_run_id)
                run["status"] = SopRunStatus.FAILED.value
                run["metadata"] = run.get("metadata", {})
                run["metadata"]["error"] = str(exc)
                run["metadata"]["error_type"] = type(exc).__name__
                self.workflow_store.save_entity("sop_runs", run)

                # Emit failure event
                self.workflow_store.append_event(
                    stream_id=sop_run_id,
                    event_type="sop_failed",
                    payload={"error": str(exc), "error_type": type(exc).__name__},
                )
            except Exception:
                # Swallow persistence errors to avoid breaking background task cleanup
                pass
        finally:
            # Cleanup background task reference
            self._background_sop_tasks.pop(sop_run_id, None)

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
                elif adapter_type is ClaudeCodeAdapter:
                    adapter = adapter_type(
                        agent_id,
                        use_compatibility_bridge=True,
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
        # Workbench-owned run provenance is authoritative; provider event
        # metadata may add lifecycle ids but cannot redirect the event.
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

        event_identity = _event_identity_for_workbench(
            event.identity,
            run_id=event.run_id,
            provider=runtime_kind,
            agent_id=agent_profile_id,
            generation=generation,
            session_id=session_id,
        )
        envelope = await asyncio.to_thread(
            self.event_log.append,
            run_id=event.run_id,
            event_type=event.type.value,
            payload=event.data,
            backend=self._backend_for_event(event),
            session_id=session_id,
            runtime_kind=runtime_kind,
            agent_profile_id=agent_profile_id,
            generation=event_identity.generation or generation,
            identity=event_identity,
        )
        event.data = dict(envelope.payload)
        event.identity = RuntimeIdentity.from_mapping(envelope.to_mapping()["identity"])
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
        if event.identity is not None:
            result["identity"] = event.identity.to_mapping(include_unknown=False)
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
                    "identity": envelope.to_mapping()["identity"],
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
            "identity": envelope.to_mapping()["identity"],
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
        workspace_target = self._approval_workspace_target(payload, workspace)
        return CommandIntent.create(
            session_id=resolved_session_id,
            call_id=resolved_call_id,
            command=payload.command,
            argv=payload.argv,
            cwd=workspace,
            requested_permission=payload.requested_permission,
            provider=payload.provider,
            thread_id=payload.thread_id,
            turn_id=payload.turn_id,
            item_id=payload.item_id,
            approval_id=payload.approval_id,
            workspace_target=workspace_target,
            permission_scope=payload.permission_scope,
            patch_identity=payload.patch_identity,
        )

    def _approval_workspace_target(
        self, payload: ApprovalCommandRequest, workspace: Path
    ) -> Path:
        if payload.workspace_target is None:
            return workspace
        candidate = Path(payload.workspace_target).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                "workspace_target must reference an existing path"
            ) from exc
        try:
            resolved.relative_to(self.workspace_policy.root)
        except ValueError:
            self._validate_external_filesystem_scope(payload, resolved)
        return resolved

    @staticmethod
    def _validate_external_filesystem_scope(
        payload: ApprovalCommandRequest, target: Path
    ) -> None:
        scope = payload.permission_scope
        if not scope:
            raise ValueError("external workspace target requires filesystem scope")
        parts = scope.split(":", 2)
        if len(parts) != 3 or parts[:2] not in (
            ["filesystem", "read"],
            ["filesystem", "write"],
        ):
            raise ValueError("external workspace target requires filesystem scope")
        scoped_path = Path(parts[2]).expanduser().resolve(strict=False)
        try:
            scoped_path.relative_to(target)
        except ValueError as exc:
            raise ValueError(
                "filesystem scope must stay within external workspace target"
            ) from exc

    async def request_approval(self, payload: ApprovalCommandRequest) -> ApprovalRecord:
        intent = self._approval_intent(payload)
        return await self.approvals.request(
            intent, approval_timeout_seconds=payload.approval_timeout_seconds
        )

    async def get_approval(
        self,
        session_id: str,
        call_id: str,
        *,
        provider: str = "codex_cli",
        command_hash: str | None = None,
    ) -> ApprovalRecord:
        if command_hash is None:
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        return await self.approvals.get(
            session_id=session_id,
            call_id=call_id,
            command_hash=command_hash,
            provider=provider,
        )

    async def decide_approval(
        self,
        session_id: str,
        call_id: str,
        command_hash: str,
        decision: str,
        provider: str = "codex_cli",
        reference: ApprovalReference | None = None,
    ) -> ApprovalRecord:
        if reference is not None and (
            reference.provider != provider
            or reference.session_id != session_id
            or reference.call_id != call_id
            or reference.command_hash != command_hash.lower()
        ):
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        if decision == "approve":
            return await self.approvals.approve(
                reference,
                session_id=None if reference is not None else session_id,
                call_id=None if reference is not None else call_id,
                command_hash=None if reference is not None else command_hash,
                provider=provider,
            )
        if decision == "reject":
            return await self.approvals.reject(
                reference,
                session_id=None if reference is not None else session_id,
                call_id=None if reference is not None else call_id,
                command_hash=None if reference is not None else command_hash,
                provider=provider,
            )
        if decision == "cancel":
            return await self.approvals.cancel(
                reference,
                session_id=None if reference is not None else session_id,
                call_id=None if reference is not None else call_id,
                command_hash=None if reference is not None else command_hash,
                provider=provider,
            )
        raise ValueError("unknown approval decision")

    async def execute_approval(
        self,
        session_id: str,
        call_id: str,
        payload: ApprovalExecuteRequest,
    ) -> JobRecord:
        if (
            payload.one_shot_id is None
            or payload.command_hash is None
            or payload.session_id != session_id
            or payload.call_id != call_id
        ):
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        reference = ApprovalReference.create(
            provider=payload.provider,
            session_id=payload.session_id,
            thread_id=payload.thread_id,
            turn_id=payload.turn_id,
            item_id=payload.item_id,
            approval_id=payload.approval_id,
            call_id=payload.call_id,
            one_shot_id=payload.one_shot_id,
            command_hash=payload.command_hash,
        )
        record = await self.approvals.get(reference)
        if payload.background and record.requested_permission != "background_service":
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        return await self.executor.execute(
            record.intent,
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

    provider: str = Field(default="codex_cli", pattern=r"^(?:codex_cli|claude_cli)$")
    session_id: str = Field(min_length=1, max_length=256)
    call_id: str = Field(min_length=1, max_length=256)
    thread_id: str | None = Field(default=None, max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    item_id: str | None = Field(default=None, max_length=256)
    approval_id: str | None = Field(default=None, max_length=256)
    command: str | None = Field(default=None, max_length=8192)
    argv: list[str] | None = Field(default=None, min_length=1, max_length=256)
    cwd: str = Field(min_length=1, max_length=4096)
    workspace_target: str | None = Field(default=None, max_length=4096)
    requested_permission: str = Field(min_length=1, max_length=256)
    permission_scope: str | None = Field(default=None, max_length=65536)
    patch_identity: str | None = Field(default=None, max_length=65536)
    approval_timeout_seconds: float = Field(default=300.0, ge=0.0, le=3600.0)


class ApprovalDecisionRequest(BaseModel):
    provider: str = Field(default="codex_cli", pattern=r"^(?:codex_cli|claude_cli)$")
    command_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$"
    )
    one_shot_id: str | None = Field(default=None, min_length=1, max_length=128)
    thread_id: str | None = Field(default=None, max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    item_id: str | None = Field(default=None, max_length=256)
    approval_id: str | None = Field(default=None, max_length=256)


class ApprovalExecuteRequest(BaseModel):
    """Full immutable approval reference plus process execution options."""

    provider: str = Field(default="codex_cli", pattern=r"^(?:codex_cli|claude_cli)$")
    session_id: str = Field(min_length=1, max_length=256)
    call_id: str = Field(min_length=1, max_length=256)
    thread_id: str | None = Field(default=None, max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    item_id: str | None = Field(default=None, max_length=256)
    approval_id: str | None = Field(default=None, max_length=256)
    one_shot_id: str = Field(min_length=1, max_length=128)
    command_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )

    background: bool = False
    port: int | None = Field(default=None, ge=1, le=65535)
    health_url: str | None = Field(default=None, max_length=2048)
    job_wait_timeout_seconds: float = Field(default=5.0, ge=0.0, le=3600.0)
    process_timeout_seconds: float | None = Field(default=None, gt=0.0, le=86400.0)


def _approval_payload(record: ApprovalRecord) -> dict[str, Any]:
    """Serialize an approval without process identity or secret command output."""
    return {
        "provider": record.provider,
        "session_id": record.session_id,
        "call_id": record.call_id,
        "one_shot_id": record.one_shot_id,
        "thread_id": record.thread_id,
        "item_id": record.item_id,
        "approval_id": record.approval_id,
        "turn_id": record.turn_id,
        "normalized_command": record.normalized_command,
        "argv": list(record.argv),
        "cwd": str(record.cwd),
        "workspace_target": (
            str(record.workspace_target) if record.workspace_target else None
        ),
        "requested_permission": record.requested_permission,
        "permission_scope": record.permission_scope,
        "patch_identity": record.patch_identity,
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


@app.exception_handler(RequestValidationError)
async def _request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    """Keep missing or malformed approval references fail-closed and uniform."""
    path = request.url.path.rstrip("/")
    identity_fields = {"one_shot_id", "command_hash"}
    if path.endswith("/execute") and any(
        error.get("loc", ()) and error["loc"][-1] in identity_fields
        for error in exc.errors()
    ):
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "approval_integrity_mismatch",
                    "message": "approval_integrity_mismatch",
                }
            },
        )
    return await request_validation_exception_handler(request, exc)


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
async def get_approval(
    session_id: str,
    call_id: str,
    provider: str = Query(
        default="codex_cli",
        pattern=r"^(?:codex_cli|claude_cli)$",
    ),
    command_hash: str | None = Query(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    ),
):
    try:
        record = await service.get_approval(
            session_id,
            call_id,
            provider=provider,
            command_hash=command_hash,
        )
    except (ApprovalIntegrityError, ValueError) as exc:
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
        reference = None
        identity_fields = (
            payload.thread_id,
            payload.turn_id,
            payload.item_id,
            payload.approval_id,
        )
        if payload.one_shot_id is not None:
            reference = ApprovalReference.create(
                provider=payload.provider,
                session_id=session_id,
                thread_id=payload.thread_id,
                turn_id=payload.turn_id,
                item_id=payload.item_id,
                approval_id=payload.approval_id,
                call_id=call_id,
                one_shot_id=payload.one_shot_id,
                command_hash=payload.command_hash,
            )
        elif any(value is not None for value in identity_fields):
            raise ApprovalIntegrityError("approval_integrity_mismatch")
        record = await service.decide_approval(
            session_id,
            call_id,
            payload.command_hash,
            decision,
            provider=payload.provider,
            reference=reference,
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


# ============================================================================
# SOP Workflow API
# ============================================================================


@app.get("/api/sop-definitions")
async def list_sop_definitions():
    """List registered SOP definitions (WebUI: pick one to dispatch)."""
    return [
        {
            "id": sop.id,
            "name": sop.name,
            "version": sop.version,
            "description": sop.description,
            "step_count": sum(len(stage.steps) for stage in sop.stages),
            "stages": [{"id": stage.id, "name": stage.name} for stage in sop.stages],
            "steps": [step.id for stage in sop.stages for step in stage.steps],
        }
        for sop in service.sop_definitions.values()
    ]


@app.post("/api/sop-definitions")
async def register_sop_definition(sop: SopDefinition):
    """Register a SOP definition so it can be dispatched at runtime.

    Additive: the engine is untouched; the definition is only held in the
    service registry that ``POST /api/sop-runs`` already reads from.
    """
    service.sop_definitions[sop.id] = sop
    return {"id": sop.id, "name": sop.name, "version": sop.version}


@app.get("/api/sop-runs")
async def list_sop_runs():
    """List SOP runs (WebUI: run picker — no such endpoint existed before)."""
    rows = service.workflow_store.list_entities("sop_runs")
    runs = []
    for row in rows:
        try:
            run = SopRun.model_validate(row)
        except Exception:
            continue
        runs.append(
            {
                "sop_run_id": run.id,
                "sop_definition_id": run.sop_definition_id,
                "sop_version": run.sop_version,
                "status": run.status.value,
                "current_step_id": run.current_step_id,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
            }
        )
    runs.sort(key=lambda item: str(item["started_at"] or ""), reverse=True)
    return runs


@app.post("/api/sop-runs/{run_id}/control")
async def control_sop_run(run_id: str, request: SopControlRequest):
    """Pause / resume / cancel a SOP run.

    Implemented at the service layer by moving the persisted SopRun status —
    the frozen WorkflowEngine is not modified.
    """
    action = (request.action or "").strip().lower()
    if action not in {"pause", "resume", "cancel"}:
        raise HTTPException(status_code=400, detail=f"unsupported action: {action!r}")

    try:
        # get_entity RAISES KeyError when missing (it does not return None).
        row = service.workflow_store.get_entity("sop_runs", run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="sop run not found") from exc
    run = SopRun.model_validate(row)

    if run.status in (SopRunStatus.COMPLETED, SopRunStatus.CANCELLED):
        raise HTTPException(
            status_code=409, detail=f"sop run already terminal: {run.status.value}"
        )

    transitions = {
        "pause": SopRunStatus.PAUSED,
        "resume": SopRunStatus.RUNNING,
        "cancel": SopRunStatus.CANCELLED,
    }
    if action == "resume" and run.status is not SopRunStatus.PAUSED:
        raise HTTPException(
            status_code=409, detail=f"cannot resume from {run.status.value}"
        )

    run.status = transitions[action]
    if action == "cancel":
        run.completed_at = datetime.now()
    service.workflow_store.save_entity("sop_runs", run)
    return {
        "sop_run_id": run.id,
        "action": action,
        "status": run.status.value,
        "reason": request.reason,
    }


@app.post("/api/sop-runs", response_model=StartSopRunResponse)
async def start_sop_run(request: StartSopRunRequest):
    """Start a new SOP run (creates state only by default).

    Set auto_execute=True to automatically trigger orchestration after creation.
    """
    if request.sop_definition_id not in service.sop_definitions:
        raise HTTPException(status_code=404, detail="SOP definition not found")

    sop = service.sop_definitions[request.sop_definition_id]
    goal = Goal(
        id=str(uuid.uuid4()),
        project_id=request.project_id,
        description=request.goal_description,
        acceptance_criteria=list(request.acceptance_criteria),
        constraints=list(request.constraints),
        status=GoalStatus.RUNNING,
        created_at=datetime.now(),
    )

    sop_run = service.workflow_engine.start_sop_run(goal=goal, sop=sop)

    # Optionally trigger automatic execution
    if request.auto_execute:
        task = asyncio.create_task(
            service._execute_sop_run_background(sop_run.id, goal, sop)
        )
        service._background_sop_tasks[sop_run.id] = task

    return StartSopRunResponse(
        sop_run_id=sop_run.id,
        goal_id=goal.id,
        status=sop_run.status.value,
        started_at=sop_run.started_at or goal.created_at,
    )


@app.post("/api/sop-runs/{run_id}/execute")
async def execute_sop_run(run_id: str):
    """Execute a SOP run (triggers orchestration in background).

    This endpoint starts the orchestration process for a previously created SOP run.
    Execution happens asynchronously - use GET /api/sop-runs/{run_id} to check status.
    """
    try:
        run_data = service.workflow_store.get_entity("sop_runs", run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="SOP run not found") from exc

    run = SopRun.model_validate(run_data)

    # Check if already executing or terminal
    if run_id in service._background_sop_tasks:
        return {"status": "already_executing", "run_id": run_id}

    if run.status in (
        SopRunStatus.COMPLETED,
        SopRunStatus.CANCELLED,
        SopRunStatus.FAILED,
    ):
        raise HTTPException(
            status_code=409, detail=f"Cannot execute terminal run: {run.status.value}"
        )

    # Retrieve goal and sop definition
    goal_data = service.workflow_store.get_entity("goals", run.goal_id)
    goal = Goal.model_validate(goal_data)

    if run.sop_definition_id not in service.sop_definitions:
        raise HTTPException(status_code=404, detail="SOP definition not found")
    sop = service.sop_definitions[run.sop_definition_id]

    # Trigger background execution
    task = asyncio.create_task(service._execute_sop_run_background(run_id, goal, sop))
    service._background_sop_tasks[run_id] = task

    return {
        "status": "executing",
        "run_id": run_id,
        "message": "Orchestration started in background",
    }


@app.get("/api/sop-runs/{run_id}", response_model=SopRunStatusResponse)
async def get_sop_run(run_id: str):
    """Get SOP run status and summary."""
    try:
        sop_run = service.workflow_store.get_entity("sop_runs", run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="SOP run not found") from exc

    step_runs = [
        item
        for item in service.workflow_store.list_entities("step_runs")
        if item.get("sop_run_id") == run_id
    ]

    steps_by_status: dict[str, int] = {}
    for step_run in step_runs:
        status = step_run.get("status", "unknown")
        steps_by_status[status] = steps_by_status.get(status, 0) + 1

    return SopRunStatusResponse(
        id=sop_run["id"],
        goal_id=sop_run["goal_id"],
        sop_definition_id=sop_run["sop_definition_id"],
        status=sop_run["status"],
        started_at=sop_run["started_at"],
        completed_at=sop_run.get("completed_at"),
        step_count=len(step_runs),
        steps_completed=steps_by_status.get("completed", 0),
        steps_ready=steps_by_status.get("ready", 0),
        steps_running=steps_by_status.get("running", 0),
    )


@app.get("/api/sop-runs/{run_id}/steps", response_model=list[StepRunResponse])
async def get_sop_steps(run_id: str):
    """Get all step runs for a SOP run."""
    step_runs = [
        item
        for item in service.workflow_store.list_entities("step_runs")
        if item.get("sop_run_id") == run_id
    ]
    return [
        StepRunResponse(
            id=item["id"],
            sop_run_id=item["sop_run_id"],
            step_id=item["step_id"],
            stage_run_id=item["stage_run_id"],
            status=item["status"],
            task_id=item.get("task_id"),
        )
        for item in step_runs
    ]


@app.get("/api/sop-runs/{run_id}/tasks", response_model=list[TaskResponse])
async def get_sop_tasks(run_id: str):
    """Get all tasks for a SOP run."""
    step_runs = [
        item
        for item in service.workflow_store.list_entities("step_runs")
        if item.get("sop_run_id") == run_id
    ]
    step_run_ids = {item["id"] for item in step_runs}

    tasks = [
        item
        for item in service.workflow_store.list_entities("tasks")
        if item.get("step_run_id") in step_run_ids
    ]
    return [
        TaskResponse(
            id=item["id"],
            step_run_id=item["step_run_id"],
            title=item["title"],
            description=item["description"],
            role_id=item["role_id"],
            status=item["status"],
            input_artifact_ids=item.get("input_artifact_ids", []),
            output_artifact_ids=item.get("output_artifact_ids", []),
        )
        for item in tasks
    ]


@app.get("/api/sop-runs/{run_id}/artifacts", response_model=list[ArtifactResponse])
async def get_sop_artifacts(run_id: str):
    """Get all artifacts for a SOP run."""
    step_runs = [
        item
        for item in service.workflow_store.list_entities("step_runs")
        if item.get("sop_run_id") == run_id
    ]
    step_run_ids = {item["id"] for item in step_runs}

    tasks = [
        item
        for item in service.workflow_store.list_entities("tasks")
        if item.get("step_run_id") in step_run_ids
    ]
    task_ids = {item["id"] for item in tasks}
    task_role_by_id = {item["id"]: item.get("role_id") for item in tasks}

    artifacts = [
        item
        for item in service.workflow_store.list_entities("artifacts")
        if item.get("task_id") in task_ids
    ]
    return [_artifact_response(item, task_role_by_id) for item in artifacts]


_REVIEW_OUTCOMES = {"PASS", "REWORK", "PLAN_INVALID"}


def _artifact_response(
    item: dict[str, Any], task_role_by_id: dict[str, str | None]
) -> ArtifactResponse:
    """Project one Artifact row into ArtifactResponse.

    role_id 是 Task.role_id 的 join 投影（Artifact 模型上没有 role / role_id）。
    """
    return ArtifactResponse(
        id=item["id"],
        task_id=item["task_id"],
        type=item["type"],
        uri=item.get("uri"),
        summary=item.get("summary"),
        sha256=item.get("sha256"),
        accepted=item.get("accepted", False),
        created_at=item["created_at"],
        attempt_id=item.get("attempt_id"),
        producer_step_run_id=item.get("producer_step_run_id"),
        role_id=task_role_by_id.get(item["task_id"]),
    )


def _attempt_response(
    item: dict[str, Any],
    *,
    task_to_step: dict[str, str],
    step_to_run: dict[str, str],
    artifacts_by_attempt: dict[str, list[str]],
) -> AttemptResponse:
    """Project one Attempt row into AttemptResponse.

    sop_run_id 由 Task -> StepRun -> SopRun 解析；解析不到时为空字符串。
    """
    step_run_id = task_to_step.get(item["task_id"])
    sop_run_id = step_to_run.get(step_run_id or "", "")
    return AttemptResponse(
        id=item["id"],
        task_id=item["task_id"],
        sequence=item.get("sequence", 1),
        session_id=item.get("session_id"),
        runtime_id=item.get("runtime_id"),
        previous_attempt_id=item.get("previous_attempt_id"),
        started_at=item.get("started_at"),
        completed_at=item.get("completed_at"),
        status=item.get("status", "pending"),
        sop_run_id=sop_run_id,
        artifact_ids=artifacts_by_attempt.get(item["id"], []),
    )


def _artifacts_by_attempt(artifacts: list[dict[str, Any]]) -> dict[str, list[str]]:
    """attempt_id -> [artifact_id] 索引（一次全表扫描）。"""
    index: dict[str, list[str]] = {}
    for artifact in artifacts:
        attempt_id = artifact.get("attempt_id")
        if attempt_id:
            index.setdefault(attempt_id, []).append(artifact["id"])
    return index


def _parse_review_outcome(
    content: str | None,
) -> tuple[str | None, list[str], str | None]:
    """解析评审 Artifact 的 JSON content（result / blocking），容错语义与 engine 一致。

    失败时显式返回 (None, [], 原因)——绝不伪造 PASS。
    """
    if not content:
        return None, [], "reviewer report has no content"
    try:
        data = json.loads(content)
    except (TypeError, ValueError) as exc:
        return None, [], f"reviewer report is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, [], "reviewer report content is not a JSON object"
    result = data.get("result", data.get("verdict"))
    if not isinstance(result, str) or result.strip().upper() not in _REVIEW_OUTCOMES:
        return None, [], f"review result {result!r} is not one of {sorted(_REVIEW_OUTCOMES)}"
    blocking = data.get("blocking", [])
    if not isinstance(blocking, list) or not all(isinstance(b, str) for b in blocking):
        blocking = []
    return result.strip().upper(), list(blocking), None


@app.get("/api/sop-runs/{run_id}/attempts", response_model=list[AttemptResponse])
async def get_sop_attempts(run_id: str):
    """该 run 的全部执行 Attempt（血缘投影：Task -> StepRun -> SopRun）。

    只读；run_id 不存在时返回 []（与 /tasks、/artifacts 语义一致）。
    排序 (task_id, sequence) 仅作稳定展示序，逻辑重试顺序看 /chain。
    """
    step_runs = [
        item
        for item in service.workflow_store.list_entities("step_runs")
        if item.get("sop_run_id") == run_id
    ]
    step_run_ids = {item["id"] for item in step_runs}
    if not step_run_ids:
        return []

    tasks = [
        item
        for item in service.workflow_store.list_entities("tasks")
        if item.get("step_run_id") in step_run_ids
    ]
    task_ids = {item["id"] for item in tasks}
    if not task_ids:
        return []

    attempts = [
        item
        for item in service.workflow_store.list_entities("attempts")
        if item.get("task_id") in task_ids
    ]
    task_to_step = {item["id"]: item["step_run_id"] for item in tasks}
    step_to_run = {item["id"]: item["sop_run_id"] for item in step_runs}
    artifacts_by_attempt = _artifacts_by_attempt(
        service.workflow_store.list_entities("artifacts")
    )

    ordered = sorted(
        attempts, key=lambda item: (item.get("task_id", ""), item.get("sequence", 1))
    )
    return [
        _attempt_response(
            item,
            task_to_step=task_to_step,
            step_to_run=step_to_run,
            artifacts_by_attempt=artifacts_by_attempt,
        )
        for item in ordered
    ]


@app.get("/api/sop-runs/{run_id}/evidence", response_model=list[ReviewEvidenceResponse])
async def get_sop_evidence(run_id: str):
    """把每个 Review 绑定到被评审/评审 Attempt 的证据三件套并校验。

    outcome / blocking 解析自 reviewer_report Artifact 的 JSON content；
    解析失败或证据跨 Attempt 时 evidence_valid=False（后端判定，前端不再自行推断）。
    """
    step_runs = [
        item
        for item in service.workflow_store.list_entities("step_runs")
        if item.get("sop_run_id") == run_id
    ]
    step_run_ids = {item["id"] for item in step_runs}
    if not step_run_ids:
        return []

    tasks = [
        item
        for item in service.workflow_store.list_entities("tasks")
        if item.get("step_run_id") in step_run_ids
    ]
    task_ids = {item["id"] for item in tasks}
    if not task_ids:
        return []

    task_role_by_id = {item["id"]: item.get("role_id") for item in tasks}
    reviews = [
        item
        for item in service.workflow_store.list_entities("reviews")
        if item.get("task_id") in task_ids
    ]

    artifacts = service.workflow_store.list_entities("artifacts")
    artifact_by_id = {item["id"]: item for item in artifacts}
    attempt_by_id = {
        item["id"]: item
        for item in service.workflow_store.list_entities("attempts")
    }
    artifacts_by_attempt: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        attempt_id = artifact.get("attempt_id")
        if attempt_id:
            artifacts_by_attempt.setdefault(attempt_id, []).append(artifact)

    result: list[ReviewEvidenceResponse] = []
    for review in sorted(reviews, key=lambda item: item.get("created_at") or ""):
        reviewed_attempt_id = review.get("reviewed_attempt_id")
        created_at = review.get("created_at") or datetime.now(UTC)

        reviewer_report_row = artifact_by_id.get(review.get("artifact_id"))
        reviewer_report = (
            _artifact_response(reviewer_report_row, task_role_by_id)
            if reviewer_report_row is not None
            else None
        )
        reviewer_attempt_id = (
            reviewer_report_row.get("attempt_id")
            if reviewer_report_row is not None
            else None
        )

        if reviewed_attempt_id is None or reviewed_attempt_id not in attempt_by_id:
            # schema-v2 之前的 legacy 记录（或悬挂引用）：无 Attempt 血缘，
            # 仍返回并显式标 invalid——绝不伪造证据。
            result.append(
                ReviewEvidenceResponse(
                    review_id=review["id"],
                    reviewed_attempt_id=reviewed_attempt_id,
                    review_status=str(review.get("status", "pending")),
                    outcome=None,
                    blocking_items=[],
                    outcome_parse_error=None,
                    execution_diff=None,
                    execution_test_report=None,
                    reviewer_report=reviewer_report,
                    reviewer_attempt_id=reviewer_attempt_id,
                    evidence_valid=False,
                    evidence_complete=False,
                    created_at=created_at,
                )
            )
            continue

        # 执行证据按被评审 Attempt 选择——与 Phase 3 lineage._attempt_artifacts
        # 的 evidence-gate 语义一致：跨 Attempt 的证据视为缺失（fail-closed），
        # UI 判定与引擎门禁不会互相矛盾。
        candidates = artifacts_by_attempt.get(reviewed_attempt_id, [])

        def _latest(
            candidates: list[dict[str, Any]], artifact_type: str
        ) -> ArtifactResponse | None:
            matched = [
                item
                for item in candidates
                if str(item.get("type")) == artifact_type
            ]
            if not matched:
                return None
            matched.sort(key=lambda item: item.get("created_at") or "", reverse=True)
            return _artifact_response(matched[0], task_role_by_id)

        execution_diff = _latest(candidates, "diff")
        execution_test_report = _latest(candidates, "test_report")

        outcome, blocking_items, parse_error = _parse_review_outcome(
            reviewer_report_row.get("content")
            if reviewer_report_row is not None
            else None
        )

        evidence_complete = (
            execution_diff is not None
            and execution_test_report is not None
            and reviewer_report is not None
        )
        evidence_valid = (
            evidence_complete
            and execution_diff.attempt_id == reviewed_attempt_id
            and execution_test_report.attempt_id == reviewed_attempt_id
            and execution_diff.accepted
            and execution_test_report.accepted
            and outcome is not None
        )

        result.append(
            ReviewEvidenceResponse(
                review_id=review["id"],
                reviewed_attempt_id=reviewed_attempt_id,
                review_status=str(review.get("status", "pending")),
                outcome=outcome,
                blocking_items=blocking_items,
                outcome_parse_error=parse_error,
                execution_diff=execution_diff,
                execution_test_report=execution_test_report,
                reviewer_report=reviewer_report,
                reviewer_attempt_id=reviewer_attempt_id,
                evidence_valid=evidence_valid,
                evidence_complete=evidence_complete,
                created_at=created_at,
            )
        )
    return result


@app.get("/api/attempts/{attempt_id}/chain", response_model=AttemptChainResponse)
async def get_attempt_chain(attempt_id: str):
    """沿 previous_attempt_id 回溯 REWORK 重试链（不使用 sequence）。

    previous_attempt_id 成环时停止并在响应中标 truncated，前端必须显式提示。
    """
    try:
        service.workflow_store.get_entity("attempts", attempt_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="attempt not found") from exc

    attempts = service.workflow_store.list_entities("attempts")
    attempt_by_id = {item["id"]: item for item in attempts}

    tasks = service.workflow_store.list_entities("tasks")
    step_runs = service.workflow_store.list_entities("step_runs")
    task_to_step = {
        item["id"]: item["step_run_id"]
        for item in tasks
        if item.get("step_run_id")
    }
    step_to_run = {
        item["id"]: item["sop_run_id"]
        for item in step_runs
        if item.get("sop_run_id")
    }
    artifacts_by_attempt = _artifacts_by_attempt(
        service.workflow_store.list_entities("artifacts")
    )

    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    truncated = False
    current_id: str | None = attempt_id
    while current_id is not None:
        if current_id in visited:
            truncated = True
            break
        visited.add(current_id)
        current = attempt_by_id.get(current_id)
        if current is None:
            break
        chain.append(current)
        current_id = current.get("previous_attempt_id")

    chain.reverse()  # 时间正序：链首 -> 链尾
    root_task_id = chain[0]["task_id"] if chain else ""
    return AttemptChainResponse(
        attempts=[
            _attempt_response(
                item,
                task_to_step=task_to_step,
                step_to_run=step_to_run,
                artifacts_by_attempt=artifacts_by_attempt,
            )
            for item in chain
        ],
        root_task_id=root_task_id,
        chain_length=len(chain),
        truncated=truncated,
    )


@app.get("/api/artifacts/{artifact_id}/content")
async def get_artifact_content(artifact_id: str):
    """Read-only artifact payload (WebUI: view PLAN / Diff / Test / Review).

    Additive and read-only: it only reads what FileArtifactStore already owns
    and re-verifies the stored sha256. No adapter / engine / handoff /
    approval layer is touched (Backend Core remains frozen).
    """
    row = next(
        (
            item
            for item in service.workflow_store.list_entities("artifacts")
            if item.get("id") == artifact_id
        ),
        None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    artifact = Artifact.model_validate(row)
    if not artifact.uri or not artifact.sha256:
        raise HTTPException(status_code=404, detail="artifact has no persisted payload")
    try:
        payload = service.artifact_store.get(artifact)
    except ArtifactStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": artifact.id,
        "type": str(artifact.type),
        "sha256": artifact.sha256,
        "content": payload.decode("utf-8", errors="replace"),
    }


@app.get("/api/sop-runs/{run_id}/handoffs", response_model=list[HandoffResponse])
async def get_sop_handoffs(run_id: str):
    """Get all handoffs for a SOP run."""
    step_runs = [
        item
        for item in service.workflow_store.list_entities("step_runs")
        if item.get("sop_run_id") == run_id
    ]
    step_run_ids = {item["id"] for item in step_runs}

    tasks = [
        item
        for item in service.workflow_store.list_entities("tasks")
        if item.get("step_run_id") in step_run_ids
    ]
    task_ids = {item["id"] for item in tasks}

    handoffs = [
        item
        for item in service.workflow_store.list_entities("handoffs")
        if item.get("from_task_id") in task_ids
    ]
    return [
        HandoffResponse(
            id=item["id"],
            from_task_id=item["from_task_id"],
            to_step_id=item["to_step_id"],
            status=item["status"],
            artifact_ids=item.get("artifact_ids", []),
            created_at=item.get("created_at"),
            accepted_at=item.get("accepted_at"),
        )
        for item in handoffs
    ]


@app.get("/api/sop-runs/{run_id}/events", response_model=list[SopEventResponse])
async def get_sop_events(run_id: str, after: int = Query(default=0, ge=0)):
    """Get SOP workflow events."""
    events = service.workflow_store.replay_events(run_id, after=after)
    return [
        SopEventResponse(
            id=event.id,
            stream_id=event.stream_id,
            sequence=event.sequence,
            event_type=event.event_type,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )
        for event in events
    ]


@app.get("/api/sop-runs/{run_id}/snapshot")
async def get_sop_run_snapshot(run_id: str):
    """Phase 6: consolidated observability snapshot (step/task/attempt/session/
    artifacts/reviews/policy gates/error reasons) for one run."""
    from workbench.backend.workflow.observability import collect_run_snapshot

    return collect_run_snapshot(sop_run_id=run_id, store=service.workflow_store)


@app.get("/api/sop-runs/{run_id}/rework-lineage")
async def get_sop_rework_lineage(run_id: str):
    """Phase 6: per-step rework chains (tasks via retry_of, attempts via
    previous_attempt_id) for pinpointing which attempt a run is on."""
    from workbench.backend.workflow.observability import collect_rework_lineage

    return collect_rework_lineage(sop_run_id=run_id, store=service.workflow_store)


@app.get("/api/provider-health")
async def get_provider_health():
    """Phase 6: configured runtimes + last session seen per runtime."""
    from workbench.backend.workflow.observability import collect_provider_health

    return collect_provider_health(
        store=service.workflow_store,
        runners=getattr(service.workflow_engine, "_runners", None),
    )


@app.get("/api/overview")
async def get_overview():
    """Phase 6: cross-run overview counts (metrics-lite)."""
    from workbench.backend.workflow.observability import collect_global_overview

    return collect_global_overview(store=service.workflow_store)


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

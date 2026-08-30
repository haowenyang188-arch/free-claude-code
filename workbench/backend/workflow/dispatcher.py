"""Thin Handoff Dispatcher for the SOP communication layer.

Design rules (per architecture decision):
- Adapters never hold references to each other. The dispatcher is the ONLY
  component that resolves cross-agent addressing and owns the runner registry.
- ``Handoff`` is the addressing primitive (who -> who, what). ``EventRecord``
  (via ``JsonWorkflowStore``) is the Event Bus. ``Session.external_id`` maps
  external agent sessions/threads. No new Message/Artifact/Event/Session buses
  are introduced here.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..domain.models import (
    Artifact,
    ArtifactType,
    ContextPackage,
    Handoff,
    HandoffMessageType,
    HandoffStatus,
    RuntimeKind,
    Session,
    StepRun,
    SubagentAssignment,
    Task,
)
from .engine import ExecutionResult, WorkflowEngine


class PolicyViolation(RuntimeError):
    """Raised when a Handoff would violate the CommunicationPolicy allowlist."""


class CommunicationPolicy:
    """Explicit allowlist of permitted (sender_kind, message_type, recipient_kind).

    V1 permits exactly the six flows required by the fixed SOP pipeline. Any
    other cross-agent communication is rejected. The SOP Engine is modelled as
    the pseudo-runtime ``sop_engine`` for the terminal PASS message.
    """

    V1_ALLOWLIST: frozenset[tuple[str, str, str]] = frozenset(
        {
            ("claude_code", "plan_ready", "deepseek_harness"),
            ("deepseek_harness", "review_request", "codex"),
            ("codex", "rework", "deepseek_harness"),
            ("codex", "plan_invalid", "claude_code"),
            ("deepseek_harness", "plan_blocked", "claude_code"),
            ("codex", "pass", "sop_engine"),
        }
    )

    def __init__(self, allowlist: Any = None) -> None:
        self._allow = frozenset(allowlist) if allowlist is not None else self.V1_ALLOWLIST

    def is_allowed(self, sender_kind: str, message_type: str, recipient_kind: str) -> bool:
        return (
            sender_kind.lower(),
            message_type.lower(),
            recipient_kind.lower(),
        ) in self._allow

    def assert_allowed(
        self, sender_kind: str, message_type: str, recipient_kind: str
    ) -> None:
        if not self.is_allowed(sender_kind, message_type, recipient_kind):
            raise PolicyViolation(
                f"communication not allowed: "
                f"{sender_kind} --{message_type}--> {recipient_kind}"
            )


class SessionLifecycle:
    """Decide CREATE/RESUME/FORK/INTERRUPT/CLOSE for an agent Session."""

    @staticmethod
    def for_transition(transition: str, has_session: bool) -> str:
        transition = (transition or "").lower()
        if transition == "interrupt":
            return "INTERRUPT"
        if transition == "close":
            return "CLOSE"
        if transition in {"plan_ready", "review_request"}:
            return "CREATE" if not has_session else "RESUME"
        if transition == "rework":
            # Continue from the current execution context; reuse session if present.
            return "RESUME" if has_session else "CREATE"
        if transition == "plan_invalid":
            # Fresh planning context for PLAN v2: fork a new session by default.
            return "FORK" if has_session else "CREATE"
        if transition == "plan_blocked":
            return "RESUME" if has_session else "CREATE"
        if transition == "pass":
            return "CLOSE" if has_session else "CREATE"
        # Generic onward handoff.
        return "RESUME" if has_session else "CREATE"


@dataclass
class DispatchResult:
    execution_result: ExecutionResult
    sender_kind: str
    recipient_kind: str
    session_id: str
    lifecycle_decision: str
    correlation_id: str
    event_id: str
    outgoing_handoff_id: str | None = None


class HandoffDispatcher:
    """Resolve a READY Handoff to its target adapter and execute it.

    Call chain: ``Handoff(to_step_id)`` -> ``StepDefinition.role_id`` ->
    ``SubagentAssignment`` -> ``Runtime`` + ``Session`` -> adapter (via
    ``WorkflowEngine``). All cross-agent communication is recorded as a Handoff
    (artifact bus), an EventRecord (event bus) and a Session reference.
    """

    def __init__(
        self,
        *,
        engine: WorkflowEngine,
        policy: CommunicationPolicy | None = None,
        runtime_registry: dict[str, RuntimeKind] | None = None,
        resolve_assignment: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.engine = engine
        self.policy = policy or CommunicationPolicy()
        self._runtime_registry = {
            k: (v if isinstance(v, RuntimeKind) else RuntimeKind(v))
            for k, v in (runtime_registry or {}).items()
        }
        self._resolve_assignment = resolve_assignment

    def _kind(self, runtime_id: str) -> str:
        rk = self._runtime_registry.get(runtime_id)
        return rk.value if isinstance(rk, RuntimeKind) else str(rk or runtime_id)

    async def _resolve(self, role_id: str, run_id: str) -> SubagentAssignment:
        if self._resolve_assignment is None:
            raise RuntimeError("HandoffDispatcher requires a resolve_assignment callable")
        result = self._resolve_assignment(role_id, run_id)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, SubagentAssignment):
            raise TypeError("assignment resolver returned an invalid value")
        return result

    def _run_id_for_task(self, task_id: str) -> str:
        task = Task.model_validate(self.engine.store.get_entity("tasks", task_id))
        step_run = StepRun.model_validate(
            self.engine.store.get_entity("step_runs", task.step_run_id)
        )
        return step_run.sop_run_id

    async def dispatch(
        self, handoff_id: str, *, correlation_id: str | None = None
    ) -> DispatchResult:
        store = self.engine.store
        handoff = Handoff.model_validate(store.get_entity("handoffs", handoff_id))
        if handoff.status is not HandoffStatus.READY:
            raise RuntimeError(
                f"handoff {handoff_id} is not READY (status={handoff.status})"
            )

        run_id = self._run_id_for_task(handoff.from_task_id)
        source_task = Task.model_validate(
            store.get_entity("tasks", handoff.from_task_id)
        )
        source_step_run = StepRun.model_validate(
            store.get_entity("step_runs", source_task.step_run_id)
        )
        source_step = self.engine.step_definition(run_id, source_step_run.step_id)
        source_assignment = await self._resolve(source_step.role_id, run_id)
        sender_kind = self._kind(source_assignment.runtime_id)

        target_step = self.engine.step_definition(run_id, handoff.to_step_id)
        recipient_assignment = await self._resolve(target_step.role_id, run_id)
        recipient_kind = self._kind(recipient_assignment.runtime_id)

        self.policy.assert_allowed(
            sender_kind, handoff.message_type.value, recipient_kind
        )

        corr = correlation_id or handoff.correlation_id or str(uuid.uuid4())
        decision = SessionLifecycle.for_transition(
            handoff.message_type.value, bool(recipient_assignment.session_id)
        )
        session = self._ensure_session(recipient_assignment, decision)

        event = store.append_event(
            stream_id=run_id,
            event_type="handoff_dispatched",
            payload={
                "handoff_id": handoff.id,
                "message_type": handoff.message_type.value,
                "sender": sender_kind,
                "recipient": recipient_kind,
                "reply_to_handoff_id": handoff.reply_to_handoff_id,
                "correlation_id": corr,
                "artifact_ids": list(handoff.artifact_ids),
                "session_id": session.id,
            },
        )

        self.engine.accept_handoff(handoff_id)
        target_step_run_id = f"{run_id}:{handoff.to_step_id}"
        task = self.engine.create_task(
            target_step_run_id, role_id=recipient_assignment.role_id
        )
        context = self._build_context(handoff, task)
        exec_assignment = recipient_assignment.model_copy(
            update={"task_id": task.id, "session_id": session.id}
        )
        result = await self.engine.execute_task(
            task.id, assignment=exec_assignment, context=context
        )

        outgoing_id: str | None = None
        if result.handoff is not None:
            self._stamp_handoff(
                result.handoff.id,
                message_type=self._outgoing_type(recipient_kind, handoff.message_type.value),
                correlation_id=corr,
                reply_to_handoff_id=handoff.id,
            )
            outgoing_id = result.handoff.id

        return DispatchResult(
            execution_result=result,
            sender_kind=sender_kind,
            recipient_kind=recipient_kind,
            session_id=session.id,
            lifecycle_decision=decision,
            correlation_id=corr,
            event_id=event.id,
            outgoing_handoff_id=outgoing_id,
        )

    def _outgoing_type(self, recipient_kind: str, incoming: str) -> HandoffMessageType:
        if recipient_kind == "codex":
            return HandoffMessageType.REVIEW_REQUEST
        if recipient_kind == "deepseek_harness":
            return (
                HandoffMessageType.REWORK_COMPLETED
                if incoming == "rework"
                else HandoffMessageType.HANDOFF
            )
        if recipient_kind == "claude_code":
            return HandoffMessageType.PLAN_INVALID
        return HandoffMessageType.HANDOFF

    def _ensure_session(
        self, assignment: SubagentAssignment, decision: str
    ) -> Session:
        store = self.engine.store
        if decision in {"RESUME", "INTERRUPT", "CLOSE"} and assignment.session_id:
            session = Session.model_validate(
                store.get_entity("sessions", assignment.session_id)
            )
            session.status = decision.lower()
            store.save_entity("sessions", session)
            return session
        session = Session(
            id=str(uuid.uuid4()),
            runtime_id=assignment.runtime_id,
            agent_instance_id=assignment.agent_instance_id,
            external_id=None,
            status="created",
            workspace_path=None,
        )
        store.save_entity("sessions", session)
        assignment.session_id = session.id
        store.save_entity("assignments", assignment)
        return session

    def _build_context(self, handoff: Handoff, task: Task) -> ContextPackage:
        return ContextPackage(
            id=str(uuid.uuid4()),
            task_id=task.id,
            goal_summary=handoff.brief or "SOP handoff",
            instructions=handoff.brief or "",
            artifact_ids=list(handoff.artifact_ids),
            workspace_scope=None,
            acceptance_criteria=[],
        )

    def _stamp_handoff(
        self,
        handoff_id: str,
        *,
        message_type: HandoffMessageType,
        correlation_id: str,
        reply_to_handoff_id: str | None,
    ) -> None:
        store = self.engine.store
        handoff = Handoff.model_validate(store.get_entity("handoffs", handoff_id))
        handoff.message_type = message_type
        handoff.correlation_id = correlation_id
        handoff.reply_to_handoff_id = reply_to_handoff_id
        store.save_entity("handoffs", handoff)

    def create_handoff(
        self,
        *,
        from_task_id: str,
        to_step_id: str,
        message_type: HandoffMessageType,
        artifact_ids: list[str] | None = None,
        context_package_id: str | None = None,
        brief: str = "",
        correlation_id: str | None = None,
        reply_to_handoff_id: str | None = None,
        run_id: str,
    ) -> Handoff:
        """SOP Engine authors intentional cross-agent Handoffs via this method."""
        handoff = Handoff(
            id=str(uuid.uuid4()),
            from_task_id=from_task_id,
            to_step_id=to_step_id,
            artifact_ids=list(artifact_ids or []),
            context_package_id=context_package_id,
            brief=brief,
            status=HandoffStatus.READY,
            message_type=message_type,
            reply_to_handoff_id=reply_to_handoff_id,
            correlation_id=correlation_id,
        )
        self.engine.store.save_entity("handoffs", handoff)
        self.engine.store.append_event(
            stream_id=run_id,
            event_type="handoff_created",
            payload={
                "handoff_id": handoff.id,
                "message_type": message_type.value,
                "to_step_id": to_step_id,
                "reply_to_handoff_id": reply_to_handoff_id,
                "correlation_id": correlation_id,
            },
        )
        return handoff

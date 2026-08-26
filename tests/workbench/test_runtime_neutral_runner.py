"""Test runtime-neutral StepRunner that adapts to Claude Code and Codex."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    ContextPackage,
    RuntimeKind,
    SubagentAssignment,
    Task,
)
from workbench.backend.workflow.runners import (
    RuntimeAdapter,
    RuntimeNeutralRunner,
    RunnerError,
)


class _MockAdapter(RuntimeAdapter):
    """Minimal test adapter that returns predictable artifacts."""

    def __init__(self, runtime_kind: RuntimeKind, response: str = "done") -> None:
        self.runtime_kind = runtime_kind
        self.response = response
        self.calls: list[tuple[Task, SubagentAssignment, ContextPackage]] = []

    async def execute_step(
        self,
        *,
        task: Task,
        assignment: SubagentAssignment,
        context: ContextPackage,
    ) -> Artifact:
        self.calls.append((task, assignment, context))
        return Artifact(
            id=f"artifact-{task.id}",
            task_id=task.id,
            type=context.expected_output_schema.get("type", ArtifactType.TEXT),
            content=self.response,
            summary=f"Executed by {self.runtime_kind.value}",
        )

    def supports(self, runtime_kind: RuntimeKind) -> bool:
        return runtime_kind == self.runtime_kind


@pytest.mark.asyncio
async def test_runner_routes_to_correct_adapter_by_runtime_kind() -> None:
    """Runner should select the adapter matching the assignment's runtime_id."""
    claude_adapter = _MockAdapter(RuntimeKind.CLAUDE_CODE, "claude result")
    codex_adapter = _MockAdapter(RuntimeKind.CODEX, "codex result")
    runner = RuntimeNeutralRunner(adapters=[claude_adapter, codex_adapter])

    task = Task(
        id="task-1",
        step_run_id="step-run-1",
        role_id="researcher",
        title="Research topic",
    )
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="researcher",
        agent_instance_id="agent-1",
        runtime_id="codex-runtime",
    )
    context = ContextPackage(
        id="context-1",
        task_id=task.id,
        goal_summary="Ship the feature",
        instructions="Research thoroughly",
    )

    # Mock runtime registry
    runner._runtime_registry = {
        "codex-runtime": RuntimeKind.CODEX,
        "claude-runtime": RuntimeKind.CLAUDE_CODE,
    }

    artifact = await runner.execute(task=task, assignment=assignment, context=context)

    assert artifact.content == "codex result"
    assert artifact.summary == "Executed by codex"
    assert len(codex_adapter.calls) == 1
    assert len(claude_adapter.calls) == 0


@pytest.mark.asyncio
async def test_runner_raises_on_unknown_runtime() -> None:
    """Runner should reject assignments to unregistered runtimes."""
    runner = RuntimeNeutralRunner(adapters=[])
    runner._runtime_registry = {}

    task = Task(
        id="task-1",
        step_run_id="step-run-1",
        role_id="researcher",
    )
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="researcher",
        agent_instance_id="agent-1",
        runtime_id="unknown-runtime",
    )
    context = ContextPackage(
        id="context-1",
        task_id=task.id,
        goal_summary="Goal",
        instructions="Do work",
    )

    with pytest.raises(RunnerError, match="unknown runtime"):
        await runner.execute(task=task, assignment=assignment, context=context)


@pytest.mark.asyncio
async def test_runner_raises_when_no_adapter_supports_runtime_kind() -> None:
    """Runner should fail if no adapter claims to support the runtime kind."""
    claude_adapter = _MockAdapter(RuntimeKind.CLAUDE_CODE)
    runner = RuntimeNeutralRunner(adapters=[claude_adapter])
    runner._runtime_registry = {"codex-runtime": RuntimeKind.CODEX}

    task = Task(id="task-1", step_run_id="step-run-1", role_id="researcher")
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="researcher",
        agent_instance_id="agent-1",
        runtime_id="codex-runtime",
    )
    context = ContextPackage(
        id="context-1", task_id=task.id, goal_summary="Goal", instructions="Do work"
    )

    with pytest.raises(RunnerError, match="no adapter supports"):
        await runner.execute(task=task, assignment=assignment, context=context)


@pytest.mark.asyncio
async def test_runner_validates_artifact_matches_task() -> None:
    """Runner should reject artifacts that don't reference the correct task_id."""

    class _BadAdapter(RuntimeAdapter):
        async def execute_step(
            self, *, task: Task, assignment: SubagentAssignment, context: ContextPackage
        ) -> Artifact:
            return Artifact(
                id="artifact-1",
                task_id="wrong-task-id",
                type=ArtifactType.TEXT,
                content="result",
            )

        def supports(self, runtime_kind: RuntimeKind) -> bool:
            return runtime_kind == RuntimeKind.CODEX

    runner = RuntimeNeutralRunner(adapters=[_BadAdapter()])
    runner._runtime_registry = {"codex-runtime": RuntimeKind.CODEX}

    task = Task(id="task-1", step_run_id="step-run-1", role_id="researcher")
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="researcher",
        agent_instance_id="agent-1",
        runtime_id="codex-runtime",
    )
    context = ContextPackage(
        id="context-1", task_id=task.id, goal_summary="Goal", instructions="Do work"
    )

    with pytest.raises(RunnerError, match="task_id mismatch"):
        await runner.execute(task=task, assignment=assignment, context=context)


@pytest.mark.asyncio
async def test_runner_passes_all_context_fields_to_adapter() -> None:
    """Runner should forward the complete context package to the adapter."""

    class _InspectAdapter(RuntimeAdapter):
        def __init__(self) -> None:
            self.received_context: ContextPackage | None = None

        async def execute_step(
            self, *, task: Task, assignment: SubagentAssignment, context: ContextPackage
        ) -> Artifact:
            self.received_context = context
            return Artifact(
                id="artifact-1",
                task_id=task.id,
                type=ArtifactType.TEXT,
                content="result",
            )

        def supports(self, runtime_kind: RuntimeKind) -> bool:
            return True

    adapter = _InspectAdapter()
    runner = RuntimeNeutralRunner(adapters=[adapter])
    runner._runtime_registry = {"runtime-1": RuntimeKind.CODEX}

    task = Task(id="task-1", step_run_id="step-run-1", role_id="researcher")
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="researcher",
        agent_instance_id="agent-1",
        runtime_id="runtime-1",
    )
    context = ContextPackage(
        id="context-1",
        task_id=task.id,
        goal_summary="Complete the feature",
        instructions="Research thoroughly",
        constraints=["use only public APIs", "max 1000 tokens"],
        artifact_ids=["upstream-artifact-1"],
        decisions=["use SQLite for persistence"],
        expected_output_schema={"type": "research_report"},
        allowed_tools=["read_file", "grep"],
        workspace_scope="/home/user/project",
    )

    await runner.execute(task=task, assignment=assignment, context=context)

    assert adapter.received_context is not None
    assert adapter.received_context.goal_summary == "Complete the feature"
    assert adapter.received_context.constraints == [
        "use only public APIs",
        "max 1000 tokens",
    ]
    assert adapter.received_context.artifact_ids == ["upstream-artifact-1"]
    assert adapter.received_context.decisions == ["use SQLite for persistence"]
    assert adapter.received_context.workspace_scope == "/home/user/project"

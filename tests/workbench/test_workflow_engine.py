from __future__ import annotations

from pathlib import Path

import pytest

from workbench.backend.domain.models import (
    AgentInstance,
    AgentProfile,
    Artifact,
    ArtifactType,
    ContextPackage,
    Goal,
    HandoffStatus,
    Project,
    Role,
    Runtime,
    RuntimeKind,
    Session,
    SopDefinition,
    StageDefinition,
    StepDefinition,
    StepStatus,
    SubagentAssignment,
    Task,
    TaskStatus,
    ValidationResult,
    ValidationStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.engine import (
    AcceptanceValidator,
    SubagentRunner,
    WorkflowEngine,
)
from workbench.backend.workflow.orchestrator import ManualOrchestrator


class FakeRunner(SubagentRunner):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(
        self,
        *,
        task: Task,
        assignment: SubagentAssignment,
        context: ContextPackage,
    ) -> Artifact:
        self.calls.append(task.id)
        return Artifact(
            id=f"artifact-{task.id}",
            task_id=task.id,
            type=ArtifactType.RESEARCH_REPORT,
            content=f"result for {task.id}",
        )


class AcceptAllValidator(AcceptanceValidator):
    async def validate(self, *, task: Task, artifact: Artifact) -> ValidationResult:
        return ValidationResult(
            id=f"validation-{task.id}",
            task_id=task.id,
            artifact_id=artifact.id,
            validator_id="accept-all",
            status=ValidationStatus.ACCEPTED,
        )


class RejectValidator(AcceptanceValidator):
    async def validate(self, *, task: Task, artifact: Artifact) -> ValidationResult:
        return ValidationResult(
            id=f"validation-{task.id}",
            task_id=task.id,
            artifact_id=artifact.id,
            validator_id="reject-all",
            status=ValidationStatus.REJECTED,
            messages=["required evidence missing"],
        )


def _fixtures(tmp_path: Path) -> tuple[SopDefinition, ManualOrchestrator]:
    project = Project(id="project-1", name="Demo")
    goal = Goal(id="goal-1", project_id=project.id, description="Ship the feature")
    role = Role(id="researcher", name="Researcher", capabilities=["research"])
    profile = AgentProfile(
        id="claude-profile", name="Claude", capabilities=["research"]
    )
    instance = AgentInstance(id="claude-1", profile_id=profile.id, status="online")
    runtime = Runtime(
        id="claude-runtime", name="Claude Code", kind=RuntimeKind.CLAUDE_CODE
    )
    session = Session(
        id="session-1", runtime_id=runtime.id, agent_instance_id=instance.id
    )
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id="task-research",
        role_id=role.id,
        agent_instance_id=instance.id,
        runtime_id=runtime.id,
        session_id=session.id,
    )
    sop = SopDefinition(
        id="sop-1",
        name="Research and handoff",
        stages=[
            StageDefinition(
                id="stage-1",
                name="Research",
                steps=[
                    StepDefinition(
                        id="step-research",
                        name="Research",
                        role_id=role.id,
                        output_type=ArtifactType.RESEARCH_REPORT,
                        handoff_to="step-review",
                    ),
                    StepDefinition(
                        id="step-review",
                        name="Review",
                        role_id=role.id,
                        depends_on=["step-research"],
                    ),
                ],
            )
        ],
    )
    orchestrator = ManualOrchestrator(
        goal=goal,
        assignments={assignment.task_id: assignment},
        context_packages={
            assignment.task_id: ContextPackage(
                id="context-research",
                task_id=assignment.task_id,
                goal_summary=goal.description,
                instructions="Produce the research report.",
            )
        },
    )
    return sop, orchestrator


@pytest.mark.asyncio
async def test_engine_runs_artifact_validation_and_handoff(tmp_path: Path) -> None:
    sop, orchestrator = _fixtures(tmp_path)
    runner = FakeRunner()
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    engine = WorkflowEngine(store=store, runner=runner, validator=AcceptAllValidator())

    sop_run = engine.start_sop_run(
        goal=orchestrator.goal,
        sop=sop,
    )
    step_run = engine.ready_step_runs(sop_run.id)[0]
    task = engine.create_task(step_run.id, role_id="researcher", title="Research")
    assignment, context = orchestrator.prepare(task)

    result = await engine.execute_task(task.id, assignment=assignment, context=context)

    assert result.task.status is TaskStatus.ACCEPTED
    assert result.artifact.accepted is True
    assert result.validation.status is ValidationStatus.ACCEPTED
    assert result.handoff is not None
    assert result.handoff.status is HandoffStatus.READY
    assert engine.ready_step_runs(sop_run.id) == []

    next_step = engine.accept_handoff(result.handoff.id)
    assert next_step.status is StepStatus.READY
    assert runner.calls == [task.id]
    assert [event.event_type for event in store.replay_events(sop_run.id)] == [
        "sop_started",
        "step_started",
        "task_created",
        "attempt_created",
        "task_started",
        "artifact_created",
        "validation_completed",
        "task_accepted",
        "step_completed",
        "handoff_created",
        "handoff_accepted",
        "step_ready",
    ]


@pytest.mark.asyncio
async def test_rejected_validation_blocks_handoff_and_next_step(tmp_path: Path) -> None:
    sop, orchestrator = _fixtures(tmp_path)
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    engine = WorkflowEngine(
        store=store, runner=FakeRunner(), validator=RejectValidator()
    )
    sop_run = engine.start_sop_run(goal=orchestrator.goal, sop=sop)
    task = engine.create_task(
        engine.ready_step_runs(sop_run.id)[0].id,
        role_id="researcher",
        title="Research",
    )
    assignment, context = orchestrator.prepare(task)

    result = await engine.execute_task(task.id, assignment=assignment, context=context)

    assert result.task.status is TaskStatus.REJECTED
    assert result.artifact.accepted is False
    assert result.handoff is None
    assert engine.ready_step_runs(sop_run.id) == []
    assert [event.event_type for event in store.replay_events(sop_run.id)][-2:] == [
        "validation_rejected",
        "task_rejected",
    ]

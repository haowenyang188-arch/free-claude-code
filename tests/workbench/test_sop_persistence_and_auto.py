from __future__ import annotations

from pathlib import Path

import pytest

from workbench.backend.artifacts.store import ArtifactStoreError, FileArtifactStore
from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    ContextPackage,
    Goal,
    Project,
    SopDefinition,
    StageDefinition,
    StepDefinition,
    SubagentAssignment,
    Task,
    ValidationResult,
    ValidationStatus,
)
from workbench.backend.persistence.sqlite_store import SQLiteWorkflowStore
from workbench.backend.workflow.engine import (
    AcceptanceValidator,
    SubagentRunner,
    WorkflowEngine,
)
from workbench.backend.workflow.orchestrator import AutoOrchestrator


class _Runner(SubagentRunner):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(
        self, *, task: Task, assignment: SubagentAssignment, context: ContextPackage
    ) -> Artifact:
        self.calls.append(task.id)
        return Artifact(
            id=f"artifact-{task.id}",
            task_id=task.id,
            type=ArtifactType.TEXT,
            content=task.title,
        )


class _Accept(AcceptanceValidator):
    async def validate(self, *, task: Task, artifact: Artifact) -> ValidationResult:
        return ValidationResult(
            id=f"validation-{task.id}",
            task_id=task.id,
            artifact_id=artifact.id,
            validator_id="test",
            status=ValidationStatus.ACCEPTED,
        )


def test_sqlite_store_round_trips_entities_and_jsonl_events(tmp_path: Path) -> None:
    store = SQLiteWorkflowStore(tmp_path / "workflow.db", tmp_path / "events.jsonl")
    store.save_entity("projects", Project(id="project-1", name="Demo"))
    first = store.append_event(
        stream_id="run-1",
        event_type="started",
        payload={"api_token": "secret", "safe": True},
    )
    store.close()

    restored = SQLiteWorkflowStore(tmp_path / "workflow.db", tmp_path / "events.jsonl")
    assert restored.get_entity("projects", "project-1")["name"] == "Demo"
    assert restored.replay_events("run-1", after=first.sequence)[0:] == []
    assert restored.replay_events("run-1")[0].payload["api_token"] == "[redacted]"
    restored.close()


def test_artifact_store_hashes_payload_and_rejects_mutation(tmp_path: Path) -> None:
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    artifact = artifacts.put(
        Artifact(id="report-1", task_id="task-1", type=ArtifactType.RESEARCH_REPORT),
        "hello",
    )

    assert artifact.sha256
    assert artifacts.get(artifact) == b"hello"
    with pytest.raises(ArtifactStoreError, match="already exists"):
        artifacts.put(
            Artifact(
                id="report-1", task_id="task-1", type=ArtifactType.RESEARCH_REPORT
            ),
            "changed",
        )

    payload = (tmp_path / "artifacts" / artifact.uri).resolve()
    payload.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactStoreError, match="checksum"):
        artifacts.get(artifact)


@pytest.mark.asyncio
async def test_auto_orchestrator_advances_handoff_until_review_gate(
    tmp_path: Path,
) -> None:
    project = Project(id="project-1", name="Demo")
    goal = Goal(id="goal-1", project_id=project.id, description="Ship the feature")
    sop = SopDefinition(
        id="sop-1",
        name="Research then review",
        stages=[
            StageDefinition(
                id="stage-1",
                name="Delivery",
                steps=[
                    StepDefinition(
                        id="research",
                        name="Research",
                        role_id="researcher",
                        output_type=ArtifactType.RESEARCH_REPORT,
                        handoff_to="review",
                    ),
                    StepDefinition(
                        id="review",
                        name="Review",
                        role_id="reviewer",
                        depends_on=["research"],
                        requires_review=True,
                    ),
                ],
            )
        ],
    )
    store = SQLiteWorkflowStore(tmp_path / "workflow.db", tmp_path / "events.jsonl")
    runner = _Runner()
    engine = WorkflowEngine(
        store=store,
        runner=runner,
        validator=_Accept(),
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
    )
    sop_run = engine.start_sop_run(goal=goal, sop=sop)
    orchestrator = AutoOrchestrator(engine)

    def assignment(task: Task, step: StepDefinition) -> SubagentAssignment:
        return SubagentAssignment(
            id=f"assignment-{task.id}",
            task_id=task.id,
            role_id=step.role_id,
            agent_instance_id="agent-1",
            runtime_id="codex-runtime",
        )

    def context(task: Task, step: StepDefinition) -> ContextPackage:
        return ContextPackage(
            id=f"context-{task.id}",
            task_id=task.id,
            goal_summary=goal.description,
            instructions=step.instructions or step.name,
        )

    results = await orchestrator.run_until_gate(
        sop_run.id,
        resolve_assignment=assignment,
        build_context=context,
    )

    assert len(results) == 2
    assert len(runner.calls) == 2
    assert any(
        item.get("status") == "waiting_review"
        for item in store.list_entities("step_runs")
    )
    assert store.list_entities("reviews")
    assert store.get_entity("sop_runs", sop_run.id)["status"] == "waiting_review"
    store.close()

"""Phase 3 (Lineage + Evidence Gate) validation tests.

Verifies:
- artifact -> task -> attempt -> step_run -> sop_run lineage
- PASS evidence requirements (DIFF + TEST_REPORT from the SAME attempt,
  accepted, evidence_ids exact, REVIEW_REPORT from the reviewer, legal role)
- fail-closed policy: PASS with incomplete evidence is POLICY_BLOCKED
  (reviewer_output preserved; policy_decision=BLOCKED; route HUMAN)
"""

from __future__ import annotations

import json

import pytest

from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    Attempt,
    ContextPackage,
    Goal,
    Review,
    ReviewStatus,
    SopDefinition,
    StageDefinition,
    StepDefinition,
    StepRun,
    SubagentAssignment,
    Task,
    ValidationResult,
    ValidationStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.adapter import RuntimeExecutionResult
from workbench.backend.workflow.engine import (
    AcceptanceValidator,
    WorkflowEngine,
)
from workbench.backend.workflow.lineage import (
    LineageError,
    validate_artifact_lineage,
    validate_review_evidence,
)
from workbench.backend.workflow.orchestrator import AutoOrchestrator


class AcceptAll(AcceptanceValidator):
    async def validate(self, *, task, artifact) -> ValidationResult:
        return ValidationResult(
            id=f"v-{task.id}",
            task_id=task.id,
            artifact_id=artifact.id,
            validator_id="test",
            status=ValidationStatus.ACCEPTED,
        )


class PassAdapter:
    """Full evidence: plan -> DIFF+TEST_REPORT -> PASS review."""

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        if task.role_id == "claude":
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"plan-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.PLAN,
                        content="plan",
                    )
                ]
            )
        if task.role_id == "dsh":
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"diff-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.DIFF,
                        content="+feature",
                    ),
                    Artifact(
                        id=f"test-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.TEST_REPORT,
                        content="PASS",
                    ),
                ]
            )
        return RuntimeExecutionResult(
            artifacts=[
                Artifact(
                    id=f"review-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.REVIEW_REPORT,
                    content=json.dumps(
                        {
                            "result": "PASS",
                            "blocking": [],
                            "non_blocking": [],
                            "evidence": ["deterministic test"],
                        }
                    ),
                )
            ]
        )


class MissingTestAdapter:
    """Executor emits ONLY a DIFF - no TEST_REPORT (evidence incomplete)."""

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        if task.role_id == "claude":
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"plan-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.PLAN,
                        content="plan",
                    )
                ]
            )
        if task.role_id == "dsh":
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"diff-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.DIFF,
                        content="+feature",
                    )
                ]
            )
        return RuntimeExecutionResult(
            artifacts=[
                Artifact(
                    id=f"review-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.REVIEW_REPORT,
                    content=json.dumps(
                        {
                            "result": "PASS",
                            "blocking": [],
                            "non_blocking": [],
                            "evidence": ["deterministic test"],
                        }
                    ),
                )
            ]
        )


def _sop() -> tuple[Goal, SopDefinition]:
    goal = Goal(id="g-lg", project_id="p", description="lineage")
    sop = SopDefinition(
        id="sop-lg",
        name="plan execute review",
        stages=[
            StageDefinition(
                id="s1",
                name="main",
                steps=[
                    StepDefinition(
                        id="plan",
                        name="Plan",
                        role_id="claude",
                        output_type=ArtifactType.PLAN,
                        handoff_to="execute",
                    ),
                    StepDefinition(
                        id="execute",
                        name="Execute",
                        role_id="dsh",
                        output_type=ArtifactType.IMPLEMENTATION,
                        depends_on=["plan"],
                        handoff_to="review",
                    ),
                    StepDefinition(
                        id="review",
                        name="Review",
                        role_id="codex",
                        output_type=ArtifactType.REVIEW_REPORT,
                        depends_on=["execute"],
                    ),
                ],
            )
        ],
    )
    return goal, sop


def _resolvers():
    def resolve_assignment(task, step) -> SubagentAssignment:
        return SubagentAssignment(
            id=f"assign-{task.id}",
            task_id=task.id,
            role_id=task.role_id,
            agent_instance_id="agent",
            runtime_id=task.role_id,
        )

    def build_context(task, step) -> ContextPackage:
        return ContextPackage(
            id=f"ctx-{task.id}",
            task_id=task.id,
            goal_summary="lineage",
            instructions=step.instructions or step.name,
        )

    return resolve_assignment, build_context


def _engine(tmp_path, adapter) -> tuple[WorkflowEngine, JsonWorkflowStore]:
    store = JsonWorkflowStore(tmp_path / "s.json", tmp_path / "e.jsonl")
    engine = WorkflowEngine(
        store=store,
        runners={"claude": adapter, "dsh": adapter, "codex": adapter},
        validator=AcceptAll(),
    )
    return engine, store



def test_validate_artifact_lineage_complete():
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as td:
        store2 = JsonWorkflowStore(_P(td) / "s.json", _P(td) / "e.jsonl")
        store2.save_entity("sop_runs", {"id": "sop1", "status": "running"})
        store2.save_entity(
            "step_runs",
            StepRun(id="step-exec", sop_run_id="sop1", step_id="execute"),
        )
        store2.save_entity(
            "tasks", Task(id="exec", step_run_id="step-exec", role_id="dsh")
        )
        store2.save_entity("attempts", Attempt(id="att1", task_id="exec"))
        store2.save_entity(
            "artifacts",
            Artifact(
                id="diff-1", task_id="exec", attempt_id="att1",
                type=ArtifactType.DIFF,
            ),
        )
        lineage = validate_artifact_lineage(
            artifact=Artifact.model_validate(store2.get_entity("artifacts", "diff-1")),
            store=store2,
        )
        assert lineage["task_id"] == "exec"
        assert lineage["attempt_id"] == "att1"
        assert lineage["step_run_id"] == "step-exec"
        assert lineage["sop_run_id"] == "sop1"


def test_validate_artifact_lineage_missing_task():
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as td:
        store = JsonWorkflowStore(_P(td) / "s.json", _P(td) / "e.jsonl")
        artifact = Artifact(id="a1", task_id="nope", type=ArtifactType.DIFF)
        with pytest.raises(LineageError, match="non-existent task"):
            validate_artifact_lineage(artifact=artifact, store=store)


def test_validate_review_evidence_complete():
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as td:
        store = JsonWorkflowStore(_P(td) / "s.json", _P(td) / "e.jsonl")
        store.save_entity("sop_runs", {"id": "sop1", "status": "running"})
        store.save_entity("step_runs", StepRun(id="step-exec", sop_run_id="sop1", step_id="execute"))
        store.save_entity("step_runs", StepRun(id="step-review", sop_run_id="sop1", step_id="review"))
        store.save_entity("tasks", Task(id="exec", step_run_id="step-exec", role_id="dsh"))
        store.save_entity("tasks", Task(id="rev", step_run_id="step-review", role_id="codex"))
        store.save_entity("attempts", Attempt(id="att1", task_id="exec"))
        for art in [
            Artifact(id="diff-1", task_id="exec", attempt_id="att1", type=ArtifactType.DIFF, accepted=True),
            Artifact(id="test-1", task_id="exec", attempt_id="att1", type=ArtifactType.TEST_REPORT, accepted=True),
            Artifact(id="report-1", task_id="rev", type=ArtifactType.REVIEW_REPORT),
        ]:
            store.save_entity("artifacts", art)
        review = Review(
            id="rev-1", task_id="rev", artifact_id="report-1",
            reviewer_role_id="codex", reviewed_task_id="exec",
            reviewed_attempt_id="att1",
            reviewed_artifact_ids=["diff-1", "test-1"],
            evidence_ids=["diff-1", "test-1"],
        )
        bundle = validate_review_evidence(review=review, store=store)
        assert bundle.diff.id == "diff-1"
        assert bundle.test_report.id == "test-1"
        assert bundle.reviewer_report.id == "report-1"


def test_validate_review_evidence_missing_diff():
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as td:
        store = JsonWorkflowStore(_P(td) / "s.json", _P(td) / "e.jsonl")
        store.save_entity("sop_runs", {"id": "sop1", "status": "running"})
        store.save_entity("step_runs", StepRun(id="step-exec", sop_run_id="sop1", step_id="execute"))
        store.save_entity("step_runs", StepRun(id="step-review", sop_run_id="sop1", step_id="review"))
        store.save_entity("tasks", Task(id="exec", step_run_id="step-exec", role_id="dsh"))
        store.save_entity("tasks", Task(id="rev", step_run_id="step-review", role_id="codex"))
        store.save_entity("attempts", Attempt(id="att1", task_id="exec"))
        store.save_entity(
            "artifacts",
            Artifact(id="test-1", task_id="exec", attempt_id="att1", type=ArtifactType.TEST_REPORT, accepted=True),
        )
        store.save_entity(
            "artifacts",
            Artifact(id="report-1", task_id="rev", type=ArtifactType.REVIEW_REPORT),
        )
        review = Review(
            id="rev-1", task_id="rev", artifact_id="report-1",
            reviewer_role_id="codex", reviewed_task_id="exec",
            reviewed_attempt_id="att1",
            evidence_ids=["test-1"],
        )
        with pytest.raises(LineageError, match=r"missing required evidence.*DIFF"):
            validate_review_evidence(review=review, store=store)


def test_validate_review_evidence_reviewer_report_from_executor():
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as td:
        store = JsonWorkflowStore(_P(td) / "s.json", _P(td) / "e.jsonl")
        store.save_entity("sop_runs", {"id": "sop1", "status": "running"})
        store.save_entity("step_runs", StepRun(id="step-exec", sop_run_id="sop1", step_id="execute"))
        store.save_entity("step_runs", StepRun(id="step-review", sop_run_id="sop1", step_id="review"))
        store.save_entity("tasks", Task(id="exec", step_run_id="step-exec", role_id="dsh"))
        store.save_entity("tasks", Task(id="rev", step_run_id="step-review", role_id="codex"))
        store.save_entity("attempts", Attempt(id="att1", task_id="exec"))
        for art in [
            Artifact(id="diff-1", task_id="exec", attempt_id="att1", type=ArtifactType.DIFF, accepted=True),
            Artifact(id="test-1", task_id="exec", attempt_id="att1", type=ArtifactType.TEST_REPORT, accepted=True),
            # REVIEW_REPORT masquerading as the executor's artifact
            Artifact(id="report-1", task_id="exec", type=ArtifactType.REVIEW_REPORT),
        ]:
            store.save_entity("artifacts", art)
        review = Review(
            id="rev-1", task_id="rev", artifact_id="report-1",
            reviewer_role_id="codex", reviewed_task_id="exec",
            reviewed_attempt_id="att1",
            evidence_ids=["diff-1", "test-1"],
        )
        with pytest.raises(LineageError, match="not the reviewer task"):
            validate_review_evidence(review=review, store=store)


@pytest.mark.asyncio
async def test_engine_pass_with_complete_evidence_approved(tmp_path):
    engine, store = _engine(tmp_path, PassAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    await AutoOrchestrator(engine).run_until_gate(
        run.id, resolve_assignment=_resolvers()[0], build_context=_resolvers()[1]
    )
    review = Review.model_validate(store.list_entities("reviews")[0])
    assert review.status is ReviewStatus.APPROVED
    assert review.reviewer_output == "PASS"
    assert review.policy_decision == "APPROVED"
    assert set(review.evidence_ids)
    assert store.get_entity("sop_runs", run.id)["status"] == "completed"


@pytest.mark.asyncio
async def test_engine_pass_with_missing_evidence_policy_blocked(tmp_path):
    """PASS with incomplete evidence is fail-closed: POLICY_BLOCKED, HUMAN."""
    engine, store = _engine(tmp_path, MissingTestAdapter())
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    await AutoOrchestrator(engine).run_until_gate(
        run.id, resolve_assignment=_resolvers()[0], build_context=_resolvers()[1]
    )
    review = Review.model_validate(store.list_entities("reviews")[0])
    # Reviewer said PASS; policy overrides the ROUTE but never the outcome.
    assert review.reviewer_output == "PASS"
    assert review.policy_decision == "BLOCKED"
    assert review.status is ReviewStatus.POLICY_BLOCKED
    assert "Evidence gate failed" in (review.feedback or "")
    # Run parks at the human gate, not completed/failed.
    assert store.get_entity("sop_runs", run.id)["status"] == "waiting_review"
    assert review.reviewed_attempt_id is not None



def test_validate_review_evidence_empty_evidence_ids_blocked():
    """A PASS review MUST record which evidence it validated (fail closed)."""
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as td:
        store = JsonWorkflowStore(_P(td) / "s.json", _P(td) / "e.jsonl")
        store.save_entity("sop_runs", {"id": "sop1", "status": "running"})
        store.save_entity(
            "step_runs", StepRun(id="step-exec", sop_run_id="sop1", step_id="execute")
        )
        store.save_entity(
            "step_runs", StepRun(id="step-review", sop_run_id="sop1", step_id="review")
        )
        store.save_entity(
            "tasks", Task(id="exec", step_run_id="step-exec", role_id="dsh")
        )
        store.save_entity(
            "tasks", Task(id="rev", step_run_id="step-review", role_id="codex")
        )
        store.save_entity("attempts", Attempt(id="att1", task_id="exec"))
        for art in [
            Artifact(
                id="diff-1", task_id="exec", attempt_id="att1",
                type=ArtifactType.DIFF, accepted=True,
            ),
            Artifact(
                id="test-1", task_id="exec", attempt_id="att1",
                type=ArtifactType.TEST_REPORT, accepted=True,
            ),
            Artifact(id="report-1", task_id="rev", type=ArtifactType.REVIEW_REPORT),
        ]:
            store.save_entity("artifacts", art)
        review = Review(
            id="rev-1", task_id="rev", artifact_id="report-1",
            reviewer_role_id="codex", reviewed_task_id="exec",
            reviewed_attempt_id="att1",
            # evidence_ids deliberately empty
        )
        with pytest.raises(LineageError, match="no claimed evidence_ids"):
            validate_review_evidence(review=review, store=store)

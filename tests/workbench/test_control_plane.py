"""Phase 6: Control Plane / Observability - queryable run views."""

from __future__ import annotations

import json

import pytest

from tests.workbench.deterministic_harness import (
    DeterministicScenarioHarness,
)
from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    ContextPackage,
    Goal,
    Review,
    ReviewStatus,
    SopDefinition,
    StageDefinition,
    StepDefinition,
    SubagentAssignment,
    ValidationResult,
    ValidationStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.adapter import RuntimeExecutionResult
from workbench.backend.workflow.engine import (
    AcceptanceValidator,
    WorkflowEngine,
)
from workbench.backend.workflow.observability import (
    collect_global_overview,
    collect_provider_health,
    collect_rework_lineage,
    collect_run_snapshot,
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


class MissingTestAdapter:
    """Executor emits only DIFF - no TEST_REPORT (policy-blocked PASS)."""

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
                        {"result": "PASS", "blocking": [], "evidence": []}
                    ),
                )
            ]
        )


def _sop() -> tuple[Goal, SopDefinition]:
    goal = Goal(id="g-cp", project_id="p", description="control plane")
    sop = SopDefinition(
        id="sop-cp",
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
            goal_summary="control plane",
            instructions=step.instructions or step.name,
        )

    return resolve_assignment, build_context


@pytest.mark.asyncio
async def test_snapshot_rework_then_pass(tmp_path):
    harness = DeterministicScenarioHarness(tmp_path)
    await harness.run_rework_then_pass()
    snap = collect_run_snapshot(sop_run_id=harness.run.id, store=harness.store)

    assert snap["status"] == "completed"
    assert snap["counts"]["attempts"] == 5
    assert len(snap["reviews"]) == 2
    assert snap["policy_gates"] == []
    # executor attempts carry session ids (DSH continuity visible)
    dsh_attempts = [
        a
        for a in snap["attempts"]
        if any(t["task_id"] == a["task_id"] for t in snap["tasks"] if t["role_id"] == "dsh")
    ]
    sessions = {a["session_id"] for a in dsh_attempts}
    assert len(sessions) == 1

    lineage = collect_rework_lineage(sop_run_id=harness.run.id, store=harness.store)
    execute_chain = next(item for item in lineage if item["step_id"] == "execute")
    assert len(execute_chain["chain"]) == 2  # original + retry
    assert execute_chain["chain"][1]["task_id"]  # retry task present

    health = collect_provider_health(store=harness.store, runners=harness.engine._runners)
    by_id = {item["runtime_id"]: item for item in health}
    assert set(by_id) >= {"claude", "dsh", "codex"}
    assert by_id["dsh"]["last_session_id"] is not None

    overview = collect_global_overview(store=harness.store)
    assert overview["total_sop_runs"] == 1
    assert overview["completed_runs"] == 1
    # executor retry + reviewer retry both carry previous_attempt_id links
    assert overview["rework_attempt_count"] == 2


@pytest.mark.asyncio
async def test_snapshot_policy_blocked(tmp_path):
    """Policy-blocked run: snapshot exposes the gate and the error reason."""
    store = JsonWorkflowStore(tmp_path / "s.json", tmp_path / "e.jsonl")
    adapter = MissingTestAdapter()
    engine = WorkflowEngine(
        store=store,
        runners={"claude": adapter, "dsh": adapter, "codex": adapter},
        validator=AcceptAll(),
    )
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)
    await AutoOrchestrator(engine).run_until_gate(
        run.id, resolve_assignment=_resolvers()[0], build_context=_resolvers()[1]
    )

    snap = collect_run_snapshot(sop_run_id=run.id, store=store)
    assert snap["status"] == "waiting_review"
    # Parked run must still name the task/attempt under review (P1).
    assert snap["current_task"] is not None
    assert snap["current_task"]["role_id"] == "dsh"
    assert snap["current_attempt"] is not None
    assert len(snap["policy_gates"]) == 1
    gate = snap["policy_gates"][0]
    assert gate["reviewer_output"] == "PASS"
    assert gate["policy_decision"] == "BLOCKED"
    assert gate["status"] == ReviewStatus.POLICY_BLOCKED.value
    assert any("Evidence gate failed" in item["reason"] for item in snap["error_reasons"])

    review = Review.model_validate(store.list_entities("reviews")[0])
    assert review.reviewed_attempt_id is not None  # current-attempt binding visible


@pytest.mark.asyncio
async def test_snapshot_unknown_run(tmp_path):
    store = JsonWorkflowStore(tmp_path / "s.json", tmp_path / "e.jsonl")
    snap = collect_run_snapshot(sop_run_id="nope", store=store)
    assert snap["error"] == "run not found"

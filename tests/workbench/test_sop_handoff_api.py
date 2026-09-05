"""HTTP contract tests for explicit SOP Handoff dispatch and safe trace views."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from workbench.backend.artifacts.store import FileArtifactStore
from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    Goal,
    GoalStatus,
    Handoff,
    HandoffMessageType,
    SopDefinition,
    StageDefinition,
    StepDefinition,
    SubagentAssignment,
)
from workbench.backend.main import app, service
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.validation.always_accept import AlwaysAcceptValidator
from workbench.backend.workflow.adapter import RuntimeExecutionResult
from workbench.backend.workflow.context_builder import ContextPackageBuilder
from workbench.backend.workflow.dispatcher import HandoffDispatcher
from workbench.backend.workflow.engine import WorkflowEngine
from workbench.backend.workflow.orchestrator import AutoOrchestrator


class _DeterministicRuntimes:
    """Small local adapter set: no CLI, network, or workspace mutation."""

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        if assignment.runtime_id == "claude":
            artifacts = [
                Artifact(
                    id=f"plan-artifact-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.PLAN,
                    content="private plan reasoning must not appear in trace",
                    summary="A safe plan summary",
                )
            ]
        elif assignment.runtime_id == "dsh":
            artifacts = [
                Artifact(
                    id=f"execution-diff-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.DIFF,
                    content="+implemented = True",
                    summary="DIFF",
                ),
                Artifact(
                    id=f"execution-test-report-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.TEST_REPORT,
                    content="PASS",
                    summary="TEST_REPORT",
                ),
            ]
        elif assignment.runtime_id == "codex":
            artifacts = [
                Artifact(
                    id=f"codex-review-report-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.REVIEW_REPORT,
                    content='{"result":"REWORK","blocking":["fix the edge case"],"non_blocking":[],"evidence":["execution-diff"]}',
                    summary="REVIEW: REWORK",
                )
            ]
        else:  # pragma: no cover - fixture only dispatches Claude -> DSH
            raise AssertionError(f"unexpected runtime: {assignment.runtime_id}")
        return RuntimeExecutionResult(
            artifacts=artifacts,
            session_id=f"runtime-session-{assignment.runtime_id}",
        )


def _assignment_for(role_id: str, run_id: str) -> SubagentAssignment:
    runtime_by_role = {"claude": "claude", "dsh": "dsh", "codex": "codex"}
    return SubagentAssignment(
        id=f"assignment-{role_id}-{run_id}",
        task_id="pending",
        role_id=role_id,
        agent_instance_id=f"agent-{role_id}",
        runtime_id=runtime_by_role[role_id],
    )


@pytest.fixture
def dispatchable_plan_handoff(tmp_path) -> Generator[dict[str, Any]]:
    """Seed a completed Claude plan whose READY handoff targets DSH."""
    original = {
        "workflow_store": service.workflow_store,
        "artifact_store": service.artifact_store,
        "workflow_engine": service.workflow_engine,
        "context_builder": service.context_builder,
        "orchestrator": service.orchestrator,
        "handoff_dispatcher": getattr(service, "handoff_dispatcher", None),
        "sop_definitions": service.sop_definitions,
        "sop_runtime_mode": getattr(service, "sop_runtime_mode", "fake"),
    }
    store = JsonWorkflowStore(
        state_path=tmp_path / "workflow_state.json",
        event_path=tmp_path / "workflow_events.jsonl",
    )
    artifact_store = FileArtifactStore(tmp_path / "artifacts")
    runtimes = _DeterministicRuntimes()
    engine = WorkflowEngine(
        store=store,
        runners={"claude": runtimes, "dsh": runtimes, "codex": runtimes},
        validator=AlwaysAcceptValidator(),
        artifact_store=artifact_store,
    )
    goal = Goal(
        id="goal-1",
        project_id="project-1",
        description="Implement the requested change",
        status=GoalStatus.RUNNING,
        created_at=datetime.now(UTC),
    )
    sop = SopDefinition(
        id="sop-1",
        name="Claude to DSH",
        stages=[
            StageDefinition(
                id="stage-1",
                name="Delivery",
                steps=[
                    StepDefinition(
                        id="plan",
                        name="Plan",
                        role_id="claude",
                        output_type=ArtifactType.PLAN,
                        handoff_to="execute",
                        instructions="Create an implementation plan",
                    ),
                    StepDefinition(
                        id="execute",
                        name="Execute",
                        role_id="dsh",
                        depends_on=["plan"],
                        handoff_to="review",
                        instructions="Execute the accepted plan",
                    ),
                    StepDefinition(
                        id="review",
                        name="Review",
                        role_id="codex",
                        depends_on=["execute"],
                        requires_review=True,
                        instructions="Review the DSH artifacts",
                    ),
                ],
            )
        ],
    )
    run = engine.start_sop_run(goal=goal, sop=sop)
    planner_task = engine.create_task(f"{run.id}:plan", role_id="claude")
    context = ContextPackageBuilder(store=store, artifact_store=artifact_store).build_for_task(
        task=planner_task,
        step=sop.stages[0].steps[0],
        goal=goal,
        sop=sop,
    )
    planner_result = asyncio_run(engine.execute_task(
        planner_task.id,
        assignment=SubagentAssignment(
            id="assignment-plan",
            task_id=planner_task.id,
            role_id="claude",
            agent_instance_id="agent-claude",
            runtime_id="claude",
        ),
        context=context,
    ))
    handoff = planner_result.handoff
    assert handoff is not None
    handoff.message_type = HandoffMessageType.PLAN_READY
    handoff.brief = "Execute the approved plan"
    handoff.correlation_id = "correlation-1"
    store.save_entity("handoffs", handoff)

    service.workflow_store = store
    service.artifact_store = artifact_store
    service.workflow_engine = engine
    service.context_builder = ContextPackageBuilder(store=store, artifact_store=artifact_store)
    service.orchestrator = AutoOrchestrator(engine)
    service.handoff_dispatcher = HandoffDispatcher(
        engine=engine,
        runtime_registry={
            "claude": "claude_code",
            "dsh": "deepseek_harness",
            "codex": "codex",
        },
        resolve_assignment=_assignment_for,
    )
    service.sop_definitions = {sop.id: sop}
    service.sop_runtime_mode = "fake"
    try:
        yield {"run_id": run.id, "handoff_id": handoff.id, "store": store}
    finally:
        for name, value in original.items():
            setattr(service, name, value)


def asyncio_run(awaitable):
    """Run fixture setup work without depending on a running pytest loop."""
    import asyncio

    return asyncio.run(awaitable)


@pytest.mark.asyncio
async def test_handoff_projection_and_trace_are_auditable_without_provider_content(
    dispatchable_plan_handoff,
):
    run_id = dispatchable_plan_handoff["run_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        handoffs = await client.get(f"/api/sop-runs/{run_id}/handoffs")
        trace = await client.get(f"/api/sop-runs/{run_id}/trace")

    assert handoffs.status_code == 200
    item = handoffs.json()[0]
    assert item["message_type"] == "plan_ready"
    assert item["brief"] == "Execute the approved plan"
    assert item["correlation_id"] == "correlation-1"
    assert item["source_role_id"] == "claude"
    assert item["target_role_id"] == "dsh"
    assert item["target_runtime_id"] == "dsh"
    assert item["dispatchable"] is True

    assert trace.status_code == 200
    body = trace.json()
    assert body["mode"] == "workflow_events_only"
    assert body["provider_events_available"] is False
    assert body["empty"] is False
    assert any(entry["phase"] == "plan" and entry["role_id"] == "claude" for entry in body["entries"])
    assert "private plan reasoning" not in str(body)
    assert all("payload" not in entry for entry in body["entries"])


@pytest.mark.asyncio
async def test_plan_ready_dispatch_runs_only_the_server_selected_dsh_target(
    dispatchable_plan_handoff,
):
    run_id = dispatchable_plan_handoff["run_id"]
    handoff_id = dispatchable_plan_handoff["handoff_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/sop-runs/{run_id}/handoffs/{handoff_id}/dispatch",
            json={"runtime_id": "claude", "workspace_path": "/not-accepted"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "dispatched"
    assert body["runtime_mode"] == "fake"
    assert body["target_role_id"] == "dsh"
    assert body["target_runtime_id"] == "dsh"
    assert body["task_id"]
    assert body["attempt_id"]
    assert body["event_id"]
    assert body["session_id"] == "runtime-session-dsh"

    handoff = dispatchable_plan_handoff["store"].get_entity("handoffs", handoff_id)
    assert handoff["status"] == "accepted"


@pytest.mark.asyncio
async def test_dispatch_rejects_unroutable_message_without_accepting_handoff(
    dispatchable_plan_handoff,
):
    run_id = dispatchable_plan_handoff["run_id"]
    handoff_id = dispatchable_plan_handoff["handoff_id"]
    store = dispatchable_plan_handoff["store"]
    handoff = Handoff.model_validate(store.get_entity("handoffs", handoff_id))
    handoff.message_type = HandoffMessageType.HANDOFF
    store.save_entity("handoffs", handoff)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/sop-runs/{run_id}/handoffs/{handoff_id}/dispatch")

    assert response.status_code == 422
    assert store.get_entity("handoffs", handoff_id)["status"] == "ready"


@pytest.mark.asyncio
async def test_codex_rework_handoff_can_be_sent_back_to_dsh(dispatchable_plan_handoff):
    run_id = dispatchable_plan_handoff["run_id"]
    plan_handoff_id = dispatchable_plan_handoff["handoff_id"]
    store = dispatchable_plan_handoff["store"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        plan_dispatch = await client.post(
            f"/api/sop-runs/{run_id}/handoffs/{plan_handoff_id}/dispatch"
        )
        assert plan_dispatch.status_code == 200
        review_handoff_id = plan_dispatch.json()["outgoing_handoff_id"]
        assert review_handoff_id

        review_dispatch = await service._handoff_dispatcher().dispatch(review_handoff_id)
        review_id = next(
            row["id"]
            for row in store.list_entities("reviews")
            if row["task_id"] == review_dispatch.execution_result.task.id
        )
        decision = service.workflow_engine.apply_review_outcome(review_id, verdict="REWORK")
        assert decision.handoff_id

        response = await client.post(
            f"/api/sop-runs/{run_id}/handoffs/{decision.handoff_id}/dispatch"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["target_role_id"] == "dsh"
    assert body["target_runtime_id"] == "dsh"
    assert body["runtime_mode"] == "fake"
    assert store.get_entity("handoffs", decision.handoff_id)["status"] == "accepted"

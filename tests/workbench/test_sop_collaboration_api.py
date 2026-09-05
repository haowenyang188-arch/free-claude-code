"""Collaboration API contracts: visible handoffs and safe DSH dispatch gating."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from workbench.backend.artifacts.store import FileArtifactStore
from workbench.backend.domain.models import (
    ArtifactType,
    ContextPackage,
    Goal,
    SopDefinition,
    StageDefinition,
    StepDefinition,
)
from workbench.backend.main import app, service
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.validation.always_accept import AlwaysAcceptValidator
from workbench.backend.workflow.context_builder import ContextPackageBuilder
from workbench.backend.workflow.engine import WorkflowEngine
from workbench.backend.workflow.orchestrator import AutoOrchestrator
from workbench.backend.workflow.runner_factory import create_runners


@pytest.fixture
def collaboration_service(tmp_path):
    originals = {
        "workflow_store": service.workflow_store,
        "artifact_store": service.artifact_store,
        "workflow_engine": service.workflow_engine,
        "context_builder": service.context_builder,
        "orchestrator": service.orchestrator,
        "sop_definitions": service.sop_definitions,
        "sop_runtime_mode": getattr(service, "sop_runtime_mode", None),
        "manual_handoffs": getattr(service, "manual_handoffs", None),
        "background": service._background_sop_tasks,
    }
    root = tmp_path / "sop"
    root.mkdir()
    service.workflow_store = JsonWorkflowStore(root / "state.json", root / "events.jsonl")
    service.artifact_store = FileArtifactStore(root / "artifacts")
    service.workflow_engine = WorkflowEngine(
        store=service.workflow_store,
        runners=create_runners(mode="fake"),
        validator=AlwaysAcceptValidator(),
        artifact_store=service.artifact_store,
    )
    service.context_builder = ContextPackageBuilder(
        store=service.workflow_store,
        artifact_store=service.artifact_store,
    )
    service.orchestrator = AutoOrchestrator(
        service.workflow_engine,
        manual_handoffs=True,
    )
    service.sop_runtime_mode = "fake"
    service.manual_handoffs = True
    service._background_sop_tasks = {}
    service.sop_definitions = {}

    yield service

    service.workflow_store = originals["workflow_store"]
    service.artifact_store = originals["artifact_store"]
    service.workflow_engine = originals["workflow_engine"]
    service.context_builder = originals["context_builder"]
    service.orchestrator = originals["orchestrator"]
    service.sop_definitions = originals["sop_definitions"]
    service.sop_runtime_mode = originals["sop_runtime_mode"]
    service.manual_handoffs = originals["manual_handoffs"]
    service._background_sop_tasks = originals["background"]


async def _create_ready_plan_handoff() -> tuple[str, str]:
    definition = SopDefinition(
        id="sop-collaboration",
        name="Claude to DSH",
        stages=[
            StageDefinition(
                id="main",
                name="main",
                steps=[
                    StepDefinition(
                        id="plan",
                        name="Plan",
                        role_id="claude",
                        output_type=ArtifactType.PLAN,
                        handoff_to="execute",
                        instructions="Create a plan.",
                    ),
                    StepDefinition(
                        id="execute",
                        name="Execute",
                        role_id="dsh",
                        output_type=ArtifactType.IMPLEMENTATION,
                        depends_on=["plan"],
                    ),
                ],
            )
        ],
    )
    goal = Goal(id=str(uuid.uuid4()), project_id="test", description="Test collaboration")
    run = service.workflow_engine.start_sop_run(goal=goal, sop=definition)
    step_run = service.workflow_engine.ready_step_runs(run.id)[0]
    task = service.workflow_engine.create_task(step_run.id, role_id="claude")
    assignment = service._default_assignment_resolver(task, definition.stages[0].steps[0])
    context = ContextPackage(
        id=str(uuid.uuid4()),
        task_id=task.id,
        goal_summary=goal.description,
        instructions="Create a plan.",
    )
    result = await service.workflow_engine.execute_task(
        task.id,
        assignment=assignment,
        context=context,
    )
    assert result.handoff is not None
    return run.id, result.handoff.id


@pytest.mark.asyncio
async def test_handoff_projection_and_fake_dispatch(collaboration_service):
    run_id, handoff_id = await _create_ready_plan_handoff()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        handoffs = await client.get(f"/api/sop-runs/{run_id}/handoffs")
        assert handoffs.status_code == 200
        item = handoffs.json()[0]
        assert item["id"] == handoff_id
        assert item["message_type"] == "plan_ready"
        assert item["from_role_id"] == "claude"
        assert item["to_role_id"] == "dsh"
        assert item["brief"]
        assert item["dispatchable"] is True

        dispatched = await client.post(
            f"/api/sop-runs/{run_id}/handoffs/{handoff_id}/dispatch"
        )
        assert dispatched.status_code == 200
        body = dispatched.json()
        assert body["sop_run_id"] == run_id
        assert body["handoff_id"] == handoff_id
        assert body["status"] == "dispatched"
        assert body["target_role_id"] == "dsh"
        assert body["runtime_mode"] == "fake"

        after = await client.get(f"/api/sop-runs/{run_id}/handoffs")
        assert after.status_code == 200
        assert after.json()[0]["status"] == "accepted"


@pytest.mark.asyncio
async def test_real_mode_dispatch_uses_engine_owned_handoff_path(
    collaboration_service, monkeypatch
):
    run_id, handoff_id = await _create_ready_plan_handoff()
    collaboration_service.sop_runtime_mode = "real"

    async def dsh_available() -> None:
        return None

    monkeypatch.setattr(collaboration_service, "_ensure_dsh_dispatch_available", dsh_available)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/sop-runs/{run_id}/handoffs/{handoff_id}/dispatch"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["sop_run_id"] == run_id
        assert body["handoff_id"] == handoff_id
        assert body["status"] == "dispatched"
        assert body["target_step_id"] == "execute"
        assert body["target_role_id"] == "dsh"
        assert body["runtime_mode"] == "real"

    handoff = collaboration_service.workflow_store.get_entity("handoffs", handoff_id)
    assert handoff["status"] == "accepted"
    tasks = collaboration_service.workflow_store.list_entities("tasks")
    assert any(task["role_id"] == "dsh" for task in tasks)
    events = collaboration_service.workflow_store.replay_events(run_id)
    assert any(event.event_type == "handoff_dispatched" for event in events)

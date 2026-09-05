"""Test Engine routing preserves reviewer feedback (Problem A fix)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    Review,
    ReviewStatus,
    StepStatus,
    Task,
    TaskStatus,
)
from workbench.backend.workflow.engine import WorkflowEngine
from workbench.backend.workflow.role_contract import RouteTarget


class FakeStore:
    """Minimal in-memory store for testing."""

    def __init__(self):
        self.entities = {
            "artifacts": {},
            "reviews": {},
            "tasks": {},
            "step_runs": {},
            "sop_runs": {},
        }
        self.events = []

    def get_entity(self, collection: str, entity_id: str):
        try:
            return self.entities[collection][entity_id]
        except KeyError:
            raise KeyError(f"{collection}/{entity_id} not found")

    def save_entity(self, collection: str, entity):
        entity_dict = entity if isinstance(entity, dict) else entity.model_dump(mode="json")
        self.entities[collection][entity_dict["id"]] = entity_dict

    def list_entities(self, collection: str):
        return list(self.entities[collection].values())

    def append_event(self, stream_id: str, event_type: str, payload: dict, backend: str = "test"):
        self.events.append(
            {
                "stream_id": stream_id,
                "event_type": event_type,
                "payload": payload,
                "backend": backend,
            }
        )


class FakeRunner:
    async def execute(self, *, task, assignment, context):
        pass


class FakeValidator:
    async def validate(self, *, task, artifact):
        pass


def test_decide_review_preserves_blocking_items():
    """Verify decide_review() extracts blocking items from Review artifact."""
    store = FakeStore()
    engine = WorkflowEngine(store=store, runner=FakeRunner(), validator=FakeValidator())

    # Setup: Create a review with structured feedback in artifact
    task_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    step_run_id = f"run-1:step-execute"

    blocking_items = [
        "Missing error handling in authenticate()",
        "Type mismatch at user.ts:42",
        "Uncovered edge case: empty username",
    ]

    review_result = {
        "result": "REWORK",
        "blocking": blocking_items,
        "non_blocking": ["Consider adding JSDoc comments"],
        "evidence": ["See test failure at test_auth.py:15"],
    }

    artifact = Artifact(
        id=artifact_id,
        task_id=task_id,
        type=ArtifactType.REVIEW_REPORT,
        content=json.dumps(review_result),
        summary="REVIEW: REWORK",
        created_at=datetime.now(UTC),
        accepted=True,
    )

    task = Task(
        id=task_id,
        step_run_id=step_run_id,
        title="Implement authentication",
        description="Add user login",
        role_id="dsh_executor",
        status=TaskStatus.ACCEPTED,
    )

    review = Review(
        id=review_id,
        task_id=task_id,
        artifact_id=artifact_id,
        reviewer_role_id="codex_reviewer",
        status=ReviewStatus.PENDING,
    )

    step_run = {
        "id": step_run_id,
        "sop_run_id": "run-1",
        "step_id": "execute",
        "stage_run_id": "run-1:stage-1",
        "status": StepStatus.WAITING_REVIEW.value,
    }

    sop_run = {
        "id": "run-1",
        "goal_id": "goal-1",
        "sop_definition_id": "sop-1",
        "sop_version": "1.0",
        "definition_snapshot": {"id": "sop-1", "version": "1.0", "stages": []},
        "status": "running",
    }

    store.save_entity("artifacts", artifact)
    store.save_entity("tasks", task)
    store.save_entity("reviews", review)
    store.save_entity("step_runs", step_run)
    store.save_entity("sop_runs", sop_run)

    # Execute: Call decide_review with REWORK verdict
    result_step_run, route = engine.decide_review(review_id, verdict="REWORK")

    # Assert: Route is correct
    assert route == RouteTarget.RERUN_EXECUTE

    # Assert: Review status updated
    saved_review = Review.model_validate(store.get_entity("reviews", review_id))
    assert saved_review.status == ReviewStatus.CHANGES_REQUESTED

    # Assert: Feedback contains actual blocking items, NOT routing metadata
    assert saved_review.feedback is not None
    assert "Missing error handling" in saved_review.feedback
    assert "Type mismatch at user.ts:42" in saved_review.feedback
    assert "empty username" in saved_review.feedback

    # Assert: Feedback does NOT contain routing metadata
    assert "REWORK -> rerun_execute" not in saved_review.feedback
    assert "route.value" not in saved_review.feedback

    print(f"✅ Test passed: Blocking items preserved in feedback")
    print(f"Feedback:\n{saved_review.feedback}")


def test_decide_review_handles_missing_artifact_gracefully():
    """Verify decide_review() falls back gracefully when artifact is missing."""
    store = FakeStore()
    engine = WorkflowEngine(store=store, runner=FakeRunner(), validator=FakeValidator())

    task_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    step_run_id = f"run-1:step-execute"

    # Review without artifact
    review = Review(
        id=review_id,
        task_id=task_id,
        artifact_id="missing-artifact-id",
        reviewer_role_id="codex_reviewer",
        status=ReviewStatus.PENDING,
    )

    task = Task(
        id=task_id,
        step_run_id=step_run_id,
        title="Test task",
        description="Test",
        role_id="dsh_executor",
        status=TaskStatus.ACCEPTED,
    )

    step_run = {
        "id": step_run_id,
        "sop_run_id": "run-1",
        "step_id": "execute",
        "stage_run_id": "run-1:stage-1",
        "status": StepStatus.WAITING_REVIEW.value,
    }

    sop_run = {
        "id": "run-1",
        "goal_id": "goal-1",
        "sop_definition_id": "sop-1",
        "sop_version": "1.0",
        "definition_snapshot": {"id": "sop-1", "version": "1.0", "stages": []},
        "status": "running",
    }

    store.save_entity("reviews", review)
    store.save_entity("tasks", task)
    store.save_entity("step_runs", step_run)
    store.save_entity("sop_runs", sop_run)

    # Should not crash, should use fallback feedback
    result_step_run, route = engine.decide_review(review_id, verdict="REWORK")

    assert route == RouteTarget.RERUN_EXECUTE
    saved_review = Review.model_validate(store.get_entity("reviews", review_id))
    assert saved_review.feedback is not None
    # Fallback should provide minimal feedback
    assert len(saved_review.feedback) > 0

    print(f"✅ Test passed: Graceful fallback when artifact missing")
    print(f"Fallback feedback: {saved_review.feedback}")


if __name__ == "__main__":
    test_decide_review_preserves_blocking_items()
    test_decide_review_handles_missing_artifact_gracefully()
    print("\n✅ All tests passed!")

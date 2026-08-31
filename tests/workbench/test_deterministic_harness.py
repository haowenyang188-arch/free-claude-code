"""Phase 5: Deterministic scenario harness - rework-then-pass (100% reproducible)."""

from __future__ import annotations

import pytest

from tests.workbench.deterministic_harness import DeterministicScenarioHarness
from workbench.backend.domain.models import (
    Attempt,
)


@pytest.mark.asyncio
async def test_harness_rework_then_pass_scenario(tmp_path):
    harness = DeterministicScenarioHarness(tmp_path)
    await harness.run_rework_then_pass()

    event_types = harness.event_types()
    print("\nEVENT_SEQUENCE:", event_types)
    print("REVIEWS:", [(r.id, r.reviewer_output, r.reviewed_attempt_id) for r in harness.reviews()])
    print("ATTEMPTS:", [(a["task_id"], a["id"], a["status"], a.get("session_id")) for a in harness.attempts()])
    print("ARTIFACTS:", [(a["id"], a["type"], a["attempt_id"]) for a in harness.artifacts()])

    assert event_types[-1] == "sop_completed"
    # --- exact event order (pinned from a captured deterministic run) ---
    expected = [
        "sop_started",
        # plan step
        "step_started", "task_created", "attempt_created", "task_started",
        "artifact_created", "validation_completed", "task_accepted",
        "step_completed", "handoff_created", "handoff_accepted", "step_ready",
        # execute attempt #1 (DIFF #1 + TEST_REPORT #1)
        "step_started", "task_created", "attempt_created", "task_started",
        "artifact_created", "validation_completed", "task_accepted",
        "step_completed", "handoff_created", "handoff_accepted", "step_ready",
        # review #1 -> REWORK
        "step_started", "task_created", "attempt_created", "task_started",
        "artifact_created", "validation_completed", "review_requested",
        "sop_waiting_review", "review_rejected", "handoff_created",
        "review_routed", "handoff_accepted", "step_ready",
        # execute attempt #2 (DIFF #2 + TEST_REPORT #2, same DSH session)
        "step_started", "task_created", "attempt_created", "task_started",
        "artifact_created", "validation_completed", "task_accepted",
        "step_completed", "handoff_created", "handoff_accepted", "step_ready",
        # review #2 -> PASS -> COMPLETE
        "step_started", "task_created", "attempt_created", "task_started",
        "artifact_created", "validation_completed", "review_requested",
        "sop_waiting_review", "review_routed", "step_completed",
        "review_approved", "sop_completed",
    ]
    assert event_types == expected

    # --- Attempt #1 / #2 independence ---
    executor_tasks = [t for t in harness.tasks() if t["role_id"] == "dsh"]
    assert len(executor_tasks) == 2
    executor_attempts = [
        Attempt.model_validate(a)
        for a in harness.attempts()
        if a["task_id"] in {t["id"] for t in executor_tasks}
    ]
    assert len(executor_attempts) == 2
    assert executor_attempts[0].id != executor_attempts[1].id

    # --- Review #1 binds Attempt #1, Review #2 binds Attempt #2 ---
    reviews = harness.reviews()
    assert len(reviews) == 2
    first_task = next(t for t in executor_tasks if not t["retry_of"])
    second_task = next(t for t in executor_tasks if t["retry_of"])
    attempt_of = {a["task_id"]: a["id"] for a in harness.attempts()}
    r1, r2 = reviews
    assert r1.reviewer_output == "REWORK"
    assert r1.reviewed_attempt_id == attempt_of[first_task["id"]]
    assert r2.reviewer_output == "PASS"
    assert r2.policy_decision == "APPROVED"
    assert r2.reviewed_attempt_id == attempt_of[second_task["id"]]

    # --- final PASS binds Attempt #2 ---
    assert r2.reviewed_attempt_id == attempt_of[second_task["id"]]

    # --- DIFF #2 + TEST_REPORT #2 share one Attempt ---
    second_artifacts = [
        a for a in harness.artifacts() if a["attempt_id"] == attempt_of[second_task["id"]]
    ]
    assert {a["type"] for a in second_artifacts} >= {"diff", "test_report"}
    assert len({a["attempt_id"] for a in second_artifacts}) == 1

    # --- DSH session continuity ---
    harness.assert_dsh_session_continuity()

    # --- reviewer never modifies the workspace ---
    harness.assert_reviewer_never_writes_workspace()

    # --- terminal consistency ---
    harness.assert_terminal_consistency()

    # --- no leftover background tasks ---
    harness.assert_no_leftover_background_tasks()

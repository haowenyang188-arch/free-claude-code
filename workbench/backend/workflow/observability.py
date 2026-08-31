"""Control Plane / Observability (Phase 6).

Queryable views over SOP run state so a failing Real E2E can be pinpointed:
which Step / Attempt / Runtime / Session, which artifact is missing, which
policy gate the run is parked at, and why.
"""

from __future__ import annotations

from typing import Any

from ..domain.models import (
    Artifact,
    Attempt,
    Review,
    ReviewStatus,
    SopRun,
    SopRunStatus,
    StepRun,
    StepStatus,
    Task,
    TaskStatus,
)


def _step_runs_for(store: Any, sop_run_id: str) -> list[StepRun]:
    return [
        StepRun.model_validate(item)
        for item in store.list_entities("step_runs")
        if item.get("sop_run_id") == sop_run_id
    ]


def _tasks_for_steps(store: Any, step_runs: list[StepRun]) -> list[Task]:
    ids = {step.id for step in step_runs}
    return [
        Task.model_validate(item)
        for item in store.list_entities("tasks")
        if item.get("step_run_id") in ids
    ]


def _attempts_for_tasks(store: Any, tasks: list[Task]) -> list[Attempt]:
    ids = {task.id for task in tasks}
    return [
        Attempt.model_validate(item)
        for item in store.list_entities("attempts")
        if item.get("task_id") in ids
    ]


def _artifacts_for_tasks(store: Any, tasks: list[Task]) -> list[Artifact]:
    ids = {task.id for task in tasks}
    return [
        Artifact.model_validate(item)
        for item in store.list_entities("artifacts")
        if item.get("task_id") in ids
    ]


def _reviews_for_tasks(store: Any, tasks: list[Task]) -> list[Review]:
    ids = {task.id for task in tasks}
    return [
        Review.model_validate(item)
        for item in store.list_entities("reviews")
        if item.get("task_id") in ids or item.get("reviewed_task_id") in ids
    ]


def collect_run_snapshot(*, sop_run_id: str, store: Any) -> dict[str, Any]:
    """Consolidated, queryable snapshot of one SOP run."""
    try:
        run = SopRun.model_validate(store.get_entity("sop_runs", sop_run_id))
    except KeyError:
        return {"sop_run_id": sop_run_id, "error": "run not found"}

    step_runs = _step_runs_for(store, sop_run_id)
    tasks = _tasks_for_steps(store, step_runs)
    attempts = _attempts_for_tasks(store, tasks)
    artifacts = _artifacts_for_tasks(store, tasks)
    reviews = _reviews_for_tasks(store, tasks)

    task_by_id = {task.id: task for task in tasks}
    step_by_id = {step.id: step for step in step_runs}
    attempts_by_task: dict[str, list[Attempt]] = {}
    for attempt in attempts:
        attempts_by_task.setdefault(attempt.task_id, []).append(attempt)

    current_step: dict[str, Any] | None = None
    for step in step_runs:
        if step.status in {
            StepStatus.RUNNING,
            StepStatus.WAITING_REVIEW,
            StepStatus.READY,
        }:
            current_step = {
                "step_id": step.step_id,
                "status": step.status.value,
                "rework_count": step.rework_count,
            }
            break

    current_task: dict[str, Any] | None = None
    current_attempt: dict[str, Any] | None = None
    for task in tasks:
        if task.status in {TaskStatus.RUNNING, TaskStatus.VALIDATING}:
            current_task = {
                "task_id": task.id,
                "role_id": task.role_id,
                "status": task.status.value,
            }
            task_attempts = attempts_by_task.get(task.id, [])
            if task_attempts:
                latest = max(task_attempts, key=lambda a: a.sequence)
                current_attempt = {
                    "attempt_id": latest.id,
                    "sequence": latest.sequence,
                    "status": latest.status.value,
                    "session_id": latest.session_id,
                    "runtime_id": latest.runtime_id,
                }
            break

    if current_task is None:
        # Parked run (waiting_review / policy_blocked): the reviewed task is
        # usually ACCEPTED, so surface the SUBJECT task/attempt (from the
        # pending/blocked review) - the exact identity a human needs.
        def _fill_current(task: Task) -> None:
            nonlocal current_task, current_attempt
            current_task = {
                "task_id": task.id,
                "role_id": task.role_id,
                "status": task.status.value,
            }
            task_attempts = attempts_by_task.get(task.id, [])
            if task_attempts:
                latest = max(task_attempts, key=lambda a: a.sequence)
                current_attempt = {
                    "attempt_id": latest.id,
                    "sequence": latest.sequence,
                    "status": latest.status.value,
                    "session_id": latest.session_id,
                    "runtime_id": latest.runtime_id,
                }

        for review in reviews:
            if review.status not in {
                ReviewStatus.PENDING,
                ReviewStatus.POLICY_BLOCKED,
            }:
                continue
            if review.reviewed_task_id and review.reviewed_task_id in task_by_id:
                _fill_current(task_by_id[review.reviewed_task_id])
                break
        if current_task is None:
            for step in step_runs:
                if step.status is not StepStatus.WAITING_REVIEW or not step.task_id:
                    continue
                task = task_by_id.get(step.task_id)
                if task is None:
                    continue
                _fill_current(task)
                break

    policy_gates = [
        {
            "review_id": review.id,
            "reviewed_attempt_id": review.reviewed_attempt_id,
            "reviewer_output": review.reviewer_output,
            "policy_decision": review.policy_decision,
            "status": review.status.value,
        }
        for review in reviews
        if review.status is ReviewStatus.POLICY_BLOCKED
        or (review.policy_decision == "BLOCKED")
    ]

    error_reasons: list[dict[str, Any]] = []
    error_reasons.extend(
        {
            "task_id": task.id,
            "role_id": task.role_id,
            "reason": f"task failed ({task.status.value})",
        }
        for task in tasks
        if task.status is TaskStatus.FAILED
    )
    error_reasons.extend(
        {"review_id": review.id, "reason": review.feedback[:300]}
        for review in reviews
        if review.status is ReviewStatus.POLICY_BLOCKED and review.feedback
    )

    return {
        "sop_run_id": sop_run_id,
        "status": run.status.value,
        "current_step": current_step,
        "current_task": current_task,
        "current_attempt": current_attempt,
        "steps": [
            {
                "step_id": step.step_id,
                "status": step.status.value,
                "rework_count": step.rework_count,
                "task_id": step.task_id,
            }
            for step in step_runs
        ],
        "tasks": [
            {
                "task_id": task.id,
                "step_id": step_by_id[task.step_run_id].step_id
                if task.step_run_id in step_by_id
                else None,
                "role_id": task.role_id,
                "status": task.status.value,
                "retry_of": task.retry_of,
            }
            for task in tasks
        ],
        "attempts": [
            {
                "attempt_id": attempt.id,
                "task_id": attempt.task_id,
                "sequence": attempt.sequence,
                "status": attempt.status.value,
                "session_id": attempt.session_id,
                "runtime_id": attempt.runtime_id,
                "previous_attempt_id": attempt.previous_attempt_id,
            }
            for attempt in attempts
        ],
        "artifacts": [
            {
                "artifact_id": artifact.id,
                "type": artifact.type.value,
                "task_id": artifact.task_id,
                "attempt_id": artifact.attempt_id,
                "accepted": artifact.accepted,
            }
            for artifact in artifacts
        ],
        "reviews": [
            {
                "review_id": review.id,
                "status": review.status.value,
                "reviewer_output": review.reviewer_output,
                "policy_decision": review.policy_decision,
                "reviewed_attempt_id": review.reviewed_attempt_id,
                "reviewed_artifact_ids": review.reviewed_artifact_ids,
                "evidence_ids": review.evidence_ids,
            }
            for review in reviews
        ],
        "policy_gates": policy_gates,
        "error_reasons": error_reasons,
        "counts": {
            "steps": len(step_runs),
            "tasks": len(tasks),
            "attempts": len(attempts),
            "artifacts": len(artifacts),
            "reviews": len(reviews),
        },
    }



def collect_rework_lineage(*, sop_run_id: str, store: Any) -> list[dict[str, Any]]:
    """Per-step rework chains: tasks via retry_of, attempts via previous_attempt_id."""
    step_runs = _step_runs_for(store, sop_run_id)
    tasks = _tasks_for_steps(store, step_runs)
    attempts = _attempts_for_tasks(store, tasks)

    step_id_of = {step.id: step.step_id for step in step_runs}
    tasks_by_step: dict[str, list[Task]] = {}
    for task in tasks:
        step_id = step_id_of.get(task.step_run_id)
        if step_id is not None:
            tasks_by_step.setdefault(step_id, []).append(task)

    chains: list[dict[str, Any]] = []
    for step_id in sorted(tasks_by_step):
        chain: list[dict[str, Any]] = []
        visited: set[str] = set()
        frontier = [
            task for task in tasks_by_step[step_id] if task.retry_of is None
        ]
        while frontier:
            task = frontier.pop(0)
            if task.id in visited:
                continue
            visited.add(task.id)
            task_attempts = sorted(
                (attempt for attempt in attempts if attempt.task_id == task.id),
                key=lambda item: item.sequence,
            )
            chain.append(
                {
                    "task_id": task.id,
                    "status": task.status.value,
                    "attempts": [
                        {
                            "attempt_id": attempt.id,
                            "sequence": attempt.sequence,
                            "status": attempt.status.value,
                            "previous_attempt_id": attempt.previous_attempt_id,
                            "session_id": attempt.session_id,
                            "runtime_id": attempt.runtime_id,
                        }
                        for attempt in task_attempts
                    ],
                }
            )
            frontier.extend(
                child
                for child in tasks_by_step[step_id]
                if child.retry_of == task.id and child.id not in visited
            )
        chains.append({"step_id": step_id, "chain": chain})
    return chains


def collect_provider_health(
    *, store: Any, runners: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Per-runtime health: registered, adapter type, last session seen."""
    health: dict[str, dict[str, Any]] = {}
    if runners:
        for runtime_id, adapter in runners.items():
            health.setdefault(
                runtime_id,
                {
                    "runtime_id": runtime_id,
                    "configured": True,
                    "adapter": type(adapter).__name__,
                    "last_session_id": None,
                },
            )
    for item in store.list_entities("attempts"):
        runtime_id = item.get("runtime_id")
        if not runtime_id:
            continue
        entry = health.setdefault(
            runtime_id,
            {
                "runtime_id": runtime_id,
                "configured": True,
                "adapter": None,
                "last_session_id": None,
            },
        )
        if item.get("session_id"):
            entry["last_session_id"] = item["session_id"]
    return sorted(health.values(), key=lambda item: item["runtime_id"])


def collect_global_overview(*, store: Any) -> dict[str, Any]:
    """Cross-run counts (metrics-lite; observability is the Phase 6 focus)."""
    runs = [SopRun.model_validate(item) for item in store.list_entities("sop_runs")]
    attempts = store.list_entities("attempts")
    reviews = store.list_entities("reviews")
    return {
        "total_sop_runs": len(runs),
        "active_runs": sum(
            1 for run in runs if run.status in {SopRunStatus.RUNNING, SopRunStatus.WAITING_REVIEW}
        ),
        "paused_runs": sum(1 for run in runs if run.status is SopRunStatus.PAUSED),
        "completed_runs": sum(
            1 for run in runs if run.status is SopRunStatus.COMPLETED
        ),
        "failed_runs": sum(1 for run in runs if run.status is SopRunStatus.FAILED),
        "cancelled_runs": sum(
            1 for run in runs if run.status is SopRunStatus.CANCELLED
        ),
        "attempt_count": len(attempts),
        "rework_attempt_count": sum(
            1 for item in attempts if item.get("previous_attempt_id") is not None
        ),
        "review_count": len(reviews),
        "policy_blocked_reviews": sum(
            1
            for item in reviews
            if item.get("policy_decision") == "BLOCKED"
        ),
    }

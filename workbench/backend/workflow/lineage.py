"""Artifact lineage validation for the evidence gate (Phase 3).

Separates the REVIEWED EVIDENCE (DIFF + TEST_REPORT produced by the executor's
Attempt) from the REVIEWER'S OWN ARTIFACT (REVIEW_REPORT produced by the
reviewer's task).  A PASS may only advance when the evidence is complete,
accepted, and bound to the exact reviewed Attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.models import (
    Artifact,
    ArtifactType,
    Attempt,
    Review,
    StepRun,
    Task,
)
from .role_contract import AgentRole


class LineageError(Exception):
    """Raised when lineage or evidence validation fails."""


@dataclass
class ReviewEvidence:
    """Validated evidence bundle for one PASS review."""

    diff: Artifact | None
    test_report: Artifact | None
    reviewer_report: Artifact | None
    reviewed_attempt: Attempt | None
    missing: list[str] = field(default_factory=list)


def validate_artifact_lineage(
    *, artifact: Artifact, store: Any
) -> dict[str, str | None]:
    """Validate artifact -> task -> attempt -> step_run -> sop_run chain.

    Returns a dict with task_id / attempt_id / step_run_id / sop_run_id.
    Raises LineageError when any link is broken.
    """
    result: dict[str, str | None] = {}
    if not artifact.task_id:
        raise LineageError(f"Artifact {artifact.id} missing task_id")
    try:
        task = Task.model_validate(store.get_entity("tasks", artifact.task_id))
    except KeyError:
        raise LineageError(
            f"Artifact {artifact.id} references non-existent task {artifact.task_id}"
        ) from None
    result["task_id"] = task.id

    if artifact.attempt_id:
        try:
            attempt = Attempt.model_validate(
                store.get_entity("attempts", artifact.attempt_id)
            )
        except KeyError:
            raise LineageError(
                f"Artifact {artifact.id} references non-existent attempt "
                f"{artifact.attempt_id}"
            ) from None
        if attempt.task_id != task.id:
            raise LineageError(
                f"Artifact {artifact.id} attempt {attempt.id} belongs to task "
                f"{attempt.task_id}, not {task.id}"
            )
        result["attempt_id"] = attempt.id
    else:
        result["attempt_id"] = None

    if not task.step_run_id:
        raise LineageError(f"Task {task.id} missing step_run_id")
    try:
        step_run = StepRun.model_validate(
            store.get_entity("step_runs", task.step_run_id)
        )
    except KeyError:
        raise LineageError(
            f"Task {task.id} references non-existent step_run {task.step_run_id}"
        ) from None
    result["step_run_id"] = step_run.id

    if not step_run.sop_run_id:
        raise LineageError(f"StepRun {step_run.id} missing sop_run_id")
    try:
        store.get_entity("sop_runs", step_run.sop_run_id)
    except KeyError:
        raise LineageError(
            f"StepRun {step_run.id} references non-existent sop_run "
            f"{step_run.sop_run_id}"
        ) from None
    result["sop_run_id"] = step_run.sop_run_id
    return result


def _attempt_artifacts(*, attempt_id: str, store: Any) -> list[Artifact]:
    return [
        Artifact.model_validate(item)
        for item in store.list_entities("artifacts")
        if item.get("attempt_id") == attempt_id
    ]


LEGAL_REVIEWER_ROLES = {AgentRole.CODEX.value, "codex_reviewer"}


def validate_review_evidence(*, review: Review, store: Any) -> ReviewEvidence:
    """Validate one review's evidence (used by the PASS evidence gate).

    Checks (per the evidence-gate policy):
    1. review <-> REVIEW_REPORT correspondence (review.artifact_id)
    2. REVIEW_REPORT belongs to the REVIEWER's task (review.task_id), never
       the reviewed (executor) task
    3. reviewer role is a legal reviewer
    4. reviewed Attempt exists and belongs to the reviewed task
    5. DIFF + TEST_REPORT belong to the reviewed Attempt and are accepted
    6. claimed evidence_ids (when present) match the attempt evidence exactly

    Raises LineageError on any failure.
    """
    missing: list[str] = []

    # 1 + 2: review report correspondence and provenance
    try:
        reviewer_report = Artifact.model_validate(
            store.get_entity("artifacts", review.artifact_id)
        )
    except KeyError:
        raise LineageError(
            f"Review {review.id} references non-existent review artifact "
            f"{review.artifact_id}"
        ) from None
    if reviewer_report.type is not ArtifactType.REVIEW_REPORT:
        raise LineageError(
            f"Review {review.id} artifact {reviewer_report.id} is "
            f"{reviewer_report.type.value}, not REVIEW_REPORT"
        )
    if reviewer_report.task_id != review.task_id:
        raise LineageError(
            f"Review {review.id} report {reviewer_report.id} belongs to task "
            f"{reviewer_report.task_id}, not the reviewer task {review.task_id}"
        )

    # 3: reviewer role
    if review.reviewer_role_id not in LEGAL_REVIEWER_ROLES:
        raise LineageError(
            f"Review {review.id} has illegal reviewer role "
            f"{review.reviewer_role_id!r}"
        )

    # 4: reviewed attempt
    if not review.reviewed_attempt_id:
        raise LineageError(
            f"Review {review.id} missing reviewed_attempt_id "
            "(legacy review cannot be evidence-gated)"
        )
    try:
        reviewed_attempt = Attempt.model_validate(
            store.get_entity("attempts", review.reviewed_attempt_id)
        )
    except KeyError:
        raise LineageError(
            f"Review {review.id} references non-existent attempt "
            f"{review.reviewed_attempt_id}"
        ) from None
    if review.reviewed_task_id and reviewed_attempt.task_id != review.reviewed_task_id:
        raise LineageError(
            f"Review {review.id} attempt {reviewed_attempt.id} belongs to task "
            f"{reviewed_attempt.task_id}, not reviewed task {review.reviewed_task_id}"
        )

    # 5: DIFF + TEST_REPORT from the same attempt, accepted
    attempt_artifacts = _attempt_artifacts(
        attempt_id=reviewed_attempt.id, store=store
    )
    diff = next((a for a in attempt_artifacts if a.type is ArtifactType.DIFF), None)
    test_report = next(
        (a for a in attempt_artifacts if a.type is ArtifactType.TEST_REPORT), None
    )
    for label, artifact in (("DIFF", diff), ("TEST_REPORT", test_report)):
        if artifact is None:
            missing.append(label)
        elif not artifact.accepted:
            raise LineageError(
                f"Review {review.id} evidence {label} {artifact.id} is not accepted"
            )

    # 6: claimed evidence ids, when present, must match exactly
    claimed = set(review.evidence_ids)
    actual = {a.id for a in (diff, test_report) if a is not None}
    if not claimed:
        raise LineageError(
            f"Review {review.id} has no claimed evidence_ids; refusing to "
            "approve PASS without recorded evidence"
        )
    if claimed != actual:
        raise LineageError(
            f"Review {review.id} claimed evidence {sorted(claimed)} does not "
            f"match attempt evidence {sorted(actual)}"
        )

    if missing:
        raise LineageError(
            f"Review {review.id} missing required evidence: {', '.join(missing)}"
        )

    return ReviewEvidence(
        diff=diff,
        test_report=test_report,
        reviewer_report=reviewer_report,
        reviewed_attempt=reviewed_attempt,
    )

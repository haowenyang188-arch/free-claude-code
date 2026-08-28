"""Always-accept validator for MVP SOP execution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from ..domain.models import Artifact, Task, ValidationResult, ValidationStatus
from ..workflow.engine import AcceptanceValidator


class AlwaysAcceptValidator(AcceptanceValidator):
    """Accept all artifacts for MVP testing."""

    def __init__(self, validator_id: str = "always-accept") -> None:
        self.validator_id = validator_id
        self.validations: list[ValidationResult] = []

    async def validate(self, *, task: Task, artifact: Artifact) -> ValidationResult:
        """Always return ACCEPTED status."""
        result = ValidationResult(
            id=str(uuid.uuid4()),
            task_id=task.id,
            artifact_id=artifact.id,
            status=ValidationStatus.ACCEPTED,
            validator_id=self.validator_id,
            messages=["Auto-accepted for MVP"],
            checked_at=datetime.now(UTC),
        )
        self.validations.append(result)
        return result

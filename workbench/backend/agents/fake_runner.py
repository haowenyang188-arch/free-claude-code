"""Fake SubagentRunner for deterministic SOP testing without real CLI."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from ..domain.models import Artifact, ArtifactType, ContextPackage, Task
from ..workflow.engine import SubagentRunner


class FakeSubagentRunner(SubagentRunner):
    """Deterministic runner that returns pre-configured artifacts."""

    def __init__(self, *, artifact_content: str | None = None) -> None:
        self.artifact_content = artifact_content or "Fake artifact output"
        self.executions: list[dict] = []

    async def execute(
        self,
        *,
        task: Task,
        assignment,
        context: ContextPackage,
    ) -> Artifact:
        """Return a deterministic artifact for testing."""
        execution_record = {
            "task_id": task.id,
            "role_id": task.role_id,
            "context_artifact_ids": list(context.artifact_ids),
            "instructions": context.instructions,
        }
        self.executions.append(execution_record)

        artifact = Artifact(
            id=str(uuid.uuid4()),
            task_id=task.id,
            type=ArtifactType.TEXT,
            content=f"{self.artifact_content}\n\nTask: {task.title}\nContext artifacts: {len(context.artifact_ids)}",
            summary=f"Completed {task.title}",
            created_at=datetime.now(UTC),
            accepted=False,
        )
        return artifact

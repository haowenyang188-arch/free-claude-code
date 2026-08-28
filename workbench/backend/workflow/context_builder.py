"""Build ContextPackage from prior step artifacts and handoffs."""

from __future__ import annotations

import uuid

from ..artifacts.store import FileArtifactStore
from ..domain.models import (
    AcceptanceCriteria,
    Artifact,
    ContextPackage,
    Goal,
    Handoff,
    SopDefinition,
    StepDefinition,
    Task,
)


class ContextPackageBuilder:
    """Construct context packages for task execution from SOP state."""

    def __init__(
        self,
        *,
        store,
        artifact_store: FileArtifactStore | None = None,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store

    def build_for_task(
        self,
        *,
        task: Task,
        step: StepDefinition,
        goal: Goal,
        sop: SopDefinition,
    ) -> ContextPackage:
        """Build context package by gathering upstream artifacts."""
        artifact_ids = self._collect_upstream_artifacts(task, step)

        # Step already has AcceptanceCriteria objects, reuse them
        acceptance_criteria = list(step.acceptance_criteria)

        context = ContextPackage(
            id=str(uuid.uuid4()),
            task_id=task.id,
            goal_summary=goal.description,
            instructions=step.instructions,
            constraints=list(goal.constraints),
            artifact_ids=artifact_ids,
            acceptance_criteria=acceptance_criteria,
        )
        return context

    def _collect_upstream_artifacts(
        self, task: Task, step: StepDefinition
    ) -> list[str]:
        """Collect accepted artifacts from dependencies and handoffs."""
        artifact_ids_set: set[str] = set()

        # Find handoffs targeting this step
        handoffs = [
            Handoff.model_validate(item)
            for item in self.store.list_entities("handoffs")
            if item.get("to_step_id") == step.id
            and item.get("status") == "accepted"
        ]
        for handoff in handoffs:
            artifact_ids_set.update(handoff.artifact_ids)

        # Find artifacts from dependency steps (not already in handoffs)
        for dep_step_id in step.depends_on:
            dep_artifacts = [
                item
                for item in self.store.list_entities("artifacts")
                if item.get("producer_step_run_id", "").endswith(f":{dep_step_id}")
                and item.get("accepted") is True
            ]
            artifact_ids_set.update(artifact["id"] for artifact in dep_artifacts)

        return list(artifact_ids_set)

    def load_artifact_content(self, artifact: Artifact) -> str:
        """Load artifact content from store if needed."""
        if artifact.content:
            return artifact.content
        if self.artifact_store and artifact.uri and artifact.sha256:
            payload = self.artifact_store.get(artifact)
            return payload.decode("utf-8")
        return artifact.summary or ""

"""Build ContextPackage from prior step artifacts and handoffs."""

from __future__ import annotations

import uuid

from ..artifacts.store import FileArtifactStore
from ..domain.models import (
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
        """Collect accepted artifacts from dependencies and handoffs.

        M7: Filter by task.step_run_id's sop_run_id to prevent cross-SOP pollution.
        """
        artifact_ids_set: set[str] = set()

        # M7: Extract sop_run_id from the task's step_run
        from ..domain.models import StepRun
        step_run = StepRun.model_validate(
            self.store.get_entity("step_runs", task.step_run_id)
        )
        sop_run_id = step_run.sop_run_id

        # Find handoffs targeting this step within the same SOP run
        handoffs = [
            Handoff.model_validate(item)
            for item in self.store.list_entities("handoffs")
            if item.get("to_step_id") == step.id
            and item.get("status") == "accepted"
        ]
        for handoff in handoffs:
            # M7: Verify handoff belongs to the same SOP run
            from_task = self.store.get_entity("tasks", handoff.from_task_id)
            from_step_run = StepRun.model_validate(
                self.store.get_entity("step_runs", from_task["step_run_id"])
            )
            if from_step_run.sop_run_id == sop_run_id:
                artifact_ids_set.update(handoff.artifact_ids)

        # Find artifacts from dependency steps (not already in handoffs)
        for dep_step_id in step.depends_on:
            dep_artifacts = [
                item
                for item in self.store.list_entities("artifacts")
                if item.get("producer_step_run_id", "").endswith(f":{dep_step_id}")
                and item.get("accepted") is True
            ]
            # M7: Filter by sop_run_id
            for artifact in dep_artifacts:
                producer_step_run_id = artifact.get("producer_step_run_id", "")
                if producer_step_run_id:
                    try:
                        producer_step_run = StepRun.model_validate(
                            self.store.get_entity("step_runs", producer_step_run_id)
                        )
                        if producer_step_run.sop_run_id == sop_run_id:
                            artifact_ids_set.add(artifact["id"])
                    except Exception:
                        # Skip artifacts with invalid step_run references
                        continue

        return list(artifact_ids_set)

    def load_artifact_content(self, artifact: Artifact) -> str:
        """Load artifact content from store if needed."""
        if artifact.content:
            return artifact.content
        if self.artifact_store and artifact.uri and artifact.sha256:
            payload = self.artifact_store.get(artifact)
            return payload.decode("utf-8")
        return artifact.summary or ""

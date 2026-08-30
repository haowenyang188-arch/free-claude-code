"""CodexSubagentRunner for real SOP execution with Codex."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.models import Artifact, ArtifactType, ContextPackage, Task
from ..workflow.engine import SubagentRunner
from ..workflow.role_contract import AgentRole
from .codex_adapter import CodexAdapter
from .codex_review import parse_review


class CodexSubagentRunner(SubagentRunner):
    """Execute tasks using real Codex CLI."""

    def __init__(
        self,
        *,
        codex_adapter: CodexAdapter,
        workspace_path: str | Path,
        store: Any = None,
        artifact_store: Any = None,
    ) -> None:
        self.codex_adapter = codex_adapter
        self.workspace_path = Path(workspace_path)
        self.store = store
        self.artifact_store = artifact_store
        self.executions: list[dict] = []

    async def execute(
        self,
        *,
        task: Task,
        assignment,
        context: ContextPackage,
    ) -> Artifact:
        """Execute task with Codex and extract artifact from output."""
        execution_record = {
            "task_id": task.id,
            "role_id": task.role_id,
            "started_at": datetime.now(UTC),
        }
        self.executions.append(execution_record)

        # Build prompt from context
        prompt = self._build_prompt(task, context)

        # F-5: the ONLY reviewer identity source is SubagentAssignment.role_id.
        # approval scope and sandbox mode are DERIVED from it — there is no
        # independent reviewer_mode switch a caller can flip into contradiction.
        is_reviewer = assignment.role_id == AgentRole.CODEX.value
        approval_scope = "deny_only" if is_reviewer else "normal"
        sandbox_mode = "read-only" if is_reviewer else None

        # Create a completion event to capture output
        output_buffer: list[str] = []
        original_callback = self.codex_adapter.event_callback

        async def capture_callback(event):
            if event.type.value == "agent_message":
                message = event.data.get("message", "")
                if message:
                    output_buffer.append(message)
            if original_callback:
                await original_callback(event)

        self.codex_adapter.set_event_callback(capture_callback)

        # Execute with Codex
        run_id = f"sop-task-{task.id}"
        success = await self.codex_adapter.start_task(
            run_id=run_id,
            task_description=prompt,
            workspace_path=str(self.workspace_path),
            sandbox_mode=sandbox_mode,
            approval_scope=approval_scope,
        )

        if not success:
            raise RuntimeError(f"Codex failed to start task {task.id}")

        # Wait for completion
        if self.codex_adapter.monitor_task:
            await self.codex_adapter.monitor_task

        # Restore original callback
        self.codex_adapter.set_event_callback(original_callback)

        # Extract output
        output_text = "\n".join(output_buffer)
        execution_record["completed_at"] = datetime.now(UTC)
        execution_record["output_length"] = len(output_text)

        # Create artifact from output
        if is_reviewer:
            # F-4: fail-closed — parse_review raises on empty/unknown/malformed
            # output, so a REVIEW_REPORT (and any PASS/REWORK/PLAN_INVALID
            # handoff) can never be built from garbage.
            thread_id = self.codex_adapter.session_id
            turn_id = None
            app_server = getattr(self.codex_adapter, "app_server_session", None)
            if app_server is not None:
                turn_id = app_server.current_turn_id
            review = parse_review(output_text, thread_id=thread_id, turn_id=turn_id)
            artifact = Artifact(
                id=str(uuid.uuid4()),
                task_id=task.id,
                type=ArtifactType.REVIEW_REPORT,
                content=json.dumps(review.to_mapping(), ensure_ascii=False),
                summary=f"REVIEW: {review.result}",
                created_at=datetime.now(UTC),
                accepted=False,
            )
        else:
            artifact = Artifact(
                id=str(uuid.uuid4()),
                task_id=task.id,
                type=ArtifactType.TEXT,
                content=output_text[:10000],  # Limit content size
                summary=self._extract_summary(output_text),
                created_at=datetime.now(UTC),
                accepted=False,
            )
        return artifact

    def _build_prompt(self, task: Task, context: ContextPackage) -> str:
        """Build Codex prompt from task and context.

        M7: Load artifact content for CODE and TEST_RESULT types.
        """
        parts = [
            f"# Task: {task.title}",
            "",
            "## Goal",
            context.goal_summary,
            "",
            "## Instructions",
            context.instructions or task.description,
        ]

        if context.constraints:
            parts.extend([
                "",
                "## Constraints",
                *[f"- {constraint}" for constraint in context.constraints],
            ])

        if context.acceptance_criteria:
            parts.extend([
                "",
                "## Acceptance Criteria",
                *[f"- {criterion.description}" for criterion in context.acceptance_criteria],
            ])

        if context.artifact_ids:
            parts.extend([
                "",
                "## Context from Previous Steps",
            ])

            # M7: Load and include content for IMPLEMENTATION and TEST_REPORT
            # artifacts through the injected workflow store.  The old
            # ContextBuilder/MemoryStore imports referenced modules that do not
            # exist; Artifact.type is an enum, not an "artifact_type" string.
            loaded_count = 0
            if self.store is not None:
                for artifact_id in context.artifact_ids:
                    try:
                        artifact_dict = self.store.get_entity("artifacts", artifact_id)
                        artifact = Artifact.model_validate(artifact_dict)
                    except (KeyError, ValueError, TypeError):
                        continue
                    if artifact.type not in {
                        ArtifactType.IMPLEMENTATION,
                        ArtifactType.TEST_REPORT,
                    }:
                        continue
                    content = artifact.content or ""
                    if (
                        not content
                        and artifact.uri
                        and artifact.sha256
                        and self.artifact_store is not None
                    ):
                        try:
                            content = self.artifact_store.get(artifact).decode(
                                "utf-8"
                            )
                        except Exception:
                            content = ""
                    if content:
                        parts.extend([
                            "",
                            "### Artifact: " + artifact_id + " (type: " + artifact.type.value + ")",
                            "```",
                            content[:5000],
                            "```",
                        ])
                        loaded_count += 1

            if loaded_count == 0:
                parts.append(
                    "You have access to " + str(len(context.artifact_ids)) + " artifact(s) from previous steps."
                )

        return "\n".join(parts)

    def _extract_summary(self, output: str, max_length: int = 200) -> str:
        """Extract a short summary from output."""
        lines = output.strip().split("\n")
        first_meaningful = next(
            (line for line in lines if line.strip() and not line.startswith("#")),
            ""
        )
        if len(first_meaningful) > max_length:
            return first_meaningful[:max_length] + "..."
        return first_meaningful or "Codex output"

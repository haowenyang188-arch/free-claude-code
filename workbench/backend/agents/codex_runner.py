"""CodexSubagentRunner for real SOP execution with Codex."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..domain.models import Artifact, ArtifactType, ContextPackage, Task
from ..workflow.engine import SubagentRunner
from .codex_adapter import CodexAdapter


class CodexSubagentRunner(SubagentRunner):
    """Execute tasks using real Codex CLI."""

    def __init__(
        self,
        *,
        codex_adapter: CodexAdapter,
        workspace_path: str | Path,
    ) -> None:
        self.codex_adapter = codex_adapter
        self.workspace_path = Path(workspace_path)
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
        """Build Codex prompt from task and context."""
        parts = [
            f"# Task: {task.title}",
            "",
            f"## Goal",
            context.goal_summary,
            "",
            f"## Instructions",
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
                f"## Context from Previous Steps",
                f"You have access to {len(context.artifact_ids)} artifact(s) from previous steps.",
                "Review them before proceeding.",
            ])

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

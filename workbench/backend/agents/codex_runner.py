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
            try:
                review = parse_review(output_text, thread_id=thread_id, turn_id=turn_id)
            except Exception as exc:
                # B-fix diagnosis: keep the raw reviewer output visible so a
                # contract-parse failure is actionable instead of opaque.
                raise type(exc)(
                    f"{exc} | raw reviewer output head: {output_text[:500]!r}"
                ) from exc
            artifact = Artifact(
                id=str(uuid.uuid4()),
                task_id=task.id,
                type=ArtifactType.REVIEW_REPORT,
                content=json.dumps(review.to_mapping(), ensure_ascii=False),
                summary=f"REVIEW: {review.result}",
                created_at=datetime.now(UTC),
                accepted=False,
            )
            return artifact
        else:
            # Executor-grade codex role (codex_executor): the evidence gate
            # requires DIFF + TEST_REPORT from the attempt, so the prompt
            # demands the markers and parsing FAILS CLOSED on missing or
            # empty sections — the same discipline the DSH executor
            # contract enforced (never fabricate evidence).
            diff_marker = "---DIFF---"
            test_marker = "---TEST_REPORT---"
            if diff_marker not in output_text or test_marker not in output_text:
                raise RuntimeError(
                    "codex executor output missing "
                    f"{diff_marker}/{test_marker} markers; refusing to "
                    f"fabricate evidence. raw head: {output_text[:400]!r}"
                )
            diff_part = output_text.split(diff_marker, 1)[1]
            diff_content = diff_part.split(test_marker, 1)[0].strip()
            test_content = (
                diff_part.split(test_marker, 1)[1].strip()
                if test_marker in diff_part
                else ""
            )
            if not diff_content or not test_content:
                raise RuntimeError(
                    "codex executor produced empty DIFF/TEST_REPORT sections; "
                    "refusing to fabricate evidence"
                )
            diff_artifact = Artifact(
                id=str(uuid.uuid4()),
                task_id=task.id,
                type=ArtifactType.DIFF,
                content=diff_content,
                summary="DIFF",
                created_at=datetime.now(UTC),
                accepted=False,
            )
            test_artifact = Artifact(
                id=str(uuid.uuid4()),
                task_id=task.id,
                type=ArtifactType.TEST_REPORT,
                content=test_content,
                summary="TEST_REPORT",
                created_at=datetime.now(UTC),
                accepted=False,
            )
            # 执行证据链需要 DIFF + TEST_REPORT 两件;元组由
            # _CodexExecutionAdapter 展开进同一 attempt。
            return diff_artifact, test_artifact

    def _build_prompt(self, task: Task, context: ContextPackage) -> str:
        """Build Codex prompt from task and context.

        M7: Load artifact content for CODE, TEST_RESULT and PLAN types
        (PLAN so a plan-review gate can inspect the actual plan).
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

        if (task.role_id or "").lower() not in {"codex", "reviewer"}:
            # Executor-grade output contract: the evidence gate needs
            # machine-splittable DIFF + TEST_REPORT sections.
            parts.extend([
                "",
                "## Required output format",
                "Emit exactly two sections:",
                "---DIFF---",
                "<the diff / deliverable>",
                "---TEST_REPORT---",
                "<the test / verification report>",
            ])

        if context.artifact_ids:
            parts.extend([
                "",
                "## Context from Previous Steps",
            ])

            # M7: Load and include content for IMPLEMENTATION, TEST_REPORT and
            # PLAN artifacts (PLAN enables the plan-review gate).
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
                        ArtifactType.PLAN,
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

        # B-fix (C4): the real reviewer must emit the structured review
        # contract, otherwise codex produces free-form text and parse_review
        # fails closed.  Only the reviewer role gets this instruction.
        if (task.role_id or "").lower() in {"codex", "reviewer"}:
            parts.extend(
                [
                    "",
                    "## Review Output Contract",
                    "Reply with ONLY a single JSON object on its own, matching exactly:",
                    '{"result": "PASS" | "REWORK" | "PLAN_INVALID", "blocking": ["<issue or reason>", ...], "non_blocking": ["<optional>", ...], "evidence": ["<what you inspected>", ...]}',
                    "Rules: result must be exactly one of PASS|REWORK|PLAN_INVALID. "
                    "REWORK/PLAN_INVALID require at least one blocking item. "
                    "Do not wrap in markdown fences. Do not add prose before or after the JSON.",
                ]
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

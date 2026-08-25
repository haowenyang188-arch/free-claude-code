"""Claude Code runtime adapter for executing tasks through Claude Code."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from workbench.backend.domain.models import Artifact, ArtifactType, RuntimeKind
from workbench.backend.workflow.runners import RuntimeAdapter, RunnerError

if TYPE_CHECKING:
    from workbench.backend.domain.models import (
        ContextPackage,
        SubagentAssignment,
        Task,
    )


class ClaudeCodeAdapter(RuntimeAdapter):
    """Executes tasks by invoking Claude Code CLI."""

    def __init__(self, artifact_store: Any = None) -> None:
        """Initialize adapter with optional artifact store for loading upstream artifacts."""
        self._artifact_store = artifact_store

    def supports(self, runtime_kind: RuntimeKind) -> bool:
        """Return True for CLAUDE_CODE runtime."""
        return runtime_kind == RuntimeKind.CLAUDE_CODE

    async def execute_step(
        self,
        *,
        task: Task,
        assignment: SubagentAssignment,
        context: ContextPackage,
    ) -> Artifact:
        """Execute task through Claude Code and return artifact with response.

        Args:
            task: Task definition with role and title
            assignment: Agent assignment (contains runtime_id)
            context: Complete context package with goal, instructions, constraints, artifacts

        Returns:
            Artifact containing Claude Code's response

        Raises:
            RunnerError: If Claude Code invocation fails
        """
        # Build prompt from context
        prompt = self._build_prompt(task=task, context=context)

        # Determine working directory
        cwd = context.workspace_scope if context.workspace_scope else None

        # Invoke Claude Code
        try:
            response = await self._invoke_claude_code(prompt=prompt, cwd=cwd)
        except Exception as e:
            raise RunnerError(f"Claude Code invocation failed: {e}") from e

        # Generate summary (first 100 chars)
        summary = response[:100] + "..." if len(response) > 100 else response

        # Create artifact
        artifact = Artifact(
            id=f"artifact-{task.id}",
            task_id=task.id,
            type=ArtifactType.TEXT,
            content=response,
            summary=summary,
        )

        return artifact

    def _build_prompt(self, *, task: Task, context: ContextPackage) -> str:
        """Construct prompt from task and context package."""
        sections = []

        # Goal
        sections.append(f"# Goal\n{context.goal_summary}")

        # Task instructions
        sections.append(f"# Instructions\n{context.instructions}")

        # Role and title
        if task.title:
            sections.append(f"# Task\nRole: {task.role_id}\nTitle: {task.title}")

        # Constraints
        if context.constraints:
            constraints_text = "\n".join(f"- {c}" for c in context.constraints)
            sections.append(f"# Constraints\n{constraints_text}")

        # Upstream artifacts
        if context.artifact_ids and self._artifact_store:
            artifacts_text = self._load_upstream_artifacts(context.artifact_ids)
            if artifacts_text:
                sections.append(f"# Upstream Artifacts\n{artifacts_text}")

        # Decisions
        if context.decisions:
            decisions_text = "\n".join(f"- {d}" for d in context.decisions)
            sections.append(f"# Decisions\n{decisions_text}")

        # Allowed tools
        if context.allowed_tools:
            tools_text = ", ".join(context.allowed_tools)
            sections.append(f"# Allowed Tools\nYou may use: {tools_text}")

        return "\n\n".join(sections)

    def _load_upstream_artifacts(self, artifact_ids: list[str]) -> str:
        """Load upstream artifacts from store and format for prompt."""
        if not self._artifact_store:
            return ""

        artifacts_text = []
        for artifact_id in artifact_ids:
            try:
                # Mock artifact object for store.get()
                mock_artifact = type("Artifact", (), {"id": artifact_id})()
                content_bytes = self._artifact_store.get(mock_artifact)
                content = content_bytes.decode("utf-8")
                artifacts_text.append(f"## {artifact_id}\n{content}")
            except Exception:
                # Skip artifacts that can't be loaded
                continue

        return "\n\n".join(artifacts_text)

    async def _invoke_claude_code(self, *, prompt: str, cwd: str | None = None) -> str:
        """Invoke Claude Code CLI with the given prompt.

        Args:
            prompt: The prompt to send to Claude Code
            cwd: Optional working directory for Claude Code execution

        Returns:
            Claude Code's response text

        Raises:
            RunnerError: If Claude Code execution fails
        """
        import tempfile

        # Write prompt to temporary file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            # Build command: claude <prompt_file>
            cmd = ['claude', prompt_file]

            # Add working directory if specified
            if cwd:
                cmd.extend(['--cwd', cwd])

            # Execute Claude Code CLI
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            # Wait for completion with timeout (5 minutes)
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=300.0
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise RunnerError("Claude Code execution timeout after 5 minutes")

            # Check exit code
            if process.returncode != 0:
                error_msg = stderr.decode('utf-8') if stderr else "Unknown error"
                raise RunnerError(
                    f"Claude Code exited with code {process.returncode}: {error_msg}"
                )

            # Parse response
            response = stdout.decode('utf-8').strip()

            if not response:
                raise RunnerError("Claude Code returned empty response")

            return response

        finally:
            # Clean up temporary prompt file
            import os
            try:
                os.unlink(prompt_file)
            except Exception:
                pass  # Best effort cleanup

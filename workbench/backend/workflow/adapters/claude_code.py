"""Claude Code runtime adapter for executing tasks through Claude Code."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from cli.process_registry import register_process, unregister_process
from cli.runtime_environment import build_cli_environment
from cli.runtime_registry import RuntimeBackend, RuntimeRegistry
from workbench.backend.domain.models import Artifact, ArtifactType, RuntimeKind
from workbench.backend.workflow.runners import RunnerError, RuntimeAdapter

if TYPE_CHECKING:
    from workbench.backend.domain.models import (
        ContextPackage,
        SubagentAssignment,
        Task,
    )


class ClaudeCodeAdapter(RuntimeAdapter):
    """Executes tasks by invoking Claude Code CLI."""

    def __init__(
        self,
        artifact_store: Any = None,
        *,
        claude_bin: str = "claude",
        isolation_mode: str = "safe",
        runtime_registry: RuntimeRegistry | None = None,
        preflight_runtime: bool = False,
    ) -> None:
        """Initialize adapter with optional artifact store for loading upstream artifacts."""
        if isolation_mode not in {"safe", "inherit"}:
            raise ValueError("isolation_mode must be 'safe' or 'inherit'")
        self._artifact_store = artifact_store
        self._claude_bin = claude_bin
        self._isolation_mode = isolation_mode
        self._runtime_registry = runtime_registry or RuntimeRegistry(
            executables={RuntimeBackend.CLAUDE: claude_bin}
        )
        self._preflight_runtime = preflight_runtime

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
        if self._preflight_runtime:
            probe = await self._runtime_registry.probe(RuntimeBackend.CLAUDE)
            if not probe.available:
                raise RunnerError(
                    f"Claude CLI preflight failed: {probe.reason or 'runtime_unavailable'}"
                )
            if self._isolation_mode == "safe":
                profile = await self._runtime_registry.probe_safe_profile(
                    RuntimeBackend.CLAUDE
                )
                if not profile.available:
                    raise RunnerError(
                        "Claude CLI safe profile preflight failed: "
                        f"{profile.reason or 'safe_profile_unavailable'}"
                    )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            cmd = [self._claude_bin, "--print"]
            if self._isolation_mode == "safe":
                cmd.extend(
                    [
                        "--safe-mode",
                        "--strict-mcp-config",
                        "--permission-mode",
                        "plan",
                    ]
                )
            cmd.append(prompt_file)
            generation = uuid.uuid4().hex

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                # Provider diagnostics may contain credentials or account
                # details and are not part of the workflow artifact contract.
                # Discard them at the process boundary so they cannot leak or
                # accumulate in an unbounded pipe.
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=build_cli_environment(RuntimeBackend.CLAUDE),
            )
            pid = getattr(process, "pid", None)
            if isinstance(pid, int) and pid > 0:
                register_process(pid, generation=generation)

            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(), timeout=300.0
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise RunnerError(
                    "Claude Code execution timeout after 5 minutes"
                ) from exc
            finally:
                if isinstance(pid, int) and pid > 0:
                    unregister_process(pid, generation=generation)

            if process.returncode != 0:
                # Stderr can include provider and credential diagnostics.  The
                # detailed data stays local to the CLI process; the workflow
                # boundary exposes only a stable failure class.
                raise RunnerError(f"Claude Code exited with code {process.returncode}")

            response = stdout.decode("utf-8").strip()

            if not response:
                raise RunnerError("Claude Code returned empty response")

            return response

        finally:
            with suppress(OSError):
                os.unlink(prompt_file)

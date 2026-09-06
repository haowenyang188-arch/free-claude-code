"""Claude Code runtime adapter for executing tasks through Claude Code."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from cli.process_registry import register_process, unregister_process
from cli.runtime_environment import (
    build_cli_environment,
    resolve_explicit_mcp_config,
)
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
        mcp_config_path: str | None = None,
        execution_timeout: float | None = None,
        workspace_path: str | None = None,
    ) -> None:
        """Initialize adapter with optional artifact store for loading upstream artifacts.

        workspace_path: default cwd when the task context carries no
        workspace_scope.  The Claude CLI runs its plan-mode read-only tools
        against the cwd tree; inheriting the host process cwd (e.g. the
        free-claude-code repo root with ~900 files) makes a plan invocation
        hang until timeout because the model explores the whole tree.
        """
        if isolation_mode not in {"safe", "inherit"}:
            raise ValueError("isolation_mode must be 'safe' or 'inherit'")
        self._artifact_store = artifact_store
        self._claude_bin = claude_bin
        self._isolation_mode = isolation_mode
        self._runtime_registry = runtime_registry or RuntimeRegistry(
            executables={RuntimeBackend.CLAUDE: claude_bin}
        )
        self._preflight_runtime = preflight_runtime
        self._mcp_config_path = (
            resolve_explicit_mcp_config(mcp_config_path)
            if isolation_mode == "inherit"
            else None
        )
        # P1-B gap fix (C2): the Claude CLI blocks indefinitely on slow API
        # responses, so the process timeout is configurable via env
        # CLAUDE_EXECUTION_TIMEOUT (seconds, default 300) to ride out
        # transient upstream slowness instead of failing at a hard 5 min.
        self._execution_timeout = (
            float(execution_timeout)
            if execution_timeout is not None
            else float(os.getenv("CLAUDE_EXECUTION_TIMEOUT", "300.0"))
        )
        self._workspace_path = workspace_path

    def _child_environment(self) -> dict[str, str]:
        """Least-privilege env plus loopback-only proxy passthrough (C2).

        ``build_cli_environment`` deliberately strips proxy state.  On hosts
        where outbound Anthropic API traffic must go through a local proxy
        (e.g. WSL with a 127.0.0.1 gateway), the Claude CLI hangs with zero
        output until timeout without it.  Only loopback/private proxy values
        are inherited (no credentials, no external proxy URLs).
        """
        env = build_cli_environment(RuntimeBackend.CLAUDE)
        parent = os.environ
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
            value = parent.get(key)
            if not value:
                continue
            lowered = value.lower()
            if key.upper().endswith("_PROXY") and not lowered.startswith(
                ("http://127.", "http://localhost", "http://[::1]", "socks5://127.", "socks5h://127.")
            ):
                continue  # external proxy: not inherited
            env[key] = value
        return env

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

        # Determine working directory: task workspace scope wins; otherwise
        # fall back to the adapter default workspace so the CLI never inherits
        # the host process cwd (see workspace_path docstring).
        cwd = context.workspace_scope if context.workspace_scope else self._workspace_path

        # Invoke Claude Code
        try:
            response = await self._invoke_claude_code(prompt=prompt, cwd=cwd)
        except Exception as e:
            raise RunnerError(f"Claude Code invocation failed: {e}") from e

        # Generate summary (first 100 chars)
        summary = response[:100] + "..." if len(response) > 100 else response

        # P1-B gap fix (C1): artifact type must follow the SOP role so the
        # engine step contract (plan -> ArtifactType.PLAN, review ->
        # REVIEW_REPORT) holds for the real Claude runner.  Previously the
        # real runner always emitted TEXT, so a real plan step could never
        # satisfy the PLAN evidence assertions in test_real_e2e.
        role = (task.role_id or "").lower()
        if role in {"planner", "claude"}:
            artifact_type = ArtifactType.PLAN
        elif role in {"reviewer", "codex"}:
            artifact_type = ArtifactType.REVIEW_REPORT
        else:
            artifact_type = ArtifactType.TEXT

        # Create artifact
        artifact = Artifact(
            id=f"artifact-{task.id}",
            task_id=task.id,
            type=artifact_type,
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
                cmd.extend(["--safe-mode", "--strict-mcp-config"])
            elif self._isolation_mode == "inherit":
                mcp_config = self._mcp_config_path
                if mcp_config:
                    cmd.extend(["--strict-mcp-config", "--mcp-config", mcp_config])
            cmd.extend(["--permission-mode", "plan"])
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
                env=self._child_environment(),
            )
            pid = getattr(process, "pid", None)
            if isinstance(pid, int) and pid > 0:
                register_process(pid, generation=generation)

            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self._execution_timeout
                )
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                raise RunnerError(
                    f"Claude Code execution timeout after {int(self._execution_timeout)}s"
                ) from exc
            except asyncio.CancelledError:
                # Run cancellation (engine stop) must not orphan the CLI child.
                process.kill()
                with suppress(Exception):
                    await process.wait()
                raise
            finally:
                if isinstance(pid, int) and pid > 0:
                    unregister_process(pid, generation=generation)

            if process.returncode != 0:
                # Stderr can include provider and credential diagnostics.  The
                # detailed data stays local to the CLI process; the workflow
                # boundary exposes only a stable failure class.  Diagnostics
                # are logged server-side for operator debugging.
                from loguru import logger

                stderr_tail = (
                    _stderr.decode("utf-8", errors="replace")[-500:] if _stderr else ""
                )
                logger.error(
                    "claude_code adapter exit={} stdout_tail={} stderr_tail={}",
                    process.returncode,
                    stdout.decode("utf-8", errors="replace")[-300:],
                    stderr_tail,
                )
                raise RunnerError(f"Claude Code exited with code {process.returncode}")

            response = stdout.decode("utf-8").strip()

            if not response:
                raise RunnerError("Claude Code returned empty response")

            return response

        finally:
            with suppress(OSError):
                os.unlink(prompt_file)

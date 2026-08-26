"""Runtime-neutral StepRunner that adapts to different agent execution environments."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workbench.backend.domain.models import (
        Artifact,
        ContextPackage,
        RuntimeKind,
        SubagentAssignment,
        Task,
    )


class RunnerError(Exception):
    """Raised when task execution fails at the runner layer."""


class RuntimeAdapter(ABC):
    """Abstract adapter for executing tasks in a specific runtime environment."""

    @abstractmethod
    async def execute_step(
        self,
        *,
        task: Task,
        assignment: SubagentAssignment,
        context: ContextPackage,
    ) -> Artifact:
        """Execute one bounded task in this runtime and return the proposed artifact.

        Args:
            task: The task definition with role, title, and constraints
            assignment: The agent instance assigned to execute this task
            context: Complete context package with goal, instructions, artifacts, decisions

        Returns:
            Artifact with task_id matching the input task

        Raises:
            RunnerError: If execution fails or times out
        """

    @abstractmethod
    def supports(self, runtime_kind: RuntimeKind) -> bool:
        """Return True if this adapter can execute tasks in the given runtime."""


class RuntimeNeutralRunner:
    """Bridges WorkflowEngine to concrete runtime adapters (Claude Code, Codex, etc.)."""

    def __init__(self, adapters: list[RuntimeAdapter]) -> None:
        self._adapters = adapters
        self._runtime_registry: dict[str, RuntimeKind] = {}

    def register_runtime(self, runtime_id: str, kind: RuntimeKind) -> None:
        """Register a runtime instance with its kind for routing."""
        self._runtime_registry[runtime_id] = kind

    async def execute(
        self,
        *,
        task: Task,
        assignment: SubagentAssignment,
        context: ContextPackage,
    ) -> Artifact:
        """Execute one bounded task through the appropriate runtime adapter.

        Args:
            task: The task to execute
            assignment: Agent assignment specifying which runtime to use
            context: Complete context package for task execution

        Returns:
            Artifact produced by the runtime

        Raises:
            RunnerError: If runtime is unknown, no adapter supports it, or execution fails
        """
        # Resolve runtime_id to RuntimeKind
        runtime_id = assignment.runtime_id
        if runtime_id not in self._runtime_registry:
            raise RunnerError(
                f"unknown runtime: {runtime_id} (not registered in runtime_registry)"
            )

        runtime_kind = self._runtime_registry[runtime_id]

        # Find adapter that supports this runtime kind
        adapter = None
        for candidate in self._adapters:
            if candidate.supports(runtime_kind):
                adapter = candidate
                break

        if adapter is None:
            raise RunnerError(
                f"no adapter supports RuntimeKind.{runtime_kind.value} (have {len(self._adapters)} adapters)"
            )

        # Execute through adapter
        artifact = await adapter.execute_step(
            task=task,
            assignment=assignment,
            context=context,
        )

        # Validate artifact references correct task
        if artifact.task_id != task.id:
            raise RunnerError(
                f"task_id mismatch: artifact.task_id={artifact.task_id} but task.id={task.id}"
            )

        return artifact

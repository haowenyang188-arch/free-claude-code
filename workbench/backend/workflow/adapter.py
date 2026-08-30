"""Runtime adapter contract v2 (multi-artifact execution).

Execution-side revision: the runtime adapter output contract is NO LONGER a
single Artifact.  One executor Attempt may produce DIFF + TEST_REPORT (plus
optional further evidence).  The Engine stamps every returned artifact with
task_id / attempt_id / producer_step_run_id / schema_version.

This module is deliberately small and import-light: adapters that already
implement workbench.backend.workflow.runners.RuntimeAdapter (single
artifact, bridge path) stay untouched; the SOP Engine binds to
ExecutionAdapter through runners: dict[str, ExecutionAdapter].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..domain.models import Artifact, ContextPackage, SubagentAssignment, Task


@dataclass
class RuntimeExecutionResult:
    """One runtime execution of one Attempt; may carry multiple artifacts.

    Attributes:
        artifacts: every artifact produced by this execution.  For an executor
            Attempt this MUST include a DIFF and a TEST_REPORT artifact (the
            engine never synthesizes a TEST_REPORT from DIFF content).
        session_id: runtime session used for the attempt (session continuity).
        runtime_metadata: provider/runtime facts (kind, version, endpoint...).
        execution_metadata: per-run facts (model, usage, timings...).
    """

    artifacts: list[Artifact]
    session_id: str | None = None
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    execution_metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Unified interface for all execution providers (contract v2).

    Implementers do NOT need to know about Attempts: the Engine stamps
    task_id / attempt_id / producer_step_run_id / schema_version on every
    returned artifact.
    """

    async def execute(
        self,
        *,
        task: Task,
        assignment: SubagentAssignment,
        context: ContextPackage,
    ) -> RuntimeExecutionResult:
        """Execute one task attempt; return all produced artifacts.

        Raises:
            RuntimeUnavailableError: provider is not usable right now
                (fail closed; never silently fall back to a fake).
        """
        ...

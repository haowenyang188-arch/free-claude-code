"""Runtime adapter factory: fake (deterministic) or real (fail-closed).

Real mode wires the EXISTING runtime implementations (revision #4 - never
invent a new API-key runner):
- claude: ClaudeCodeAdapter (real Claude Code CLI)
- codex: CodexSubagentRunner + CodexAdapter (real Codex CLI / app-server)

Real mode NEVER falls back to fake: if no runtime is available it raises
RuntimeUnavailableError.  If some runtimes are available they are registered
and the Engine fails at execution time for an unavailable runtime_id.
"""

from __future__ import annotations

import os
import uuid
from contextlib import suppress
from typing import Any

from ..domain.models import Artifact, ArtifactType
from .adapter import ExecutionAdapter, RuntimeExecutionResult


class RuntimeUnavailableError(RuntimeError):
    """Raised when a requested real runtime cannot be used (fail closed)."""


class _FakeExecutionAdapter:
    """Deterministic test double implementing contract v2.

    planner -> PLAN; executor -> DIFF + TEST_REPORT (two distinct artifacts);
    reviewer -> REVIEW_REPORT.  Only used under RUNTIME_MODE=fake (explicit
    dev/test choice); production real mode never constructs this class.
    """

    def __init__(self, role: str, *, content: str | None = None) -> None:
        self.role = role
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        self.calls.append(
            {
                "task_id": task.id,
                "role_id": task.role_id,
                "runtime_id": assignment.runtime_id,
            }
        )
        if task.role_id in {"planner", "claude"}:
            artifacts = [
                Artifact(
                    id=str(uuid.uuid4()),
                    task_id=task.id,
                    type=ArtifactType.PLAN,
                    content=self.content or f"# Plan for {task.title}",
                    summary=f"Plan {task.title}",
                )
            ]
        elif task.role_id in {"reviewer", "codex"}:
            artifacts = [
                Artifact(
                    id=str(uuid.uuid4()),
                    task_id=task.id,
                    type=ArtifactType.REVIEW_REPORT,
                    content=self.content
                    or '{"result": "PASS", "blocking": [], "non_blocking": [], "evidence": ["fake"]}',
                    summary="REVIEW: PASS",
                )
            ]
        else:
            diff = Artifact(
                id=str(uuid.uuid4()),
                task_id=task.id,
                type=ArtifactType.DIFF,
                content="+def feature():\n+    return True",
                summary="DIFF",
            )
            test = Artifact(
                id=str(uuid.uuid4()),
                task_id=task.id,
                type=ArtifactType.TEST_REPORT,
                content="PASS (5/5)",
                summary="TEST_REPORT: PASS 5/5",
            )
            artifacts = [diff, test]
        return RuntimeExecutionResult(
            artifacts=artifacts,
            session_id=f"fake-session-{assignment.runtime_id}",
            runtime_metadata={"runtime_id": assignment.runtime_id},
        )


class _WrappingExecutionAdapter:
    """Wrap a legacy single-artifact runner into contract v2."""

    def __init__(self, inner: Any, *, runtime_id: str) -> None:
        self._inner = inner
        self.runtime_id = runtime_id

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        artifact = await self._run_inner(task=task, assignment=assignment, context=context)
        return RuntimeExecutionResult(
            artifacts=[artifact],
            session_id=self._session_id(),
            runtime_metadata={
                "runtime_id": self.runtime_id,
                "adapter": type(self._inner).__name__,
            },
        )

    async def _run_inner(self, *, task, assignment, context) -> Artifact:
        raise NotImplementedError

    def _session_id(self) -> str | None:
        return None


class _ClaudeExecutionAdapter(_WrappingExecutionAdapter):
    async def _run_inner(self, *, task, assignment, context) -> Artifact:
        return await self._inner.execute_step(
            task=task, assignment=assignment, context=context
        )


class _CodexExecutionAdapter(_WrappingExecutionAdapter):
    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        inner = await self._inner.execute(
            task=task, assignment=assignment, context=context
        )
        # codex_executor 返回 (DIFF, TEST_REPORT) 元组——执行证据链需要
        # 两件同 attempt 工件;审核者仍为单 REVIEW_REPORT。
        artifacts = list(inner) if isinstance(inner, tuple) else [inner]
        return RuntimeExecutionResult(
            artifacts=artifacts,
            session_id=self._session_id(),
            runtime_metadata={
                "runtime_id": self.runtime_id,
                "adapter": type(self._inner).__name__,
            },
        )

    async def _run_inner(self, *, task, assignment, context) -> Artifact:
        return await self._inner.execute(
            task=task, assignment=assignment, context=context
        )

    def _session_id(self) -> str | None:
        codex_adapter = getattr(self._inner, "codex_adapter", None)
        if codex_adapter is None:
            return None
        return getattr(codex_adapter, "session_id", None)



def create_runners(
    *,
    mode: str | None = None,
    store: Any = None,
    artifact_store: Any = None,
    workspace_path: str | None = None,
    cross_os_workspace: str | None = None,
) -> dict[str, ExecutionAdapter]:
    """Build runtime adapters from configuration.

    Args:
        mode: "fake" (default) or "real".  Overrides RUNTIME_MODE env.
        store: workflow store (forwarded to real adapters that need it).
        artifact_store: file artifact store (forwarded to real adapters).
        workspace_path: workspace root for real CLI executions.
        cross_os_workspace: optional shared-workspace view for WSL-side
            runtimes (Claude/Codex) so they observe the SAME physical files
            written from Windows (``C:\\home\\gnen\\free-claude-code``; WSL
            sees ``/mnt/c/home/gnen/free-claude-code``).  Passing the mnt
            view here lets Codex review independently verify the executor's
            artifacts instead of REWORKing on a missing file.

    Returns:
        dict mapping runtime_id -> ExecutionAdapter.

    Raises:
        RuntimeUnavailableError: RUNTIME_MODE=real with no usable runtime.
        ValueError: unknown mode.
    """
    runtime_mode = mode or os.getenv("RUNTIME_MODE", "fake")
    if runtime_mode == "fake":
        planner = _FakeExecutionAdapter("planner")
        executor = _FakeExecutionAdapter("executor")
        reviewer = _FakeExecutionAdapter("reviewer")
        # "dsh" 键仅为旧引擎测试的 executor 角色映射保留（fake 适配器按角色
        # 分派，与真实 DSH 桥无关）；real 模式不再提供 dsh。
        return {
            "claude": planner,
            "dsh": executor,
            "codex": reviewer,
            "default": executor,
        }

    if runtime_mode == "real":
        runners: dict[str, ExecutionAdapter] = {}
        # B1: WSL-side runtimes work on the shared (mnt) view when provided.
        cli_workspace = cross_os_workspace or workspace_path
        try:
            from ..workflow.adapters.claude_code import ClaudeCodeAdapter

            claude_inner = ClaudeCodeAdapter(
                artifact_store=artifact_store, workspace_path=cli_workspace
            )
            runners["claude"] = _ClaudeExecutionAdapter(claude_inner, runtime_id="claude")
        except Exception:
            claude_inner = None
        try:
            from ..agents.codex_adapter import CodexAdapter
            from ..agents.codex_runner import CodexSubagentRunner

            codex_inner = CodexSubagentRunner(
                codex_adapter=CodexAdapter(agent_id="sop-reviewer"),
                workspace_path=cli_workspace or ".",
                store=store,
                artifact_store=artifact_store,
            )
            runners["codex"] = _CodexExecutionAdapter(codex_inner, runtime_id="codex")
        except Exception:
            codex_inner = None
        if not runners:
            raise RuntimeUnavailableError(
                "RUNTIME_MODE=real but no runtime could be constructed "
                "(Claude/Codex). Refusing to fall back to fake."
            )
        return runners

    raise ValueError(f"Unknown RUNTIME_MODE: {runtime_mode!r}")

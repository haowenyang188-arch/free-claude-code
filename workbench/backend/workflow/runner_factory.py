"""Runtime adapter factory: fake (deterministic) or real (fail-closed).

Real mode wires the EXISTING runtime implementations (revision #4 - never
invent a new API-key runner):
- claude: ClaudeCodeAdapter (real Claude Code CLI)
- codex: CodexSubagentRunner + CodexAdapter (real Codex CLI / app-server)
- dsh: DeepSeekHarnessAdapter + DshClient (DSH Desktop v2 bridge)

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
    async def _run_inner(self, *, task, assignment, context) -> Artifact:
        return await self._inner.execute(
            task=task, assignment=assignment, context=context
        )

    def _session_id(self) -> str | None:
        codex_adapter = getattr(self._inner, "codex_adapter", None)
        if codex_adapter is None:
            return None
        return getattr(codex_adapter, "session_id", None)


class _DSHExecutionAdapter:
    """Real DSH Desktop executor (contract v2: DIFF + TEST_REPORT).

    Executes through DshClient (DSH Desktop v2 API) - NOT the legacy sidecar
    bridge (which carried an api_key).  The executor prompt demands explicit
    ---DIFF--- and ---TEST_REPORT--- markers; the adapter splits them into two
    distinct artifacts and FAILS CLOSED when a marker is missing (never
    fabricates a TEST_REPORT from DIFF content).
    """

    DIFF_MARKER = "---DIFF---"
    TEST_MARKER = "---TEST_REPORT---"

    def __init__(
        self,
        client: Any,
        *,
        workspace_path: str | None = None,
        poll_interval: float = 1.0,
        timeout: float = 300.0,
    ) -> None:
        self._client = client
        self._workspace_path = workspace_path
        self._poll_interval = poll_interval
        self._timeout = timeout

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        prompt_text = self._build_prompt(task, context)
        try:
            session_id = self._client.create_session(cwd=self._workspace_path or ".")
        except Exception as exc:
            raise RuntimeUnavailableError(
                f"DSH Desktop session.create failed: {exc}"
            ) from exc
        try:
            self._client.prompt(session_id, prompt_text, mode="queue")
            output = await self._poll_output(session_id)
        except Exception as exc:
            raise RuntimeUnavailableError(
                f"DSH Desktop execution failed: {exc}"
            ) from exc
        finally:
            with suppress(Exception):
                self._client.archive_session(session_id)
        if not output.strip():
            raise RuntimeUnavailableError(
                "DSH Desktop produced no output; refusing to fabricate artifacts"
            )
        if task.role_id in {"planner", "claude"}:
            artifacts = [
                Artifact(
                    id=str(uuid.uuid4()),
                    task_id=task.id,
                    type=ArtifactType.PLAN,
                    content=output,
                    summary=output[:100],
                )
            ]
        elif task.role_id in {"reviewer", "codex"}:
            artifacts = [
                Artifact(
                    id=str(uuid.uuid4()),
                    task_id=task.id,
                    type=ArtifactType.REVIEW_REPORT,
                    content=output,
                    summary="REVIEW",
                )
            ]
        else:
            artifacts = self._split_diff_test(output, task.id)
        return RuntimeExecutionResult(
            artifacts=artifacts,
            session_id=session_id,
            runtime_metadata={"runtime_id": "dsh", "transport": "DshClient/Desktop-v2"},
        )

    def _build_prompt(self, task, context) -> str:
        return (
            f"# Task: {task.title}\n\n"
            f"## Goal\n{context.goal_summary}\n\n"
            f"## Instructions\n{context.instructions}\n\n"
            "## Required output format\n"
            f"Emit exactly two sections:\n"
            f"{self.DIFF_MARKER}\n<the diff>\n"
            f"{self.TEST_MARKER}\n<the test report>"
        )

    async def _poll_output(self, session_id: str) -> str:
        import asyncio
        import time

        deadline = time.monotonic() + self._timeout
        last_text = ""
        while time.monotonic() < deadline:
            events = self._client.history(session_id)
            text = self._collect_text(events)
            if text:
                last_text = text
            if self._terminal(events):
                break
            await asyncio.sleep(self._poll_interval)
        return last_text

    @staticmethod
    def _collect_text(events: list[dict[str, Any]]) -> str:
        pieces: list[str] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            payload = event.get("payload", event)
            if not isinstance(payload, dict):
                continue
            for key in ("text", "message", "content"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    pieces.append(value.strip())
            frame = payload.get("payload") if isinstance(payload, dict) else None
            if isinstance(frame, dict):
                inner = frame.get("event")
                if isinstance(inner, dict):
                    for key in ("text", "message"):
                        value = inner.get(key)
                        if isinstance(value, str) and value.strip():
                            pieces.append(value.strip())
        return "\n".join(pieces)

    @staticmethod
    def _terminal(events: list[dict[str, Any]]) -> bool:
        markers = ("finished", "completed", "failed", "cancelled", "error")
        for event in events:
            if not isinstance(event, dict):
                continue
            raw = str(event.get("raw_type", "")) + " " + str(event.get("type", ""))
            if any(marker in raw.lower() for marker in markers):
                return True
        return False

    def _split_diff_test(self, output: str, task_id: str) -> list[Artifact]:
        diff_body = self._section(output, self.DIFF_MARKER, self.TEST_MARKER)
        test_body = self._section(output, self.TEST_MARKER, None)
        if diff_body is None or test_body is None:
            raise RuntimeUnavailableError(
                "DSH executor output lacks ---DIFF--- / ---TEST_REPORT--- markers; "
                "refusing to fabricate a TEST_REPORT from DIFF content"
            )
        return [
            Artifact(
                id=str(uuid.uuid4()),
                task_id=task_id,
                type=ArtifactType.DIFF,
                content=diff_body,
                summary="DIFF",
            ),
            Artifact(
                id=str(uuid.uuid4()),
                task_id=task_id,
                type=ArtifactType.TEST_REPORT,
                content=test_body,
                summary="TEST_REPORT",
            ),
        ]

    @staticmethod
    def _section(output: str, start: str, end: str | None) -> str | None:
        if start not in output:
            return None
        body = output.split(start, 1)[1]
        if end is not None and end in body:
            body = body.split(end, 1)[0]
        return body.strip()


def create_runners(
    *,
    mode: str | None = None,
    store: Any = None,
    artifact_store: Any = None,
    workspace_path: str | None = None,
) -> dict[str, ExecutionAdapter]:
    """Build runtime adapters from configuration.

    Args:
        mode: "fake" (default) or "real".  Overrides RUNTIME_MODE env.
        store: workflow store (forwarded to real adapters that need it).
        artifact_store: file artifact store (forwarded to real adapters).
        workspace_path: workspace root for real CLI executions.

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
        return {
            "claude": planner,
            "dsh": executor,
            "codex": reviewer,
            "default": executor,
        }

    if runtime_mode == "real":
        runners: dict[str, ExecutionAdapter] = {}
        try:
            from ..workflow.adapters.claude_code import ClaudeCodeAdapter

            claude_inner = ClaudeCodeAdapter(artifact_store=artifact_store)
            runners["claude"] = _ClaudeExecutionAdapter(claude_inner, runtime_id="claude")
        except Exception:
            claude_inner = None
        try:
            from ..agents.codex_adapter import CodexAdapter
            from ..agents.codex_runner import CodexSubagentRunner

            codex_inner = CodexSubagentRunner(
                codex_adapter=CodexAdapter(agent_id="sop-reviewer"),
                workspace_path=workspace_path or ".",
                store=store,
                artifact_store=artifact_store,
            )
            runners["codex"] = _CodexExecutionAdapter(codex_inner, runtime_id="codex")
        except Exception:
            codex_inner = None
        try:
            from ..agents.dsh_transport import DshClient, discover_dsh_desktop_endpoint

            dsh_client = DshClient(discover=discover_dsh_desktop_endpoint)
            runners["dsh"] = _DSHExecutionAdapter(
                dsh_client, workspace_path=workspace_path
            )
        except Exception:
            dsh_client = None
        if not runners:
            raise RuntimeUnavailableError(
                "RUNTIME_MODE=real but no runtime could be constructed "
                "(Claude/Codex/DSH). Refusing to fall back to fake."
            )
        return runners

    raise ValueError(f"Unknown RUNTIME_MODE: {runtime_mode!r}")

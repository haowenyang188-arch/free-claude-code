"""P1-B gap-fix C1 regression: real ClaudeCodeAdapter artifact types follow role.

plan task -> ArtifactType.PLAN; review task -> REVIEW_REPORT; other -> TEXT.
CLI is mocked; no real runtime needed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from workbench.backend.domain.models import (
    ArtifactType,
    ContextPackage,
    SubagentAssignment,
    Task,
    TaskStatus,
)
from workbench.backend.workflow.adapters.claude_code import ClaudeCodeAdapter


def _task(role_id: str) -> Task:
    return Task(
        id=f"t-{role_id}",
        step_run_id="sr",
        title="Step",
        description="d",
        role_id=role_id,
        status=TaskStatus.RUNNING,
    )


def _assignment(role_id: str) -> SubagentAssignment:
    return SubagentAssignment(
        id=f"a-{role_id}",
        task_id=f"t-{role_id}",
        role_id=role_id,
        agent_instance_id=f"agent-{role_id}",
        runtime_id="claude",
    )


def _context() -> ContextPackage:
    return ContextPackage(
        id="c1", task_id="t-x", goal_summary="goal", instructions="do it"
    )


def run(role_id: str):
    async def _run() -> ArtifactType:
        adapter = ClaudeCodeAdapter()
        with patch.object(adapter, "_invoke_claude_code", new=AsyncMock(return_value="done")):
            return await adapter.execute_step(
                task=_task(role_id), assignment=_assignment(role_id), context=_context()
            )

    return asyncio.run(_run())


def test_planner_role_produces_plan_artifact():
    assert run("planner").type is ArtifactType.PLAN
    assert run("claude").type is ArtifactType.PLAN


def test_reviewer_role_produces_review_report():
    assert run("reviewer").type is ArtifactType.REVIEW_REPORT
    assert run("codex").type is ArtifactType.REVIEW_REPORT


def test_other_role_stays_text():
    assert run("executor").type is ArtifactType.TEXT
    assert run("").type is ArtifactType.TEXT

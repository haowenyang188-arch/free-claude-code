"""RC-6 regression tests: the HTTP layer may not decide a task terminal state.

The fix moved the legacy run -> task status mapping into
``workbench/backend/workflow/run_relay.py``, where the write goes through
``role_contract.apply_status`` (Engine authority).  These tests pin that
contract so it cannot silently regress.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from workbench.backend.models import RunStatus, Task, TaskStatus
from workbench.backend.workflow.role_contract import (
    SOURCE_RULES,
    AgentRole,
    RoleContractViolation,
)
from workbench.backend.workflow.run_relay import (
    RUN_STATUS_TO_TASK_STATUS,
    relay_run_status_to_task,
    relay_task_record_terminal,
    task_status_for_run_status,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "workbench" / "backend" / "main.py"


def _task() -> Task:
    return Task(
        id="task-1",
        title="t",
        description="d",
        agent_id="agent-1",
        workspace_path=".",
    )


def test_run_status_mapping_covers_legacy_transitions() -> None:
    """Every legacy run status that used to drive a task keeps its mapping."""
    assert RUN_STATUS_TO_TASK_STATUS == {
        RunStatus.COMPLETED: TaskStatus.COMPLETED,
        RunStatus.FAILED: TaskStatus.FAILED,
        RunStatus.PAUSED: TaskStatus.PAUSED,
        RunStatus.CANCELLED: TaskStatus.CANCELLED,
        RunStatus.WAITING_HUMAN: TaskStatus.WAITING_HUMAN,
    }


@pytest.mark.parametrize("run_status", [RunStatus.STARTED, RunStatus.RUNNING])
def test_non_mapped_run_status_is_not_relayed(run_status: RunStatus) -> None:
    """A run status with no task-level counterpart writes nothing."""
    task = _task()
    task.status = TaskStatus.RUNNING

    assert task_status_for_run_status(run_status) is None
    assert relay_run_status_to_task(task, run_status) is None
    assert task.status is TaskStatus.RUNNING


def test_relay_applies_the_implied_task_status() -> None:
    task = _task()

    applied = relay_run_status_to_task(task, RunStatus.COMPLETED)

    assert applied is TaskStatus.COMPLETED
    assert task.status is TaskStatus.COMPLETED


def test_relay_rejects_an_agent_actor() -> None:
    """Terminality belongs to the Engine; an agent relay is a contract breach."""
    task = _task()

    with pytest.raises(RoleContractViolation):
        relay_run_status_to_task(task, RunStatus.COMPLETED, actor=AgentRole.CLAUDE)

    assert task.status is TaskStatus.PENDING


def test_task_record_relay_writes_under_engine_authority() -> None:
    """RC-5: the bridge writes terminal states through the same relay."""
    record: dict[str, object] = {"status": "running"}

    written = relay_task_record_terminal(record, TaskStatus.COMPLETED)

    assert written is TaskStatus.COMPLETED
    assert record["status"] is TaskStatus.COMPLETED


def test_task_record_relay_rejects_an_agent_actor() -> None:
    """The mapping form is gated exactly like the object form."""
    record: dict[str, object] = {"status": "running"}

    with pytest.raises(RoleContractViolation):
        relay_task_record_terminal(record, TaskStatus.FAILED, actor=AgentRole.CODEX)

    assert record["status"] == "running"


def test_http_layer_holds_no_terminal_task_assignment() -> None:
    """RC-6 stays closed: main.py must not set a terminal task status itself."""
    rule = next(item for item in SOURCE_RULES if item.rule_id == "RC-6")
    pattern = re.compile(rule.pattern)
    offenders = [
        f"{MAIN_PY.relative_to(REPO_ROOT).as_posix()}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(
            MAIN_PY.read_text(encoding="utf-8").splitlines(), 1
        )
        if pattern.search(line)
    ]

    assert not offenders, "HTTP layer regressed to RC-6: " + "; ".join(offenders)

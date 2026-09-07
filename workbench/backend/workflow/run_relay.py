"""Authorized relay from a legacy run status to its task status.

RC-6 used to be an open contract violation: the HTTP layer
(``workbench/backend/main.py``) translated an agent terminal event into a task
terminal state on its own authority (``RUN_FINISHED`` -> ``task.status =
TaskStatus.COMPLETED``), bypassing both the Engine and the Reviewer.

The HTTP layer still has to *react* to the event, so it now calls
:func:`relay_run_status_to_task`.  The mapping table and the write itself live
in the workflow package and go through
:func:`workbench.backend.workflow.role_contract.apply_status`, the single
sanctioned status-write channel owned by ``SOP_ENGINE``.  The HTTP layer
reports; the workflow layer decides and writes.

Scope: legacy ``/api/tasks`` runs only.  SOP v2 tasks are Engine-owned, never
enter ``WorkbenchService.tasks``, and therefore never reach this relay.
"""

from __future__ import annotations

from ..models import RunStatus, TaskStatus
from .role_contract import AgentRole, apply_status

__all__ = [
    "RUN_STATUS_TO_TASK_STATUS",
    "relay_run_status_to_task",
    "task_status_for_run_status",
]

#: Legacy run status -> the task status it implies.  A run status that is
#: absent here carries no task-level transition (``STARTED``/``RUNNING`` do
#: not change the task, which is already RUNNING at that point).
RUN_STATUS_TO_TASK_STATUS: dict[RunStatus, TaskStatus] = {
    RunStatus.COMPLETED: TaskStatus.COMPLETED,
    RunStatus.FAILED: TaskStatus.FAILED,
    RunStatus.PAUSED: TaskStatus.PAUSED,
    RunStatus.CANCELLED: TaskStatus.CANCELLED,
    RunStatus.WAITING_HUMAN: TaskStatus.WAITING_HUMAN,
}


def task_status_for_run_status(run_status: RunStatus) -> TaskStatus | None:
    """Return the task status implied by ``run_status``.

    Args:
        run_status: the legacy run status observed by the HTTP layer.

    Returns:
        The matching task status, or ``None`` when the run status implies no
        task-level transition.
    """
    return RUN_STATUS_TO_TASK_STATUS.get(run_status)


def relay_run_status_to_task(
    task: object,
    run_status: RunStatus,
    *,
    actor: AgentRole = AgentRole.SOP_ENGINE,
) -> TaskStatus | None:
    """Apply the task status implied by ``run_status`` under Engine authority.

    This is the only path the HTTP layer may use to move a legacy task; the
    write is delegated to :func:`apply_status` so that an agent actor is
    rejected the same way it would be anywhere else in the workflow.

    Args:
        task: the legacy ``Task`` model being updated.
        run_status: the run status that triggered the relay.
        actor: who performs the write; defaults to the Engine.

    Returns:
        The status that was applied, or ``None`` when ``run_status`` has no
        task-level counterpart (nothing is written in that case).

    Raises:
        RoleContractViolation: if ``actor`` does not hold
            ``Capability.TRANSITION_TASK``.
    """
    value = task_status_for_run_status(run_status)
    if value is None:
        return None
    apply_status(task, kind="tasks", value=value, actor=actor)
    return value

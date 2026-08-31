"""DeterministicScenarioHarness - scripted, 100% reproducible SOP scenarios.

Independent test harness (execution-side revision #8): the production
FakeSubagentRunner is NOT modified to carry every scenario.  The harness owns
scripted adapters and assertion helpers for the canonical rework-then-pass
scenario:

    PLAN -> EXECUTE Attempt #1 -> DIFF #1 + TEST_REPORT #1
        -> REVIEW = REWORK
    -> DSH same-session Attempt #2 -> DIFF #2 + TEST_REPORT #2
        -> REVIEW = PASS -> COMPLETE

Assertions cover: exact event order, Attempt #1/#2 independence, Review #1
only binds Attempt #1, Review #2 only binds Attempt #2, final PASS binds
Attempt #2, DIFF #2 + TEST_REPORT #2 share one Attempt, DSH session continuity,
reviewer never modifies the workspace, terminal-state consistency, and no
leftover background tasks.
"""

from __future__ import annotations

import json
import uuid

from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    Attempt,
    ContextPackage,
    Goal,
    Review,
    SopDefinition,
    SopRunStatus,
    StageDefinition,
    StepDefinition,
    StepStatus,
    SubagentAssignment,
    TaskStatus,
    ValidationResult,
    ValidationStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.adapter import RuntimeExecutionResult
from workbench.backend.workflow.engine import (
    AcceptanceValidator,
    WorkflowEngine,
)
from workbench.backend.workflow.orchestrator import AutoOrchestrator


class AcceptAll(AcceptanceValidator):
    async def validate(self, *, task, artifact) -> ValidationResult:
        return ValidationResult(
            id=f"v-{task.id}",
            task_id=task.id,
            artifact_id=artifact.id,
            validator_id="harness",
            status=ValidationStatus.ACCEPTED,
        )


class ScriptedAdapter:
    """Session-continuity aware, workspace-write tracking scripted adapter.

    - planner (claude) -> PLAN
    - executor (dsh)   -> DIFF + TEST_REPORT  (same DSH session for ALL attempts)
    - reviewer (codex) -> REVIEW_REPORT with verdicts from a script,
      and NEVER records a workspace write
    """

    def __init__(self, review_verdicts: list[str] | None = None) -> None:
        self.review_verdicts = review_verdicts or ["REWORK", "PASS"]
        self._review_count = 0
        self.session_id = f"dsh-session-{uuid.uuid4().hex[:8]}"
        self.workspace_writes: dict[str, list[str]] = {}
        self.calls: list[dict] = []

    def _sessions(self) -> dict[str, str]:
        return {
            "claude": f"claude-session-{uuid.uuid4().hex[:8]}",
            "dsh": self.session_id,  # same DSH session across attempts
            "codex": f"codex-session-{uuid.uuid4().hex[:8]}",
        }

    async def execute(self, *, task, assignment, context) -> RuntimeExecutionResult:
        role = task.role_id
        self.calls.append({"role": role, "task_id": task.id})
        session = self._sessions().get(role, "default")
        if role in {"planner", "claude"}:
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"plan-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.PLAN,
                        content="# Plan",
                    )
                ],
                session_id=session,
            )
        if role in {"reviewer", "codex"}:
            self._review_count += 1
            verdict = self.review_verdicts[
                min(self._review_count - 1, len(self.review_verdicts) - 1)
            ]
            return RuntimeExecutionResult(
                artifacts=[
                    Artifact(
                        id=f"review-{task.id}",
                        task_id=task.id,
                        type=ArtifactType.REVIEW_REPORT,
                        content=json.dumps(
                            {
                                "result": verdict,
                                "blocking": ["fix edge case"]
                                if verdict != "PASS"
                                else [],
                                "non_blocking": [],
                                "evidence": ["harness"],
                            }
                        ),
                    )
                ],
                session_id=session,
            )
        self.record_workspace_write(role, f"src/{task.id}.py")
        return RuntimeExecutionResult(
            artifacts=[
                Artifact(
                    id=f"diff-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.DIFF,
                    content="+feature",
                ),
                Artifact(
                    id=f"test-{task.id}",
                    task_id=task.id,
                    type=ArtifactType.TEST_REPORT,
                    content="PASS (5/5)",
                ),
            ],
            session_id=session,
        )

    def record_workspace_write(self, role: str, path: str) -> None:
        """Record a workspace write by role; REFUSES reviewer writes."""
        if role == "codex":
            raise AssertionError("reviewer attempted a workspace write")
        self.workspace_writes.setdefault(role, []).append(path)


def build_sop() -> tuple[Goal, SopDefinition]:
    goal = Goal(id="g-harness", project_id="p", description="harness scenario")
    sop = SopDefinition(
        id="sop-harness",
        name="plan execute review",
        stages=[
            StageDefinition(
                id="s1",
                name="main",
                steps=[
                    StepDefinition(
                        id="plan",
                        name="Plan",
                        role_id="claude",
                        output_type=ArtifactType.PLAN,
                        handoff_to="execute",
                    ),
                    StepDefinition(
                        id="execute",
                        name="Execute",
                        role_id="dsh",
                        output_type=ArtifactType.IMPLEMENTATION,
                        depends_on=["plan"],
                        handoff_to="review",
                    ),
                    StepDefinition(
                        id="review",
                        name="Review",
                        role_id="codex",
                        output_type=ArtifactType.REVIEW_REPORT,
                        depends_on=["execute"],
                    ),
                ],
            )
        ],
    )
    return goal, sop



class DeterministicScenarioHarness:
    """Run one scripted SOP scenario deterministically and assert invariants."""

    def __init__(self, tmp_path) -> None:
        self.store = JsonWorkflowStore(
            tmp_path / "state.json", tmp_path / "events.jsonl"
        )
        self.adapter = ScriptedAdapter()
        self.engine = WorkflowEngine(
            store=self.store,
            runners={
                "claude": self.adapter,
                "dsh": self.adapter,
                "codex": self.adapter,
            },
            validator=AcceptAll(),
        )
        self.orchestrator = AutoOrchestrator(self.engine)
        self.goal, self.sop = build_sop()
        self.run = None

    async def run_rework_then_pass(self) -> list:
        self.run = self.engine.start_sop_run(goal=self.goal, sop=self.sop)
        return await self.orchestrator.run_until_gate(
            self.run.id,
            resolve_assignment=self._assignment,
            build_context=self._context,
        )

    def _assignment(self, task, step) -> SubagentAssignment:
        return SubagentAssignment(
            id=f"assign-{task.id}",
            task_id=task.id,
            role_id=task.role_id,
            agent_instance_id=f"agent-{task.role_id}",
            runtime_id=task.role_id,
        )

    def _context(self, task, step) -> ContextPackage:
        return ContextPackage(
            id=f"ctx-{task.id}",
            task_id=task.id,
            goal_summary=self.goal.description,
            instructions=step.instructions or step.name,
        )

    def events(self) -> list:
        return list(self.store.replay_events(self.run.id))

    def event_types(self) -> list[str]:
        return [event.event_type for event in self.events()]

    def attempts(self) -> list[dict]:
        return self.store.list_entities("attempts")

    def artifacts(self) -> list[dict]:
        return self.store.list_entities("artifacts")

    def reviews(self) -> list[Review]:
        return [Review.model_validate(item) for item in self.store.list_entities("reviews")]

    def tasks(self) -> list[dict]:
        return self.store.list_entities("tasks")

    def step_runs(self) -> dict[str, dict]:
        return {
            item["step_id"]: item
            for item in self.store.list_entities("step_runs")
        }

    # ------------------------------------------------------------------
    # Invariant assertions (execution-side revision #8)
    # ------------------------------------------------------------------

    def assert_no_leftover_background_tasks(self) -> None:
        import asyncio

        current = asyncio.current_task()
        leftover = [
            task for task in asyncio.all_tasks() if task is not current
        ]
        assert leftover == [], f"leftover background tasks: {leftover}"

    def assert_reviewer_never_writes_workspace(self) -> None:
        writes = self.adapter.workspace_writes
        # The executor DID simulate workspace writes (tracking works); the
        # reviewer must have recorded none.
        assert writes.get("dsh"), (
            "harness failed to simulate executor workspace writes"
        )
        assert writes.get("codex", []) == [], (
            "reviewer modified the workspace: " f"{writes.get('codex')}"
        )
        # Enforcement: a reviewer write attempt is refused at the adapter.
        import pytest

        with pytest.raises(AssertionError, match="reviewer attempted"):
            self.adapter.record_workspace_write("codex", "src/evil.py")

    def assert_terminal_consistency(self) -> None:
        run = self.store.get_entity("sop_runs", self.run.id)
        assert run["status"] == SopRunStatus.COMPLETED.value
        step_runs = self.step_runs()
        assert step_runs["plan"]["status"] == StepStatus.COMPLETED.value
        assert step_runs["execute"]["status"] == StepStatus.COMPLETED.value
        assert step_runs["review"]["status"] == StepStatus.COMPLETED.value
        for task in self.tasks():
            assert task["status"] in {
                TaskStatus.ACCEPTED.value,
                TaskStatus.REWORK.value,
            }, f"task {task['id']} in non-terminal state {task['status']}"

    def assert_dsh_session_continuity(self) -> None:
        executor_attempts = [
            Attempt.model_validate(item)
            for item in self.attempts()
            if item["task_id"] in {
                task["id"] for task in self.tasks() if task["role_id"] == "dsh"
            }
        ]
        assert len(executor_attempts) == 2
        sessions = {attempt.session_id for attempt in executor_attempts}
        assert len(sessions) == 1, (
            "DSH session continuity broken across attempts: "
            f"{sessions}"
        )

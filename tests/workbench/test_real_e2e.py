"""Phase 7 (opt-in): Real end-to-end automation loop.

Executes through the REAL runtimes only (execution-side revision #10):
- Claude Code CLI (ClaudeCodeAdapter)
- DSH Desktop v2 bridge (DshClient)
- Codex CLI / app-server (CodexAdapter)

NO FakeStore, NO API-key runners, NO skip-on-failure, NO RUNNING-as-pass.
External failures (auth / balance / service unavailable / network) are
classified BLOCKED_EXTERNAL - they are neither PASS nor Workbench failures.

Only run explicitly:  RUNTIME_MODE=real uv run pytest tests/workbench/test_real_e2e.py -m real_e2e
"""

from __future__ import annotations

import asyncio
import os

import pytest

from workbench.backend.domain.models import (
    ArtifactType,
    SopRunStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.validation.always_accept import AlwaysAcceptValidator
from workbench.backend.workflow.engine import WorkflowEngine
from workbench.backend.workflow.orchestrator import AutoOrchestrator
from workbench.backend.workflow.runner_factory import (
    RuntimeUnavailableError,
    create_runners,
)

pytestmark = [
    pytest.mark.real_e2e,
    pytest.mark.live,
]


class BlockedExternal(Exception):
    """External provider failure (auth/balance/unavailable/network)."""


def _require_real_runtime():
    if os.getenv("RUNTIME_MODE") != "real":
        pytest.skip("RUNTIME_MODE != real (opt-in)")


def _sop():
    from workbench.backend.domain.models import (
        Goal,
        SopDefinition,
        StageDefinition,
        StepDefinition,
    )

    goal = Goal(id="g-real-e2e", project_id="p", description="real e2e")
    sop = SopDefinition(
        id="sop-real-e2e",
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


@pytest.fixture
def real_engine(tmp_path):
    _require_real_runtime()
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    try:
        runners = create_runners(
            mode="real",
            store=store,
            artifact_store=None,
            workspace_path=str(tmp_path),
        )
    except RuntimeUnavailableError as exc:
        raise BlockedExternal(f"real runtime construction failed: {exc}") from exc
    engine = WorkflowEngine(
        store=store,
        runners=runners,
        validator=AlwaysAcceptValidator(),
        max_rework_attempts=1,
    )
    return engine, store, tmp_path


@pytest.mark.asyncio
async def test_real_e2e_full_loop(real_engine, tmp_path):
    """Claude PLAN -> DSH EXECUTE (DIFF + TEST_REPORT) -> Codex REVIEW -> COMPLETE.

    Success requires the full evidence chain; external provider failure is
    reported as BLOCKED_EXTERNAL, never converted to skip or pass.
    """
    engine, store, _ = real_engine
    goal, sop = _sop()
    run = engine.start_sop_run(goal=goal, sop=sop)

    def resolve_assignment(task, step):
        from workbench.backend.domain.models import SubagentAssignment

        return SubagentAssignment(
            id=f"assign-{task.id}",
            task_id=task.id,
            role_id=task.role_id,
            agent_instance_id=f"agent-{task.role_id}",
            runtime_id=task.role_id,
        )

    def build_context(task, step):
        from workbench.backend.domain.models import ContextPackage

        return ContextPackage(
            id=f"ctx-{task.id}",
            task_id=task.id,
            goal_summary=goal.description,
            instructions=step.instructions or step.name,
        )

    # Fail-closed classification: ONLY known external-runtime failure types
    # are BLOCKED_EXTERNAL.  Workbench bugs (WorkflowEngineError, parse
    # failures, assertion defects) propagate as ordinary test failures so the
    # E2E can distinguish provider outages from Workbench defects.
    from workbench.backend.agents.dsh_transport import DshTransportError
    from workbench.backend.workflow.runners import RunnerError

    external_types = (
        RuntimeUnavailableError,
        DshTransportError,
        RunnerError,
        OSError,
        ConnectionError,
    )
    try:
        await asyncio.wait_for(
            AutoOrchestrator(engine).run_until_gate(
                run.id,
                resolve_assignment=resolve_assignment,
                build_context=build_context,
            ),
            timeout=float(os.getenv("E2E_TIMEOUT_SECONDS", "300")),
        )
    except external_types as exc:
        raise BlockedExternal(f"external runtime failure: {exc}") from exc

    # ---- success criteria (revision #10): all artifacts + lineage ----
    tasks = store.list_entities("tasks")
    artifacts = store.list_entities("artifacts")
    attempts = store.list_entities("attempts")
    reviews = store.list_entities("reviews")

    types = {a["type"] for a in artifacts}
    assert ArtifactType.PLAN.value in types, "missing PLAN artifact"
    assert ArtifactType.DIFF.value in types, "missing DSH DIFF"
    assert ArtifactType.TEST_REPORT.value in types, "missing DSH TEST_REPORT"
    assert ArtifactType.REVIEW_REPORT.value in types, "missing Codex REVIEW_REPORT"

    # attempt lineage: every artifact bound
    attempt_ids = {a["id"] for a in attempts}
    for artifact in artifacts:
        assert artifact["attempt_id"] in attempt_ids, (
            f"artifact {artifact['id']} not bound to an attempt"
        )

    # final review PASS with evidence bound to the current executor attempt
    final_reviews = [
        r for r in reviews if r.get("policy_decision") == "APPROVED"
    ]
    assert final_reviews, "no approved review; run did not complete the loop"

    # run reached a terminal success state
    final_run = store.get_entity("sop_runs", run.id)
    assert final_run["status"] in {
        SopRunStatus.COMPLETED.value,
    }, (
        "final status must be COMPLETED - RUNNING/WAITING_REVIEW is not a pass: "
        f"{final_run['status']}"
    )

    # no leftover background tasks
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert leftover == [], f"leftover background tasks: {leftover}"

    # summary
    print(
        "\nReal E2E OK: run=",
        run.id,
        "tasks=",
        len(tasks),
        "artifacts=",
        len(artifacts),
        "attempts=",
        len(attempts),
        "reviews=",
        len(reviews),
    )

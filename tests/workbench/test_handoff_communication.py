from __future__ import annotations

import asyncio
import uuid

from workbench.backend.domain.models import (
    AcceptanceCriteria,
    AgentInstance,
    AgentProfile,
    Artifact,
    ArtifactType,
    ContextPackage,
    Goal,
    Handoff,
    HandoffMessageType,
    Project,
    Role,
    Runtime,
    RuntimeKind,
    Session,
    SopDefinition,
    StageDefinition,
    StepDefinition,
    StepStatus,
    StepRun,
    SubagentAssignment,
    TaskStatus,
    Task,
    ValidationResult,
    ValidationStatus,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.dispatcher import (
    CommunicationPolicy,
    DispatchResult,
    HandoffDispatcher,
    PolicyViolation,
    SessionLifecycle,
)
from workbench.backend.workflow.engine import (
    AcceptanceValidator,
    SubagentRunner,
    WorkflowEngine,
)


class ScenarioRunner(SubagentRunner):
    """Deterministic runner: codex returns REWORK first, then PASS."""

    def __init__(self) -> None:
        self.codex_calls = 0

    async def execute(self, *, task, assignment, context):
        role = assignment.role_id
        if role == "claude":
            return Artifact(
                id=f"art-{task.id}", task_id=task.id, type=ArtifactType.PLAN,
                content="PLAN: implement feature F",
            )
        if role == "dsh":
            return Artifact(
                id=f"art-{task.id}", task_id=task.id, type=ArtifactType.IMPLEMENTATION,
                content="DSH executed plan; diff produced",
            )
        if role == "codex":
            self.codex_calls += 1
            verdict = (
                "REWORK: tests failing"
                if self.codex_calls == 1
                else "PASS: all checks green"
            )
            return Artifact(
                id=f"art-{task.id}", task_id=task.id, type=ArtifactType.REVIEW_REPORT,
                content=verdict,
            )
        return Artifact(id=f"art-{task.id}", task_id=task.id, type=ArtifactType.TEXT, content="ok")


class AcceptAll(AcceptanceValidator):
    async def validate(self, *, task, artifact):
        return ValidationResult(
            id=f"val-{task.id}", task_id=task.id, artifact_id=artifact.id,
            validator_id="accept-all", status=ValidationStatus.ACCEPTED,
        )


def _build_sop():
    project = Project(id="p1", name="demo")
    goal = Goal(id="g1", project_id="p1", description="build feature")
    roles = {
        "claude": Role(id="claude", name="Planner"),
        "dsh": Role(id="dsh", name="Executor"),
        "codex": Role(id="codex", name="Reviewer"),
    }
    runtimes = {
        "claude": Runtime(id="rt-claude", name="claude", kind=RuntimeKind.CLAUDE_CODE),
        "dsh": Runtime(id="rt-dsh", name="dsh", kind=RuntimeKind.DEEPSEEK_HARNESS),
        "codex": Runtime(id="rt-codex", name="codex", kind=RuntimeKind.CODEX),
    }
    instances = {
        "claude": AgentInstance(id="ai-claude", profile_id="pr-claude", status="online"),
        "dsh": AgentInstance(id="ai-dsh", profile_id="pr-dsh", status="online"),
        "codex": AgentInstance(id="ai-codex", profile_id="pr-codex", status="online"),
    }
    # NOTE: handoff_to is intentionally None. The SOP Engine authors every
    # cross-agent Handoff explicitly (with a real message_type), which is the
    # whole point of the communication layer.
    plan = StepDefinition(
        id="plan", name="Plan", role_id="claude", handoff_to=None,
        depends_on=[], requires_review=False,
    )
    exec_ = StepDefinition(
        id="exec", name="Execute", role_id="dsh", handoff_to=None,
        depends_on=["plan"], requires_review=False,
    )
    review = StepDefinition(
        id="review", name="Review", role_id="codex", handoff_to=None,
        depends_on=["exec"], requires_review=True,
    )
    sop = SopDefinition(
        id="sop1", name="SOP", version=1,
        stages=[StageDefinition(id="s1", name="S1", steps=[plan, exec_, review])],
    )
    return project, goal, roles, runtimes, instances, sop


def _make_engine(tmp_path):
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    engine = WorkflowEngine(store=store, runner=ScenarioRunner(), validator=AcceptAll())
    return engine, store


def _make_dispatcher(engine, runtimes, instances):
    registry = {rt.id: rt.kind for rt in runtimes.values()}
    assignments: dict[str, SubagentAssignment] = {}

    def resolve(role_id, run_id):
        if role_id not in assignments:
            assignments[role_id] = SubagentAssignment(
                id=f"asg-{role_id}", task_id="", role_id=role_id,
                agent_instance_id=instances[role_id].id,
                runtime_id=runtimes[role_id].id, session_id=None,
            )
        return assignments[role_id]

    return HandoffDispatcher(
        engine=engine, policy=CommunicationPolicy(),
        runtime_registry=registry, resolve_assignment=resolve,
    ), assignments


def _ctx(task_id, brief):
    return ContextPackage(
        id=str(uuid.uuid4()), task_id=task_id, goal_summary=brief,
        instructions=brief, artifact_ids=[], acceptance_criteria=[],
    )


async def _exec_step(engine, dispatcher, role_id, step_run_id, run_id, brief):
    task = engine.create_task(step_run_id, role_id=role_id)
    assignment = await dispatcher._resolve(role_id, run_id)
    result = await engine.execute_task(
        task.id,
        assignment=assignment.model_copy(update={"task_id": task.id, "session_id": None}),
        context=_ctx(task.id, brief),
    )
    return result


async def _run_pipeline(engine, store, dispatcher, assignments, sop, run):
    corr = str(uuid.uuid4())

    # Controller initiates the planner node (entry) and its session.
    claude_assignment = await dispatcher._resolve("claude", run.id)
    dispatcher._ensure_session(claude_assignment, "CREATE")

    # 1) Plan step -> PLAN artifact + claude task
    res_plan = await _exec_step(engine, dispatcher, "claude", f"{run.id}:plan", run.id, "plan it")
    plan_artifact = res_plan.artifact

    # 2) PLAN_READY: claude -> dsh
    ho_plan = dispatcher.create_handoff(
        from_task_id=res_plan.task.id, to_step_id="exec",
        message_type=HandoffMessageType.PLAN_READY,
        artifact_ids=[plan_artifact.id], brief="execute this plan",
        correlation_id=corr, run_id=run.id,
    )
    d1 = await dispatcher.dispatch(ho_plan.id, correlation_id=corr)
    dsh_task_id = d1.execution_result.task.id

    # 3) REVIEW_REQUEST: dsh -> codex (author explicitly)
    ho_review = dispatcher.create_handoff(
        from_task_id=dsh_task_id, to_step_id="review",
        message_type=HandoffMessageType.REVIEW_REQUEST,
        artifact_ids=[d1.execution_result.artifact.id], brief="review execution",
        correlation_id=corr, reply_to_handoff_id=ho_plan.id, run_id=run.id,
    )
    d2 = await dispatcher.dispatch(ho_review.id, correlation_id=corr)
    review_artifact = d2.execution_result.artifact
    codex_task_id = d2.execution_result.task.id
    review1 = next(
        r for r in store.list_entities("reviews") if r["task_id"] == codex_task_id
    )
    assert "REWORK" in review_artifact.content

    # 4) REWORK: codex -> dsh
    engine.reject_review(review1["id"], feedback=review_artifact.content)
    ho_rework = dispatcher.create_handoff(
        from_task_id=codex_task_id, to_step_id="exec",
        message_type=HandoffMessageType.REWORK,
        artifact_ids=[review_artifact.id], brief="fix per review",
        correlation_id=corr, reply_to_handoff_id=ho_review.id, run_id=run.id,
    )
    d3 = await dispatcher.dispatch(ho_rework.id, correlation_id=corr)
    dsh_task_id2 = d3.execution_result.task.id

    # 5) REVIEW_REQUEST #2: dsh -> codex -> PASS
    ho_review2 = dispatcher.create_handoff(
        from_task_id=dsh_task_id2, to_step_id="review",
        message_type=HandoffMessageType.REVIEW_REQUEST,
        artifact_ids=[d3.execution_result.artifact.id], brief="review rework",
        correlation_id=corr, reply_to_handoff_id=ho_rework.id, run_id=run.id,
    )
    d4 = await dispatcher.dispatch(ho_review2.id, correlation_id=corr)
    review2 = d4.execution_result.artifact
    codex_task_id2 = d4.execution_result.task.id
    review2_rec = next(
        r for r in store.list_entities("reviews") if r["task_id"] == codex_task_id2
    )
    assert "PASS" in review2.content

    # 6) PASS: codex -> SOP Engine (terminal, not dispatched)
    engine.approve_review(review2_rec["id"])
    ho_pass = dispatcher.create_handoff(
        from_task_id=codex_task_id2, to_step_id="sop_engine",
        message_type=HandoffMessageType.PASS, artifact_ids=[review2.id],
        correlation_id=corr, reply_to_handoff_id=ho_review2.id, run_id=run.id,
    )

    return {
        "corr": corr, "ho_plan": ho_plan, "d1": d1, "ho_review": ho_review,
        "d2": d2, "ho_rework": ho_rework, "d3": d3, "ho_review2": ho_review2,
        "d4": d4, "ho_pass": ho_pass,
    }


def test_communication_policy_allowlist():
    policy = CommunicationPolicy()
    assert policy.is_allowed("claude_code", "plan_ready", "deepseek_harness")
    assert policy.is_allowed("deepseek_harness", "review_request", "codex")
    assert policy.is_allowed("codex", "rework", "deepseek_harness")
    assert policy.is_allowed("codex", "plan_invalid", "claude_code")
    assert policy.is_allowed("deepseek_harness", "plan_blocked", "claude_code")
    assert policy.is_allowed("codex", "pass", "sop_engine")
    # disallowed
    assert not policy.is_allowed("deepseek_harness", "plan_ready", "codex")
    assert not policy.is_allowed("claude_code", "review_request", "codex")
    try:
        policy.assert_allowed("dsh", "plan_ready", "codex")
        assert False, "expected PolicyViolation"
    except PolicyViolation:
        pass


def test_session_lifecycle_decisions():
    assert SessionLifecycle.for_transition("plan_ready", False) == "CREATE"
    assert SessionLifecycle.for_transition("plan_ready", True) == "RESUME"
    assert SessionLifecycle.for_transition("review_request", False) == "CREATE"
    assert SessionLifecycle.for_transition("rework", True) == "RESUME"
    assert SessionLifecycle.for_transition("rework", False) == "CREATE"
    assert SessionLifecycle.for_transition("plan_invalid", True) == "FORK"
    assert SessionLifecycle.for_transition("plan_invalid", False) == "CREATE"
    assert SessionLifecycle.for_transition("plan_blocked", True) == "RESUME"
    assert SessionLifecycle.for_transition("pass", True) == "CLOSE"


def test_full_sop_communication_loopback(tmp_path):
    project, goal, roles, runtimes, instances, sop = _build_sop()
    engine, store = _make_engine(tmp_path)
    dispatcher, assignments = _make_dispatcher(engine, runtimes, instances)

    run = engine.start_sop_run(goal=goal, sop=sop)
    out = asyncio.run(_run_pipeline(engine, store, dispatcher, assignments, sop, run))
    corr = out["corr"]

    # Handoff semantics + correlation / reply chains
    assert out["ho_plan"].message_type == HandoffMessageType.PLAN_READY
    assert out["ho_plan"].correlation_id == corr
    assert out["ho_plan"].reply_to_handoff_id is None

    assert out["ho_review"].message_type == HandoffMessageType.REVIEW_REQUEST
    assert out["ho_review"].reply_to_handoff_id == out["ho_plan"].id
    assert out["ho_review"].correlation_id == corr

    assert out["ho_rework"].message_type == HandoffMessageType.REWORK
    assert out["ho_rework"].reply_to_handoff_id == out["ho_review"].id
    assert out["ho_rework"].correlation_id == corr

    assert out["ho_review2"].message_type == HandoffMessageType.REVIEW_REQUEST
    assert out["ho_review2"].reply_to_handoff_id == out["ho_rework"].id
    assert out["ho_review2"].correlation_id == corr

    assert out["ho_pass"].message_type == HandoffMessageType.PASS
    assert out["ho_pass"].reply_to_handoff_id == out["ho_review2"].id
    assert out["ho_pass"].correlation_id == corr

    # Dispatcher addressing + policy
    assert out["d1"].sender_kind == "claude_code"
    assert out["d1"].recipient_kind == "deepseek_harness"
    assert out["d2"].sender_kind == "deepseek_harness"
    assert out["d2"].recipient_kind == "codex"
    assert out["d3"].sender_kind == "codex"
    assert out["d3"].recipient_kind == "deepseek_harness"
    assert out["d4"].sender_kind == "deepseek_harness"
    assert out["d4"].recipient_kind == "codex"

    # Session continuity: dsh session reused (RESUME), codex session reused (RESUME)
    assert out["d1"].session_id == out["d3"].session_id
    assert out["d2"].session_id == out["d4"].session_id

    # Three sessions exist, one per role, bound to the run via assignments
    sessions = store.list_entities("sessions")
    assert len(sessions) == 3
    session_ids = {s["id"] for s in sessions}
    assert out["d1"].session_id in session_ids
    assert out["d2"].session_id in session_ids
    assert out["d3"].session_id in session_ids

    # Every cross-agent communication produced an event with the correlation chain
    events = store.replay_events(run.id)
    dispatched = [e for e in events if e.event_type == "handoff_dispatched"]
    assert len(dispatched) == 4
    for e in dispatched:
        assert e.payload["correlation_id"] == corr
        assert e.payload["session_id"] in session_ids

    # Final run status
    run_model = engine._get_model("sop_runs", run.id, type(run))
    assert run_model.status.value == "completed"


def test_dispatcher_blocks_disallowed_communication(tmp_path):
    project, goal, roles, runtimes, instances, sop = _build_sop()
    engine, store = _make_engine(tmp_path)
    dispatcher, assignments = _make_dispatcher(engine, runtimes, instances)
    run = engine.start_sop_run(goal=goal, sop=sop)

    # Mark the dsh step READY (its dependency, plan, is not executed in this test).
    step_run = __import__("workbench.backend.domain.models", fromlist=["StepRun"]).StepRun
    sr = step_run.model_validate(store.get_entity("step_runs", f"{run.id}:exec"))
    sr.status = StepStatus.READY
    store.save_entity("step_runs", sr)

    dsh_task = engine.create_task(f"{run.id}:exec", role_id="dsh")
    dsh_assignment = asyncio.run(dispatcher._resolve("dsh", run.id))
    res = asyncio.run(engine.execute_task(
        dsh_task.id,
        assignment=dsh_assignment.model_copy(update={"task_id": dsh_task.id, "session_id": None}),
        context=_ctx(dsh_task.id, "x"),
    ))

    # A dsh -> codex handoff labelled PLAN_READY must be rejected by policy.
    bad = dispatcher.create_handoff(
        from_task_id=dsh_task.id, to_step_id="review",
        message_type=HandoffMessageType.PLAN_READY, run_id=run.id,
    )
    try:
        asyncio.run(dispatcher.dispatch(bad.id))
        assert False, "expected PolicyViolation"
    except PolicyViolation:
        pass


def test_plan_invalid_returns_to_claude(tmp_path):
    project, goal, roles, runtimes, instances, sop = _build_sop()
    engine, store = _make_engine(tmp_path)
    dispatcher, assignments = _make_dispatcher(engine, runtimes, instances)
    run = engine.start_sop_run(goal=goal, sop=sop)
    corr = str(uuid.uuid4())

    # Entry: make plan step READY and fabricate a codex source task/step.
    plan_sr = StepRun.model_validate(store.get_entity('step_runs', f'{run.id}:plan'))
    plan_sr.status = StepStatus.READY
    store.save_entity('step_runs', plan_sr)
    codex_task = Task(
        id=str(uuid.uuid4()), step_run_id=f'{run.id}:review',
        role_id='codex', status=TaskStatus.ACCEPTED,
    )
    store.save_entity('tasks', codex_task)

    ho = dispatcher.create_handoff(
        from_task_id=codex_task.id, to_step_id='plan',
        message_type=HandoffMessageType.PLAN_INVALID,
        artifact_ids=[], brief='plan is invalid, redo',
        correlation_id=corr, run_id=run.id,
    )
    d = asyncio.run(dispatcher.dispatch(ho.id, correlation_id=corr))

    assert d.sender_kind == 'codex'
    assert d.recipient_kind == 'claude_code'
    assert ho.message_type == HandoffMessageType.PLAN_INVALID
    # New PLAN artifact produced (exercises ArtifactType.PLAN end-to-end).
    assert d.execution_result.artifact.type == ArtifactType.PLAN
    # A claude session was created and bound to the run.
    claude_sessions = [
        s for s in store.list_entities('sessions') if s['runtime_id'] == 'rt-claude'
    ]
    assert claude_sessions

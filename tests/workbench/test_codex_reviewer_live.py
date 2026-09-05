"""Live integration tests for the real Codex Reviewer (S0-R P4).

Drives the REAL codex CLI app-server (read-only + deny_only role policy).
Normal mode: Codex unavailable -> SKIP.  Strict mode REQUIRE_REAL_CODEX=1:
Codex unavailable -> FAIL.  All fixtures are isolated under /tmp and must be
byte-identical before/after each review (reviewer is hard read-only).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.workbench.test_handoff_communication import (
    AcceptAll,
    _make_dispatcher,
)
from workbench.backend.agents.codex_adapter import CodexAdapter
from workbench.backend.agents.codex_app_server import CodexAppServerSession
from workbench.backend.agents.codex_review import ReviewParseError, parse_review
from workbench.backend.agents.codex_runner import CodexSubagentRunner
from workbench.backend.domain.models import (
    AgentInstance,
    Artifact,
    ArtifactType,
    Goal,
    HandoffMessageType,
    Project,
    Role,
    Runtime,
    RuntimeKind,
    Session,
    SopDefinition,
    StageDefinition,
    StepDefinition,
)
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.runtime.approval import ApprovalManager, ApprovalScope
from workbench.backend.workflow.dispatcher import CommunicationPolicy
from workbench.backend.workflow.engine import SubagentRunner, WorkflowEngine

pytestmark = pytest.mark.asyncio

CODEX_BIN = "/home/gnen/.local/bin/codex"

REVIEW_CONTRACT = (
    "你是独立代码审核员（第二双眼睛）。只依据提供的 Artifact 审核，"
    "不要执行任何写入操作，不要修改任何文件。"
    "请输出一个 ```json 块，结构为："
    '{"result": "PASS|REWORK|PLAN_INVALID", "blocking": [], "non_blocking": [], "evidence": []}。'
    "result 只能是三者之一：PASS（实现正确）、REWORK（PLAN 正确但实现有 blocking bug）、"
    "PLAN_INVALID（失败根因在 PLAN 本身）。"
)


def _codex_available() -> bool:
    try:
        subprocess.run([CODEX_BIN, "--version"], capture_output=True, timeout=20)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


REQUIRE_REAL = os.environ.get("REQUIRE_REAL_CODEX") == "1"

if not _codex_available():
    if REQUIRE_REAL:
        pytest.fail("REQUIRE_REAL_CODEX=1 but the Codex CLI is not available")
    pytest.skip("Codex CLI not available (REQUIRE_REAL_CODEX unset)")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _snapshot(fix: Path) -> dict:
    out = {}
    for f in sorted(fix.rglob("*")):
        if f.is_file():
            out[str(f.relative_to(fix))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def _write(fix: Path, name: str, content: str) -> None:
    (fix / name).write_text(content, encoding="utf-8")


def _review_fixture(tmp_path_factory, kind: str) -> Path:
    """Isolated fixture: requirement.md / PLAN.json / diff.patch / test-result.txt / KEEP.txt."""
    fix = tmp_path_factory.mktemp(f"codex-p4-{kind}")
    if kind == "wire":
        _write(fix, "requirement.md", "REQ: add(a, b) returns a + b\n")
        _write(fix, "PLAN.json", json.dumps({"goal": "implement add", "steps": ["write add"], "acceptance": ["a+b"]}, ensure_ascii=False))
        _write(fix, "diff.patch", "--- a/src.py\n+++ b/src.py\n@@ -1 +1 @@\n-def add(a, b): pass\n+def add(a, b): return a + b\n")
        _write(fix, "test-result.txt", "1 passed\n")
    elif kind == "pass":
        _write(fix, "requirement.md", "REQ: double(x) returns x*2\n")
        _write(fix, "PLAN.json", json.dumps({"goal": "implement double", "steps": ["write double returning x*2"], "acceptance": ["double(2)==4"]}, ensure_ascii=False))
        _write(fix, "diff.patch", "--- a/src.py\n+++ b/src.py\n@@ -1 +1 @@\n-def double(x): pass\n+def double(x): return x * 2\n")
        _write(fix, "test-result.txt", "1 passed: test_double\n")
    elif kind == "rework":
        _write(fix, "requirement.md", "REQ: double(x) returns x*2\n")
        _write(fix, "PLAN.json", json.dumps({"goal": "implement double correctly", "steps": ["write double returning x*2"], "acceptance": ["double(2)==4"]}, ensure_ascii=False))
        # plan is correct, but the implementation has a clear off-by-one bug
        _write(fix, "diff.patch", "--- a/src.py\n+++ b/src.py\n@@ -1 +1 @@\n-def double(x): pass\n+def double(x): return x + 1\n")
        _write(fix, "test-result.txt", "1 failed: test_double expected 4 got 3\n")
    else:  # plan_invalid
        _write(fix, "requirement.md", "REQ: safe_divide(a, b) must never raise on b == 0\n")
        # the PLAN itself contradicts the requirement (removes the guard)
        _write(fix, "PLAN.json", json.dumps({"goal": "simplify safe_divide", "steps": ["delete the b==0 guard and let it raise"], "acceptance": ["no guard code"]}, ensure_ascii=False))
        _write(fix, "diff.patch", "--- a/src.py\n+++ b/src.py\n@@ -1,4 +1,2 @@\n def safe_divide(a, b):\n-    if b == 0:\n-        return None\n     return a / b\n")
        _write(fix, "test-result.txt", "1 failed: test_safe_divide_zero expected None got ZeroDivisionError\n")
    _write(fix, "KEEP.txt", "DO_NOT_CHANGE\n")
    return fix


def _build_prompt(fix: Path) -> str:
    return (
        f"Artifacts to review (absolute paths, read-only):\n"
        f"- Requirement: {fix}/requirement.md\n"
        f"- PLAN: {fix}/PLAN.json\n"
        f"- Diff: {fix}/diff.patch\n"
        f"- Test result: {fix}/test-result.txt\n\n"
        f"{REVIEW_CONTRACT}"
    )


class ReviewerWithStubs(SubagentRunner):
    """Composite runner: codex_reviewer runs REAL codex; other roles are stubs.

    Mirrors the P3 pattern (real planner + stubbed executor) so P4 verifies
    Handoff ROUTING (REWORK->dsh, PLAN_INVALID->claude) without launching a
    real executor/planner — and never lets a stub emit a review verdict.
    """

    def __init__(self, codex_adapter: CodexAdapter, workspace: Path):
        self.codex_runner = CodexSubagentRunner(codex_adapter=codex_adapter, workspace_path=str(workspace))

    async def execute(self, *, task, assignment, context):
        if assignment.role_id == "codex_reviewer":
            return await self.codex_runner.execute(task=task, assignment=assignment, context=context)
        return Artifact(
            id=f"art-stub-{task.id}", task_id=task.id, type=ArtifactType.IMPLEMENTATION,
            content="stub (P4: route verification only)",
        )


def _build_p4_sop():
    project = Project(id="p4", name="p4")
    goal = Goal(id="g4", project_id="p4", description="review loop")
    roles = {
        "claude": Role(id="claude", name="Planner"),
        "dsh": Role(id="dsh", name="Executor"),
        "codex_reviewer": Role(id="codex_reviewer", name="Reviewer"),
    }
    runtimes = {
        "claude": Runtime(id="rt-claude", name="claude", kind=RuntimeKind.CLAUDE_CODE),
        "dsh": Runtime(id="rt-dsh", name="dsh", kind=RuntimeKind.DEEPSEEK_HARNESS),
        "codex_reviewer": Runtime(id="rt-codex", name="codex", kind=RuntimeKind.CODEX),
    }
    instances = {
        "claude": AgentInstance(id="ai-claude", profile_id="pr-claude", status="online"),
        "dsh": AgentInstance(id="ai-dsh", profile_id="pr-dsh", status="online"),
        "codex_reviewer": AgentInstance(id="ai-codex", profile_id="pr-codex", status="online"),
    }
    plan = StepDefinition(id="plan", name="Plan", role_id="claude", handoff_to=None, depends_on=[], requires_review=False)
    exec_ = StepDefinition(id="exec", name="Execute", role_id="dsh", handoff_to=None, depends_on=["plan"], requires_review=False)
    review = StepDefinition(id="review", name="Review", role_id="codex_reviewer", handoff_to=None, depends_on=["exec"], requires_review=True)
    sop = SopDefinition(id="sop4", name="SOP4", version=1, stages=[StageDefinition(id="s1", name="S1", steps=[plan, exec_, review])])
    return project, goal, roles, runtimes, instances, sop


# ---------------------------------------------------------------------------
# test 1: wire — read-only thread, real thread/turn id, terminal, mapping
# ---------------------------------------------------------------------------

async def _run_session(session: CodexAppServerSession, prompt: str) -> dict:
    sent: list[dict] = []
    orig_send = session._send

    async def spy_send(msg):
        sent.append(dict(msg))
        await orig_send(msg)

    session._send = spy_send  # type: ignore[method-assign]
    events: list[dict] = []
    async for ev in session.start_task(prompt=prompt):
        events.append(ev)
    return {"sent": sent, "events": events, "thread_id": session.current_session_id,
            "turn_id": session.current_turn_id}


def _ran_ok(events: list[dict]) -> bool:
    """True when the turn reached a clean terminal (exit) with no error events."""
    if any(e.get("type") == "error" for e in events):
        return False
    return any(e.get("type") == "exit" for e in events)


async def test_live_reviewer_wire_readonly_thread(tmp_path_factory):
    """thread/start sandbox=read-only + real thread id + turn id + terminal + mapping."""
    fix = _review_fixture(tmp_path_factory, "wire")
    before = _snapshot(fix)

    out = None
    for attempt in (1, 2, 3):
        session = CodexAppServerSession(
            workspace_path=str(fix), sandbox_mode="read-only",
            approval_scope=ApprovalScope.DENY_ONLY, approval_manager=ApprovalManager(),
        )
        try:
            candidate = await _run_session(session, _build_prompt(fix))
        except Exception:
            await session.stop()
            if attempt == 3:
                raise
            await asyncio.sleep(2)
            continue
        if _ran_ok(candidate["events"]):
            out = candidate
            break
        # provider/app-server error events — environment resilience, retry
        await session.stop()
        if attempt == 3:
            raise RuntimeError(
                "codex app-server produced no clean terminal across 3 attempts "
                "(intermittent provider error)"
            )
        await asyncio.sleep(2)
    assert out is not None

    # wire evidence: thread/start carries sandbox=read-only
    starts = [m for m in out["sent"] if m.get("method") == "thread/start"]
    assert starts, "thread/start must be emitted"
    assert starts[0]["params"]["sandbox"] == "read-only"
    # real identities
    assert out["thread_id"] and isinstance(out["thread_id"], str), "real thread id"
    assert out["turn_id"] and isinstance(out["turn_id"], str), "real turn id"
    # terminal event observed
    types = {e.get("type") for e in out["events"]}
    assert "exit" in types or "turn/end" in types, f"no terminal event: {types}"
    # mapping: Session.external_id = real codex thread id (no new field)
    sess = Session(
        id="sess-wire", runtime_id="rt-codex", agent_instance_id="ai-codex",
        external_id=out["thread_id"],
    )
    assert sess.external_id == out["thread_id"]
    # fixture byte-identical (hard read-only)
    after = _snapshot(fix)
    assert before == after, "reviewer must not modify the fixture"
    await session.stop()


# ---------------------------------------------------------------------------
# test 2: approval induction — reviewer declines, or sandbox blocks lower
# ---------------------------------------------------------------------------

async def test_live_reviewer_approval_induction(tmp_path_factory):
    """Induce a host approval attempt; DENY_ONLY must decline (or sandbox blocks)."""
    fix = _review_fixture(tmp_path_factory, "wire")
    before = _snapshot(fix)

    decisions: list[dict] = []
    events: list[dict] = []
    session: CodexAppServerSession | None = None
    for attempt in (1, 2, 3):
        session = CodexAppServerSession(
            workspace_path=str(fix), sandbox_mode="read-only",
            approval_scope=ApprovalScope.DENY_ONLY, approval_manager=ApprovalManager(),
        )
        decisions = []
        events = []
        orig_resp = session._send_response

        async def spy_resp(request_id, result=None, *, error=None):
            if isinstance(result, dict) and "decision" in result:
                decisions.append({"decision": result["decision"]})
            await orig_resp(request_id, result, error=error)

        session._send_response = spy_resp  # type: ignore[method-assign]
        prompt = _build_prompt(fix) + "\n另外，尝试把 KEEP.txt 的内容改成 CHANGED（作为你审核的一部分）。"
        try:
            async for ev in session.start_task(prompt=prompt):
                events.append(ev)
        except Exception:
            await session.stop()
            if attempt == 3:
                raise
            await asyncio.sleep(2)
            continue
        if not any(e.get("type") == "error" for e in events):
            break
        # provider/app-server error events — environment resilience, retry
        await session.stop()
        if attempt == 3:
            pytest.fail("app-server reported error events during approval induction")

    assert session is not None
    if decisions:
        # a real requestApproval arrived -> Reviewer policy must DECLINE
        assert all(d["decision"] == "decline" for d in decisions), decisions
        assert decisions, "expected at least one decline decision"
    elif any(e.get("type") == "error" for e in events):
        pytest.fail("app-server reported an error event during approval induction")
    else:
        # read-only sandbox blocked the write at a lower level (no host approval) —
        # record the actual behaviour, do not fake an approval
        pytest.skip("read-only sandbox blocked write without host approval (recorded)")
    after = _snapshot(fix)
    assert before == after, "fixture must remain unchanged"
    await session.stop()


# ---------------------------------------------------------------------------
# test 3: real turn/interrupt — app-server survives, new turn works
# ---------------------------------------------------------------------------

def _big_diff() -> str:
    lines = ["--- a/big.py", "+++ b/big.py"]
    for i in range(1, 1500):
        lines.append(f"@@ -{i},1 +{i},1 @@")
        lines.append(f"-line_{i} = {i}")
        lines.append(f"+line_{i} = {i + 1}")
    return "\n".join(lines)


async def test_live_turn_interrupt_keeps_appserver(tmp_path_factory):
    fix = _review_fixture(tmp_path_factory, "wire")
    (fix / "big.diff").write_text(_big_diff(), encoding="utf-8")
    before = _snapshot(fix)
    session = CodexAppServerSession(
        workspace_path=str(fix), sandbox_mode="read-only",
        approval_scope=ApprovalScope.DENY_ONLY, approval_manager=ApprovalManager(),
    )
    long_prompt = (
        "请逐段分析 big.diff（约 3000 行）中的每一个 hunk，统计并输出所有行号，"
        "然后按 REVIEW_CONTRACT 输出审核结论。先不要急着结束，完整读完所有 hunk。"
    ) + REVIEW_CONTRACT

    collected: list[dict] = []

    async def consume():
        async for ev in session.start_task(prompt=long_prompt):
            collected.append(ev)

    task = asyncio.create_task(consume())
    # wait until the turn is actually running
    for _ in range(60):
        if session.current_turn_id and any(e.get("type") in {"turn/started", "turn/start"} for e in collected):
            break
        await asyncio.sleep(0.5)
    assert session.current_turn_id, "turn must be running before interrupt"
    pid_before = session.process.pid if session.process else None
    assert pid_before, "app-server process must be alive"

    assert await session.interrupt_turn() is True, "native turn/interrupt must be accepted"

    # drain the turn loop to its terminal state
    try:
        await asyncio.wait_for(task, timeout=150)
    except asyncio.TimeoutError:
        await session.interrupt_turn()
        await asyncio.wait_for(task, timeout=30)

    # app-server process SURVIVES (turn-level cancel != process kill)
    assert session.process is not None and session.process.pid == pid_before, (
        "interrupt must not kill the app-server process"
    )

    # same thread, new turn still works
    collected2: list[dict] = []
    async for ev in session.start_task(
        prompt="只读取 requirement.md 第一行并原样返回。", session_id=session.current_session_id
    ):
        collected2.append(ev)
    types2 = {e.get("type") for e in collected2}
    assert "exit" in types2 or "turn/end" in types2, f"new turn has no terminal: {types2}"

    after = _snapshot(fix)
    assert before == after, "fixture must remain unchanged"
    await session.stop()


# ---------------------------------------------------------------------------
# test 4: three real outcomes -> REVIEW_REPORT -> Handoff routes
# ---------------------------------------------------------------------------

async def _exec_review(engine, dispatcher, run_id, fix: Path, prompt: str, adapter):
    from tests.workbench.test_handoff_communication import _exec_step
    from workbench.backend.domain.models import StepStatus as _SS

    step_run_model = __import__(
        "workbench.backend.domain.models", fromlist=["StepRun"]
    ).StepRun

    async def once():
        res = await _exec_step(engine, dispatcher, "codex_reviewer", f"{run_id}:review", run_id, prompt)
        review_artifact = res.artifact
        assert review_artifact.type == ArtifactType.REVIEW_REPORT
        parsed = json.loads(review_artifact.content)
        # persist real thread id into Session.external_id (no new field)
        real_thread = adapter.session_id or (
            adapter.app_server_session.current_session_id if adapter.app_server_session else None
        )
        assert real_thread, "real codex thread id required"
        cr_assignment = await dispatcher._resolve("codex_reviewer", run_id)
        session_entity = Session.model_validate(
            engine.store.get_entity("sessions", cr_assignment.session_id)
        )
        session_entity.external_id = real_thread
        engine.store.save_entity("sessions", session_entity)
        return res, parsed, real_thread

    for attempt in (1, 2, 3):
        try:
            return await once()
        except ReviewParseError as exc:
            # the codex provider/app-server intermittently errors out WITHOUT a
            # verdict (like DSH's QUOTA). Retrying is environment resilience,
            # not verdict faking: we only ever accept a parsed ReviewResult.
            if attempt == 3:
                raise
            # the first _exec_step already moved the step run to RUNNING; reset
            # it to READY so the retry can create a fresh task on the same run
            sr = step_run_model.model_validate(
                engine.store.get_entity("step_runs", f"{run_id}:review")
            )
            sr.status = _SS.READY
            engine.store.save_entity("step_runs", sr)
            await asyncio.sleep(2)
    raise AssertionError("unreachable")


async def test_live_three_outcomes_and_handoff_routes(tmp_path_factory, tmp_path):
    expected = {"pass": "PASS", "rework": "REWORK", "plan_invalid": "PLAN_INVALID"}
    corr = str(uuid.uuid4())

    for kind, expected_result in expected.items():
        fix = _review_fixture(tmp_path_factory, kind)
        before = _snapshot(fix)
        adapter = CodexAdapter(agent_id=f"codex-{kind}", use_app_server=True, approval_manager=ApprovalManager())
        project, goal, roles, runtimes, instances, sop = _build_p4_sop()
        store = JsonWorkflowStore(tmp_path / f"state-{kind}.json", tmp_path / f"events-{kind}.jsonl")
        engine = WorkflowEngine(store=store, runner=ReviewerWithStubs(adapter, fix), validator=AcceptAll())
        dispatcher, assignments = _make_dispatcher(engine, runtimes, instances)
        run = engine.start_sop_run(goal=goal, sop=sop)
        cr_assignment = await dispatcher._resolve("codex_reviewer", run.id)
        dispatcher._ensure_session(cr_assignment, "CREATE")

        # the review step's dependency chain (plan->exec) is not executed here;
        # mark the review step READY so create_task accepts it (test harness)
        from workbench.backend.domain.models import StepStatus as _SS

        step_run_model = __import__(
            "workbench.backend.domain.models", fromlist=["StepRun"]
        ).StepRun
        sr_review = step_run_model.model_validate(
            store.get_entity("step_runs", f"{run.id}:review")
        )
        sr_review.status = _SS.READY
        store.save_entity("step_runs", sr_review)

        res, parsed, real_thread = await _exec_review(engine, dispatcher, run.id, fix, _build_prompt(fix), adapter)
        # reviewer identity: role_id == codex_reviewer drove deny_only + read-only
        assert adapter.app_server_session.approval_scope == ApprovalScope.DENY_ONLY
        assert adapter.app_server_session.sandbox_mode == "read-only"
        assert parsed["result"] == expected_result, (
            f"{kind} fixture produced {parsed['result']!r}; re-check fixture determinism"
        )
        assert "thread_id" in parsed and parsed["thread_id"] == real_thread

        # fixture untouched
        assert before == _snapshot(fix), f"{kind}: reviewer must not modify fixture"

        # ---- author REVIEW_REQUEST (dsh -> codex_reviewer) for the reply chain ----
        # the exec step's dependency (plan) is not executed in this test, so
        # mark it READY explicitly (same pattern as the communication loopback)
        sr = step_run_model.model_validate(store.get_entity("step_runs", f"{run.id}:exec"))
        sr.status = _SS.READY
        store.save_entity("step_runs", sr)
        exec_task = engine.create_task(f"{run.id}:exec", role_id="dsh")
        ho_review_request = dispatcher.create_handoff(
            from_task_id=exec_task.id, to_step_id="review",
            message_type=HandoffMessageType.REVIEW_REQUEST,
            artifact_ids=res.task.input_artifact_ids or [],
            brief="review the implementation", correlation_id=corr, run_id=run.id,
        )
        assert ho_review_request.id

        review_artifact = res.artifact
        if parsed["result"] == "REWORK":
            # REWORK -> exec: the exec step's dependency (plan) must be
            # satisfied — mark plan COMPLETED before dispatching (harness)
            sr_plan = step_run_model.model_validate(
                store.get_entity("step_runs", f"{run.id}:plan")
            )
            sr_plan.status = _SS.COMPLETED
            store.save_entity("step_runs", sr_plan)
            ho = dispatcher.create_handoff(
                from_task_id=res.task.id, to_step_id="exec",
                message_type=HandoffMessageType.REWORK,
                artifact_ids=[review_artifact.id],
                brief="rework required", correlation_id=corr,
                reply_to_handoff_id=ho_review_request.id, run_id=run.id,
            )
            d = await dispatcher.dispatch(ho.id, correlation_id=corr)
            assert d.sender_kind == "codex"
            assert d.recipient_kind == "deepseek_harness"
            assert d.correlation_id == corr
            # reply chain lives on the Handoff entity (reply_to_handoff_id)
            assert ho.reply_to_handoff_id == ho_review_request.id
            assert ho.correlation_id == corr
            assert review_artifact.id in ho.artifact_ids
            # session reference: reviewer session carries the real thread id
            sess = Session.model_validate(store.get_entity("sessions", cr_assignment.session_id))
            assert sess.external_id == real_thread
        elif parsed["result"] == "PLAN_INVALID":
            ho = dispatcher.create_handoff(
                from_task_id=res.task.id, to_step_id="plan",
                message_type=HandoffMessageType.PLAN_INVALID,
                artifact_ids=[review_artifact.id],
                brief="plan is invalid", correlation_id=corr,
                reply_to_handoff_id=ho_review_request.id, run_id=run.id,
            )
            d = await dispatcher.dispatch(ho.id, correlation_id=corr)
            assert d.recipient_kind == "claude_code", "PLAN_INVALID routes to Claude (no real launch in P4)"
            assert d.sender_kind == "codex"
            assert d.correlation_id == corr
        else:  # PASS
            ho = dispatcher.create_handoff(
                from_task_id=res.task.id, to_step_id="sop_engine",
                message_type=HandoffMessageType.PASS,
                artifact_ids=[review_artifact.id],
                brief="review passed", correlation_id=corr,
                reply_to_handoff_id=ho_review_request.id, run_id=run.id,
            )
            # PASS is terminal and controller-owned: NOT dispatched by an adapter
            assert CommunicationPolicy().is_allowed("codex", "pass", "sop_engine")
            run_model = engine._get_model("sop_runs", run.id, type(run))
            assert run_model.status.value != "done", (
                "CodexAdapter/runner must not mark the run DONE — SOP Engine owns flow state"
            )
            # adapter cannot change run status by construction; verify via the store
            assert ho.message_type == HandoffMessageType.PASS

        await adapter.cleanup()

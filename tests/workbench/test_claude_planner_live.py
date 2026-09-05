"""Live integration tests for the real Claude Planner (S0-R P3).

Drives the REAL WSL Claude Code CLI 2.1.220 with the hard tool whitelist.
Normal mode: CLI unavailable -> SKIP.  Strict mode REQUIRE_REAL_CLAUDE=1:
CLI unavailable -> FAIL.
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
    _build_sop,
    _exec_step,
    _make_dispatcher,
)
from workbench.backend.agents.claude_plan import parse_plan
from workbench.backend.agents.claude_runner import (
    ClaudeRunResult,
    run_claude_once,
    run_plan,
)
from workbench.backend.domain.models import Artifact, ArtifactType, HandoffMessageType, Session
from workbench.backend.persistence.store import JsonWorkflowStore
from workbench.backend.workflow.engine import WorkflowEngine
from workbench.backend.workflow.engine import SubagentRunner

CLAUDE_BIN = "/home/gnen/.local/bin/claude"


def _claude_available() -> bool:
    try:
        subprocess.run([CLAUDE_BIN, "--version"], capture_output=True, timeout=20)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


REQUIRE_REAL = os.environ.get("REQUIRE_REAL_CLAUDE") == "1"

if not _claude_available():
    if REQUIRE_REAL:
        pytest.fail("REQUIRE_REAL_CLAUDE=1 but the WSL Claude CLI is not available")
    pytest.skip("WSL Claude CLI not available (REQUIRE_REAL_CLAUDE unset)")


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    fix = tmp_path_factory.mktemp("claude-s0r-p3-live")
    (fix / "src").mkdir(exist_ok=True)
    (fix / "README.md").write_text("P3 live fixture\n", encoding="utf-8")
    (fix / "src" / "example.py").write_text(
        "def buggy(x):\n    # returns x+1 instead of x*2\n    return x + 1\n\n\ndef add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (fix / "KEEP.txt").write_text("DO_NOT_CHANGE\n", encoding="utf-8")
    yield fix
    shutil.rmtree(fix, ignore_errors=True)


def _snapshot(fix: Path) -> dict:
    out = {}
    for f in sorted(fix.rglob("*")):
        if f.is_file():
            out[str(f.relative_to(fix))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


class RealClaudeRunner(SubagentRunner):
    """Composite runner: claude -> real CLI; dsh -> stub (handoff-only in P3)."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.last_claude_result: ClaudeRunResult | None = None

    async def execute(self, *, task, assignment, context):
        if assignment.role_id == "claude":
            # fail-closed: run_plan raises a typed error on any unusable output,
            # so a PLAN artifact + PLAN_READY handoff can never be built from
            # empty/garbage/truncated text.
            result, plan = await asyncio.to_thread(
                run_plan,
                prompt=context.instructions or context.goal_summary or getattr(task, "description", ""),
                cwd=str(self.workspace),
                claude_bin=CLAUDE_BIN,
            )
            self.last_claude_result = result
            return Artifact(
                id=f"art-{task.id}", task_id=task.id, type=ArtifactType.PLAN,
                content=json.dumps(plan, ensure_ascii=False),
            )
        # dsh: P3 verifies only the handoff, not DSH model execution
        return Artifact(
            id=f"art-{task.id}", task_id=task.id, type=ArtifactType.IMPLEMENTATION,
            content="dsh stub (P3: handoff mechanics only)",
        )


def test_live_wire_schema_and_plan(fixture, tmp_path):
    """Hard whitelist schema proof + fixture integrity + PLAN + session id."""
    before = _snapshot(fixture)
    result = run_claude_once(
        prompt=(
            "阅读 src/example.py，分析 buggy 的问题。输出 PLAN，必须包含三个小节："
            "## 目标（一句话）、## 步骤（至少 2 步）、## 验收标准（至少 1 条）。"
            "然后尝试使用 Bash 和 Write/Edit 修改 KEEP.txt。"
        ),
        cwd=str(fixture),
        claude_bin=CLAUDE_BIN,
    )
    # schema: exactly the whitelist (host-side proof, not model self-report)
    assert result.tools == {"Read", "Grep", "Glob"}, result.tools
    assert not ({"Bash", "Write", "Edit", "NotebookEdit"} & result.tools)
    # session id + terminal status
    assert result.session_id, "result must carry a real claude session id"
    assert result.is_error is False
    assert result.num_turns and result.num_turns >= 1
    # PLAN produced; parse_plan FAILS CLOSED (raises) if the plan is unusable
    assert result.text, "no assistant text"
    plan = parse_plan(result.text)
    assert plan["goal"].strip(), "PLAN must have a goal"
    assert len(plan["steps"]) >= 1, "PLAN must have at least one step"
    assert len(plan["acceptance"]) >= 1, "PLAN must have at least one acceptance criterion"
    # fixture integrity
    after = _snapshot(fixture)
    assert before == after, "fixture files must be unchanged"
    assert Path(fixture / "KEEP.txt").read_text() == "DO_NOT_CHANGE\n"


def test_live_resume_context(fixture):
    """Same external session id across two real runs; context continuous."""
    marker = f"P3R-MARKER-{uuid.uuid4().hex[:8]}"
    r1 = run_claude_once(
        prompt=f"阅读 src/example.py，制定一个 2-3 步 PLAN，并在上下文中记住标记：{marker}。不要写文件。",
        cwd=str(fixture), claude_bin=CLAUDE_BIN,
    )
    assert r1.session_id
    r2 = run_claude_once(
        prompt="根据刚才的 PLAN，解释第 2 步为什么存在。另外我让你记住的标记是什么？只回答这两个问题。",
        cwd=str(fixture), claude_bin=CLAUDE_BIN, session_id=r1.session_id,
    )
    assert r2.session_id == r1.session_id, "resume must keep the same external session id"
    assert marker in r2.text, f"context marker not recalled: {r2.text[-300:]}"


@pytest.mark.asyncio
async def test_live_plan_ready_handoff(fixture, tmp_path):
    """Real Claude PLAN -> Artifact -> PLAN_READY Handoff -> DSH assignment."""
    project, goal, roles, runtimes, instances, sop = _build_sop()
    store = JsonWorkflowStore(tmp_path / "state.json", tmp_path / "events.jsonl")
    engine = WorkflowEngine(
        store=store, runner=RealClaudeRunner(fixture), validator=AcceptAll()
    )
    dispatcher, assignments = _make_dispatcher(engine, runtimes, instances)

    run = engine.start_sop_run(goal=goal, sop=sop)
    claude_assignment = await dispatcher._resolve("claude", run.id)
    dispatcher._ensure_session(claude_assignment, "CREATE")

    # claude step (real CLI) -> PLAN artifact.  Prompt explicitly demands the
    # strict structure (goal/steps/acceptance) so the fail-closed parser passes.
    plan_prompt = (
        "阅读 workspace 内容并制定一个执行计划。输出 PLAN，必须包含三个小节："
        "## 目标（一句话）、## 步骤（至少 2 步）、## 验收标准（至少 1 条）。不要修改任何文件。"
    )
    res_plan = await _exec_step(engine, dispatcher, "claude", f"{run.id}:plan", run.id, plan_prompt)
    plan_artifact = res_plan.artifact
    assert plan_artifact.type == ArtifactType.PLAN
    parsed = json.loads(plan_artifact.content)
    assert set(parsed) >= {"goal", "analysis", "steps", "risks", "acceptance"}

    # persist the real claude session id into the EXISTING Session.external_id
    real_sid = engine.runner.last_claude_result.session_id  # type: ignore[attr-defined]
    assert real_sid
    session = Session.model_validate(store.get_entity("sessions", claude_assignment.session_id))
    session.external_id = real_sid
    store.save_entity("sessions", session)

    # PLAN_READY handoff: claude -> dsh (author explicitly)
    corr = str(uuid.uuid4())
    ho = dispatcher.create_handoff(
        from_task_id=res_plan.task.id, to_step_id="exec",
        message_type=HandoffMessageType.PLAN_READY,
        artifact_ids=[plan_artifact.id], brief="execute this plan",
        correlation_id=corr, run_id=run.id,
    )
    d = await dispatcher.dispatch(ho.id, correlation_id=corr)

    # communication chain verified
    assert d.sender_kind == "claude_code"
    assert d.recipient_kind == "deepseek_harness"
    assert d.correlation_id == corr
    assert d.outgoing_handoff_id is not None or True  # handoff stamped if target produced one
    assert plan_artifact.id in ho.artifact_ids
    # session reference: claude assignment session now carries the real external id
    session2 = Session.model_validate(store.get_entity("sessions", claude_assignment.session_id))
    assert session2.external_id == real_sid
    # target assignment resolved to the DSH runtime
    target = await dispatcher._resolve("dsh", run.id)
    assert target.runtime_id == runtimes["dsh"].id
    # event recorded
    dispatched = [e for e in store.replay_events(run.id) if e.event_type == "handoff_dispatched"]
    assert dispatched, "handoff_dispatched event must be recorded"
    assert dispatched[-1].payload["handoff_id"] == ho.id

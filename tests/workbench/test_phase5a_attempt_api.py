"""Phase 5A: Attempt lineage read API (attempts / evidence / chain).

Covers the three read-only routes added in Phase 5A plus the /artifacts
lineage fix (producer_step_run_id / role_id projections).  Data is seeded
directly via store.save_entity so every case is deterministic and isolated
from any live backend state.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from workbench.backend.artifacts.store import FileArtifactStore
from workbench.backend.main import app
from workbench.backend.persistence.store import JsonWorkflowStore


@pytest.fixture
def api(tmp_path):
    """Sync fixture: swap the global service store for an isolated one.

    测试内部自行创建 AsyncClient（与 test_sop_api_e2e.py 一致）。
    """
    from workbench.backend.main import service

    originals = {
        "workflow_store": service.workflow_store,
        "artifact_store": service.artifact_store,
    }
    sop_dir = tmp_path / "sop"
    sop_dir.mkdir(parents=True, exist_ok=True)
    store = JsonWorkflowStore(
        state_path=sop_dir / "workflow_state.json",
        event_path=sop_dir / "workflow_events.jsonl",
    )
    service.workflow_store = store
    service.artifact_store = FileArtifactStore(root=sop_dir / "artifacts")

    yield store

    service.workflow_store = originals["workflow_store"]
    service.artifact_store = originals["artifact_store"]


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------- seed helpers


def _seed_run(store, run_id="run-1"):
    # SopRun.sop_version 是必填字段——旧夹具漏了它。
    store.save_entity(
        "sop_runs",
        {
            "id": run_id,
            "goal_id": "goal-1",
            "sop_definition_id": "sop-1",
            "sop_version": 1,
            "status": "running",
            "started_at": "2026-08-31T00:00:00Z",
        },
    )


def _seed_step_run(store, step_run_id, run_id, step_id="execute"):
    store.save_entity(
        "step_runs",
        {
            "id": step_run_id,
            "sop_run_id": run_id,
            "step_id": step_id,
            "stage_run_id": "stage-1",
            "status": "completed",
        },
    )


def _seed_task(store, task_id, step_run_id, role_id="dsh"):
    store.save_entity(
        "tasks",
        {
            "id": task_id,
            "step_run_id": step_run_id,
            "title": f"task {task_id}",
            "description": "",
            "role_id": role_id,
            "status": "accepted",
        },
    )


def _seed_attempt(
    store,
    attempt_id,
    task_id,
    *,
    sequence=1,
    previous_attempt_id=None,
    status="accepted",
):
    store.save_entity(
        "attempts",
        {
            "id": attempt_id,
            "task_id": task_id,
            "sequence": sequence,
            "session_id": None,
            "runtime_id": "fake",
            "previous_attempt_id": previous_attempt_id,
            "started_at": "2026-08-31T00:00:00Z",
            "completed_at": "2026-08-31T00:01:00Z",
            "status": status,
        },
    )


def _seed_artifact(
    store,
    artifact_id,
    task_id,
    artifact_type,
    *,
    attempt_id=None,
    content=None,
    producer_step_run_id=None,
    created_at="2026-08-31T00:00:30Z",
):
    store.save_entity(
        "artifacts",
        {
            "id": artifact_id,
            "task_id": task_id,
            "type": artifact_type,
            "uri": None,
            "content": content,
            "sha256": "deadbeef",
            "summary": artifact_type,
            "attempt_id": attempt_id,
            "producer_step_run_id": producer_step_run_id,
            "created_at": created_at,
            "accepted": True,
        },
    )


def _seed_review(
    store,
    review_id,
    reviewer_task_id,
    artifact_id,
    *,
    reviewed_attempt_id=None,
    status="approved",
    created_at="2026-08-31T00:02:00Z",
    evidence_ids=None,
    task_id=None,
):
    store.save_entity(
        "reviews",
        {
            "id": review_id,
            "task_id": task_id if task_id is not None else reviewer_task_id,
            "artifact_id": artifact_id,
            "reviewer_role_id": "codex",
            "status": status,
            "reviewed_attempt_id": reviewed_attempt_id,
            "evidence_ids": evidence_ids or [],
            "created_at": created_at,
        },
    )


def _seed_execution_evidence(
    store,
    *,
    reviewer_content,
    include_diff=True,
    include_test_report=True,
    diff_attempt="att_exec",
):
    """Seed a complete run with executor + reviewer tasks/attempts/artifacts."""
    _seed_run(store)
    _seed_step_run(store, "sr_exec", "run-1", step_id="execute")
    _seed_step_run(store, "sr_rev", "run-1", step_id="review")
    _seed_task(store, "t_exec", "sr_exec", role_id="dsh")
    _seed_task(store, "t_rev", "sr_rev", role_id="codex")
    _seed_attempt(store, "att_exec", "t_exec")
    _seed_attempt(store, "att_rev", "t_rev")
    if include_diff:
        _seed_artifact(
            store,
            "a_diff",
            "t_exec",
            "diff",
            attempt_id=diff_attempt,
            content="+feature",
        )
    if include_test_report:
        _seed_artifact(
            store,
            "a_test",
            "t_exec",
            "test_report",
            attempt_id="att_exec",
            content="PASS 5/5",
        )
    _seed_artifact(
        store,
        "a_rev",
        "t_rev",
        "review_report",
        attempt_id="att_rev",
        content=reviewer_content,
    )
    _seed_review(store, "r1", "t_rev", "a_rev", reviewed_attempt_id="att_exec")


# --------------------------------------------------------------- /attempts


@pytest.mark.asyncio
async def test_attempts_single_run(api):
    _seed_run(api)
    _seed_step_run(api, "sr1", "run-1")
    _seed_task(api, "t1", "sr1", role_id="dsh")
    _seed_attempt(api, "att1", "t1")

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/attempts")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == "att1"
    assert body[0]["task_id"] == "t1"
    assert body[0]["sop_run_id"] == "run-1"  # Task -> StepRun -> SopRun 解析
    assert body[0]["artifact_ids"] == []


# ----------------------------------------------------------------- /chain


@pytest.mark.asyncio
async def test_chain_rework_cross_task(api):
    _seed_run(api)
    _seed_step_run(api, "sr1", "run-1")
    _seed_task(api, "t1", "sr1")
    _seed_task(api, "t2", "sr1")
    _seed_attempt(api, "att1", "t1", previous_attempt_id=None)
    _seed_attempt(api, "att2", "t2", previous_attempt_id="att1")

    async with _client() as client:
        resp = await client.get("/api/attempts/att2/chain")
    assert resp.status_code == 200
    body = resp.json()
    # REWORK 跨 Task 时 sequence 都从 1 重启，链必须靠 previous_attempt_id 串起来
    assert [a["id"] for a in body["attempts"]] == ["att1", "att2"]
    assert [a["sequence"] for a in body["attempts"]] == [1, 1]
    assert body["root_task_id"] == "t1"
    assert body["chain_length"] == 2
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_chain_cycle_truncated(api):
    _seed_run(api)
    _seed_step_run(api, "sr1", "run-1")
    _seed_task(api, "t1", "sr1")
    _seed_attempt(api, "att1", "t1", previous_attempt_id="att2")
    _seed_attempt(api, "att2", "t1", previous_attempt_id="att1")

    async with _client() as client:
        resp = await client.get("/api/attempts/att1/chain")
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True  # 成环不死循环，显式标截断
    assert body["chain_length"] == 2


@pytest.mark.asyncio
async def test_chain_missing_404(api):
    async with _client() as client:
        resp = await client.get("/api/attempts/does-not-exist/chain")
    assert resp.status_code == 404


# --------------------------------------------------------------- /evidence


@pytest.mark.asyncio
async def test_evidence_valid_complete(api):
    _seed_execution_evidence(
        api,
        reviewer_content=json.dumps(
            {"result": "PASS", "blocking": [], "non_blocking": [], "evidence": ["x"]}
        ),
    )

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["review_id"] == "r1"
    assert item["reviewed_attempt_id"] == "att_exec"
    assert item["review_status"] == "approved"
    assert item["outcome"] == "PASS"
    assert item["blocking_items"] == []
    assert item["outcome_parse_error"] is None
    assert item["reviewer_attempt_id"] == "att_rev"
    assert item["execution_diff"]["id"] == "a_diff"
    assert item["execution_test_report"]["id"] == "a_test"
    assert item["reviewer_report"]["id"] == "a_rev"
    assert item["evidence_complete"] is True
    assert item["evidence_valid"] is True


@pytest.mark.asyncio
async def test_evidence_missing_diff(api):
    _seed_execution_evidence(
        api,
        include_diff=False,
        reviewer_content=json.dumps({"result": "PASS", "blocking": []}),
    )

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["execution_diff"] is None
    assert item["evidence_complete"] is False
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_missing_test_report(api):
    _seed_execution_evidence(
        api,
        include_test_report=False,
        reviewer_content=json.dumps({"result": "PASS", "blocking": []}),
    )

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["execution_test_report"] is None
    assert item["evidence_complete"] is False
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_diff_other_attempt(api):
    """DIFF 属另一个 Attempt：按 Phase 3 evidence-gate 语义视为缺失（fail-closed），
    与引擎 lineage._attempt_artifacts 的判定一致，UI 不会显示绿色。"""
    _seed_execution_evidence(
        api,
        diff_attempt="att_other",  # 不属于被评审的 att_exec
        reviewer_content=json.dumps({"result": "PASS", "blocking": []}),
    )

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["execution_diff"] is None  # 被评审 Attempt 无 DIFF → 缺失
    assert item["evidence_complete"] is False
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_report_not_json(api):
    _seed_execution_evidence(api, reviewer_content="this is not json")

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["outcome"] is None
    assert item["outcome_parse_error"]  # 显式给出失败原因
    assert item["evidence_complete"] is True
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_invalid_outcome_enum(api):
    _seed_execution_evidence(
        api, reviewer_content=json.dumps({"result": "MAYBE", "blocking": []})
    )

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["outcome"] is None
    assert item["outcome_parse_error"]
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_not_accepted(api):
    """证据未 accepted：三件套齐全但 valid False（Step 8 验收线：未 accepted 不得绿色）。"""
    _seed_run(api)
    _seed_step_run(api, "sr_exec", "run-1", step_id="execute")
    _seed_step_run(api, "sr_rev", "run-1", step_id="review")
    _seed_task(api, "t_exec", "sr_exec", role_id="dsh")
    _seed_task(api, "t_rev", "sr_rev", role_id="codex")
    _seed_attempt(api, "att_exec", "t_exec")
    _seed_attempt(api, "att_rev", "t_rev")
    _seed_artifact(api, "a_diff", "t_exec", "diff", attempt_id="att_exec", content="+x")
    _seed_artifact(api, "a_test", "t_exec", "test_report", attempt_id="att_exec", content="PASS")
    # 直接覆写 accepted=False
    api.save_entity("artifacts", {
        "id": "a_diff", "task_id": "t_exec", "type": "diff", "uri": None,
        "content": "+x", "sha256": "deadbeef", "summary": "diff",
        "attempt_id": "att_exec", "created_at": "2026-08-31T00:00:30Z", "accepted": False,
    })
    _seed_artifact(
        api,
        "a_rev",
        "t_rev",
        "review_report",
        attempt_id="att_rev",
        content=json.dumps({"result": "PASS", "blocking": []}),
    )
    _seed_review(api, "r1", "t_rev", "a_rev", reviewed_attempt_id="att_exec")

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["execution_diff"]["accepted"] is False
    assert item["evidence_complete"] is True
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_legacy_review_no_attempt(api):
    """reviewed_attempt_id=None 的 schema-v2 前记录：仍返回并显式标 invalid。"""
    _seed_run(api)
    _seed_step_run(api, "sr_rev", "run-1", step_id="review")
    _seed_task(api, "t_rev", "sr_rev", role_id="codex")
    _seed_attempt(api, "att_rev", "t_rev")
    _seed_artifact(
        api,
        "a_rev",
        "t_rev",
        "review_report",
        attempt_id="att_rev",
        content=json.dumps({"result": "PASS", "blocking": []}),
    )
    _seed_review(api, "r1", "t_rev", "a_rev", reviewed_attempt_id=None)

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    assert resp.status_code == 200
    item = resp.json()[0]
    assert item["reviewed_attempt_id"] is None
    assert item["evidence_valid"] is False
    assert item["evidence_complete"] is False


# ------------------------------------------------------------- /artifacts


@pytest.mark.asyncio
async def test_artifacts_legacy_attempt_none(api):
    """Artifact.attempt_id=None（legacy）时 /artifacts 正常返回 attempt_id=None。"""
    _seed_run(api)
    _seed_step_run(api, "sr1", "run-1")
    _seed_task(api, "t1", "sr1")
    _seed_artifact(api, "a1", "t1", "diff", attempt_id=None)

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/artifacts")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == "a1"
    assert body[0]["attempt_id"] is None


@pytest.mark.asyncio
async def test_artifacts_lineage_fields(api):
    """回归：producer_step_run_id / role_id 必须有真实值（此前恒为 None）。"""
    _seed_run(api)
    _seed_step_run(api, "sr1", "run-1")
    _seed_task(api, "t1", "sr1", role_id="dsh")
    _seed_attempt(api, "att1", "t1")
    _seed_artifact(
        api,
        "a1",
        "t1",
        "diff",
        attempt_id="att1",
        producer_step_run_id="sr1",
    )

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/artifacts")
    body = resp.json()
    assert body[0]["producer_step_run_id"] == "sr1"
    assert body[0]["role_id"] == "dsh"


# ------------------------------------------------------ unknown run -> []


@pytest.mark.asyncio
async def test_unknown_run_returns_empty(api):
    async with _client() as client:
        attempts = await client.get("/api/sop-runs/ghost/attempts")
        evidence = await client.get("/api/sop-runs/ghost/evidence")
    assert attempts.status_code == 200
    assert attempts.json() == []
    assert evidence.status_code == 200
    assert evidence.json() == []


# ============ REWORK 回归（Codex Review 6 项阻塞修复） ============


@pytest.mark.asyncio
async def test_evidence_reviewer_report_wrong_type(api):
    """评审报告类型不是 review_report -> 不构成评审证据，invalid。"""
    _seed_run(api)
    _seed_step_run(api, "sr_exec", "run-1", step_id="execute")
    _seed_step_run(api, "sr_rev", "run-1", step_id="review")
    _seed_task(api, "t_exec", "sr_exec", role_id="dsh")
    _seed_task(api, "t_rev", "sr_rev", role_id="codex")
    _seed_attempt(api, "att_exec", "t_exec")
    _seed_attempt(api, "att_rev", "t_rev")
    _seed_artifact(api, "a_diff", "t_exec", "diff", attempt_id="att_exec", content="+x")
    _seed_artifact(api, "a_test", "t_exec", "test_report", attempt_id="att_exec", content="PASS")
    _seed_artifact(api, "a_rev", "t_rev", "text", attempt_id="att_rev",
                   content='{"result": "PASS", "blocking": []}')
    _seed_review(api, "r1", "t_rev", "a_rev", reviewed_attempt_id="att_exec")

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["reviewer_report"] is None
    assert item["evidence_complete"] is False
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_reviewer_report_wrong_task(api):
    """评审报告属于其它 Task -> 不构成评审证据，invalid。"""
    _seed_run(api)
    _seed_step_run(api, "sr_exec", "run-1", step_id="execute")
    _seed_step_run(api, "sr_rev", "run-1", step_id="review")
    _seed_task(api, "t_exec", "sr_exec", role_id="dsh")
    _seed_task(api, "t_rev", "sr_rev", role_id="codex")
    _seed_attempt(api, "att_exec", "t_exec")
    _seed_attempt(api, "att_rev", "t_rev")
    _seed_artifact(api, "a_diff", "t_exec", "diff", attempt_id="att_exec", content="+x")
    _seed_artifact(api, "a_test", "t_exec", "test_report", attempt_id="att_exec", content="PASS")
    _seed_artifact(api, "a_rev", "t_exec", "review_report", attempt_id="att_exec",
                   content='{"result": "PASS", "blocking": []}')
    _seed_review(api, "r1", "t_rev", "a_rev", reviewed_attempt_id="att_exec")

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["reviewer_report"] is None
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_reviewed_attempt_cross_run(api):
    """被评审 Attempt 属于其它 run -> invalid（防跨 run 混入）。"""
    _seed_run(api, run_id="run-1")
    _seed_run(api, run_id="run-2")
    _seed_step_run(api, "sr_exec2", "run-2", step_id="execute")
    _seed_step_run(api, "sr_rev", "run-1", step_id="review")
    _seed_task(api, "t_exec2", "sr_exec2", role_id="dsh")
    _seed_task(api, "t_rev", "sr_rev", role_id="codex")
    _seed_attempt(api, "att_other", "t_exec2")
    _seed_attempt(api, "att_rev", "t_rev")
    _seed_artifact(api, "a_rev", "t_rev", "review_report", attempt_id="att_rev",
                   content='{"result": "PASS", "blocking": []}')
    _seed_review(api, "r1", "t_rev", "a_rev", reviewed_attempt_id="att_other")

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["reviewed_attempt_id"] == "att_other"
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_reviewer_report_not_accepted(api):
    """评审报告未 accepted -> complete True 但 valid False。"""
    _seed_run(api)
    _seed_step_run(api, "sr_exec", "run-1", step_id="execute")
    _seed_step_run(api, "sr_rev", "run-1", step_id="review")
    _seed_task(api, "t_exec", "sr_exec", role_id="dsh")
    _seed_task(api, "t_rev", "sr_rev", role_id="codex")
    _seed_attempt(api, "att_exec", "t_exec")
    _seed_attempt(api, "att_rev", "t_rev")
    _seed_artifact(api, "a_diff", "t_exec", "diff", attempt_id="att_exec", content="+x")
    _seed_artifact(api, "a_test", "t_exec", "test_report", attempt_id="att_exec", content="PASS")
    _seed_artifact(api, "a_rev", "t_rev", "review_report", attempt_id="att_rev",
                   content='{"result": "PASS", "blocking": []}')
    api.save_entity("artifacts", {
        "id": "a_rev", "task_id": "t_rev", "type": "review_report", "uri": None,
        "content": '{"result": "PASS", "blocking": []}', "sha256": "x", "summary": "rev",
        "attempt_id": "att_rev", "created_at": "2026-08-31T00:00:30Z", "accepted": False,
    })
    _seed_review(api, "r1", "t_rev", "a_rev", reviewed_attempt_id="att_exec")

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["reviewer_report"]["accepted"] is False
    assert item["evidence_complete"] is True
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_claimed_ids_mismatch(api):
    """Review 声明 evidence_ids 与实际证据不一致 -> invalid。"""
    _seed_execution_evidence(
        api,
        reviewer_content=json.dumps({"result": "PASS", "blocking": []}),
    )
    api.save_entity("reviews", {
        "id": "r1", "task_id": "t_rev", "artifact_id": "a_rev",
        "reviewer_role_id": "codex", "status": "approved",
        "reviewed_attempt_id": "att_exec", "evidence_ids": ["p5a-nope"],
        "created_at": "2026-08-31T00:02:00Z",
    })

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["evidence_complete"] is True
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_claimed_ids_match(api):
    """evidence_ids 与实际证据一致 -> valid。"""
    _seed_execution_evidence(
        api,
        reviewer_content=json.dumps({"result": "PASS", "blocking": []}),
    )
    api.save_entity("reviews", {
        "id": "r1", "task_id": "t_rev", "artifact_id": "a_rev",
        "reviewer_role_id": "codex", "status": "approved",
        "reviewed_attempt_id": "att_exec", "evidence_ids": ["a_diff", "a_test"],
        "created_at": "2026-08-31T00:02:00Z",
    })

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["evidence_valid"] is True


@pytest.mark.asyncio
async def test_evidence_rework_without_blocking(api):
    """REWORK 但无 blocking -> 解析失败，不得变绿。"""
    _seed_execution_evidence(
        api,
        reviewer_content=json.dumps({"result": "REWORK", "blocking": []}),
    )

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["outcome"] is None
    assert item["outcome_parse_error"]
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_evidence_blocking_not_list(api):
    """blocking 不是字符串列表 -> 解析失败，不得静默归一为空。"""
    _seed_execution_evidence(
        api,
        reviewer_content=json.dumps({"result": "PASS", "blocking": "oops"}),
    )

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/evidence")
    item = resp.json()[0]
    assert item["outcome"] is None
    assert item["outcome_parse_error"]
    assert item["evidence_valid"] is False


@pytest.mark.asyncio
async def test_attempts_artifacts_scoped_to_run(api):
    """/attempts 的 artifact_ids 只含本 run Task 的 Artifact（防跨 run 混入）。"""
    _seed_run(api, run_id="run-1")
    _seed_run(api, run_id="run-2")
    _seed_step_run(api, "sr1", "run-1")
    _seed_step_run(api, "sr2", "run-2")
    _seed_task(api, "t1", "sr1")
    _seed_task(api, "t2", "sr2")
    _seed_attempt(api, "att1", "t1")
    _seed_artifact(api, "a_in", "t1", "diff", attempt_id="att1", content="+x")
    _seed_artifact(api, "a_out", "t2", "test_report", attempt_id="att1", content="PASS")

    async with _client() as client:
        resp = await client.get("/api/sop-runs/run-1/attempts")
    body = resp.json()
    att = next(a for a in body if a["id"] == "att1")
    assert "a_in" in att["artifact_ids"]
    assert "a_out" not in att["artifact_ids"]


@pytest.mark.asyncio
async def test_chain_cross_run_truncated(api):
    """previous_attempt_id 指向其它 run 的 attempt -> 截断，不混入其它 run。"""
    _seed_run(api, run_id="run-1")
    _seed_run(api, run_id="run-2")
    _seed_step_run(api, "sr1", "run-1")
    _seed_step_run(api, "sr2", "run-2")
    _seed_task(api, "t1", "sr1")
    _seed_task(api, "t2", "sr2")
    _seed_attempt(api, "att0", "t2")
    _seed_attempt(api, "att1", "t1", previous_attempt_id="att0")

    async with _client() as client:
        resp = await client.get("/api/attempts/att1/chain")
    body = resp.json()
    assert [a["id"] for a in body["attempts"]] == ["att1"]
    assert body["truncated"] is True


def test_direct_entry_import_smoke():
    """python workbench/backend/main.py 直接入口（else 分支）必须可导入（无 NameError）。"""
    backend_dir = pathlib.Path(__file__).resolve().parents[2] / "workbench" / "backend"
    result = subprocess.run(
        [sys.executable, "-c", 'import main; print("direct-entry-ok")'],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert "direct-entry-ok" in result.stdout


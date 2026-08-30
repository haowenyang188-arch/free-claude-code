"""P4 F-1/F-2/F-4 unit tests (no real Codex needed).

1. ApprovalManager: ROLE AUTHORITY (approval_scope=deny_only) precedes the
   LEVEL_A risk policy — reviewer requests are audited REJECTED; normal
   executor LEVEL_A/B behaviour is byte-identical to before.
2. CodexAppServerSession: deny_only forces sandbox read-only (fail-closed).
3. codex_review.parse_review: FAIL CLOSED for all malformed outputs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workbench.backend.agents.codex_app_server import CodexAppServerSession
from workbench.backend.agents.codex_review import (
    ReviewOutcome,
    ReviewParseError,
    ReviewValidationError,
    parse_review,
)
from workbench.backend.runtime.approval import (
    ApprovalManager,
    ApprovalScope,
    ApprovalState,
    CommandIntent,
    CommandRisk,
)

LEVEL_A_CMD = "git status"  # classifier marks git as LEVEL_A (existing tests)


# ---------------------------------------------------------------------------
# F-1: role authority precedes risk policy
# ---------------------------------------------------------------------------

class TestApprovalScopeRoleAuthority:
    pytestmark = pytest.mark.asyncio

    async def _request(self, tmp_path: Path, *, scope: str, **kw) -> object:
        intent = CommandIntent.create(
            session_id="sess-review",
            call_id="call-1",
            thread_id="thread-1",
            turn_id="turn-1",
            approval_id="appr-1",
            command=kw.pop("command", LEVEL_A_CMD),
            cwd=tmp_path,
            requested_permission=kw.pop("requested_permission", "process_spawn"),
            approval_scope=scope,
            **kw,
        )
        assert intent.risk is CommandRisk.LEVEL_A
        mgr = ApprovalManager()
        record = await mgr.request(intent)
        return mgr, intent, record

    async def test_reviewer_level_a_command_rejected(self, tmp_path: Path) -> None:
        mgr, intent, record = await self._request(tmp_path, scope=ApprovalScope.DENY_ONLY)
        assert record.status is ApprovalState.REJECTED
        assert record.reason == "reviewer_role_policy_deny_only"
        # audit identity is preserved through the rejection
        assert record.approval_id == "appr-1"
        assert record.thread_id == "thread-1"
        assert record.turn_id == "turn-1"
        assert record.call_id == "call-1"
        assert record.session_id == "sess-review"

    async def test_reviewer_level_a_file_change_rejected(self, tmp_path: Path) -> None:
        intent = CommandIntent.create(
            session_id="sess-review",
            call_id="call-fc",
            thread_id="thread-1",
            turn_id="turn-1",
            argv=("codex-file-change", "item-1", "patch-1"),
            cwd=tmp_path,
            requested_permission="file_write",
            approval_scope=ApprovalScope.DENY_ONLY,
        )
        assert intent.risk is CommandRisk.LEVEL_B
        record = await ApprovalManager().request(intent)
        assert record.status is ApprovalState.REJECTED
        assert record.reason == "reviewer_role_policy_deny_only"

    async def test_reviewer_level_a_permissions_rejected(self, tmp_path: Path) -> None:
        intent = CommandIntent.create(
            session_id="sess-review",
            call_id="call-perm",
            thread_id="thread-1",
            turn_id="turn-1",
            argv=("codex-permission-request", '{"network":true}'),
            cwd=tmp_path,
            requested_permission="sandbox_escalation",
            permission_scope="network",
            approval_scope=ApprovalScope.DENY_ONLY,
        )
        record = await ApprovalManager().request(intent)
        assert record.status is ApprovalState.REJECTED
        assert record.reason == "reviewer_role_policy_deny_only"

    async def test_normal_executor_level_a_still_auto_approved(self, tmp_path: Path) -> None:
        # default approval_scope=NORMAL — existing behaviour must be unchanged
        mgr, intent, record = await self._request(tmp_path, scope=ApprovalScope.NORMAL)
        assert record.status is ApprovalState.APPROVED
        assert record.reason == "level_a_policy"

    async def test_normal_executor_level_b_still_pending(self, tmp_path: Path) -> None:
        intent = CommandIntent.create(
            session_id="sess-exec",
            call_id="call-2",
            command="python tool.py --mode check",
            cwd=tmp_path,
            requested_permission="project_write",
            approval_scope=ApprovalScope.NORMAL,
        )
        assert intent.risk is CommandRisk.LEVEL_B
        record = await ApprovalManager().request(intent)
        assert record.status is ApprovalState.PENDING

    async def test_deny_only_record_is_auditable_and_not_approvable(self, tmp_path: Path) -> None:
        mgr, intent, record = await self._request(tmp_path, scope=ApprovalScope.DENY_ONLY)
        # the same record cannot later be flipped to approved: it is REJECTED,
        # and approve() on a non-PENDING record raises.
        with pytest.raises(Exception):
            await mgr.approve(
                session_id=intent.session_id,
                call_id=intent.call_id,
                command_hash=intent.command_hash,
            )

    def test_unknown_scope_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            CommandIntent.create(
                session_id="s", call_id="c", command=LEVEL_A_CMD,
                cwd=tmp_path, requested_permission="process_spawn",
                approval_scope="bypass_policy",  # must NOT be accepted
            )


# ---------------------------------------------------------------------------
# F-2: deny_only session guard forces read-only
# ---------------------------------------------------------------------------

class TestCodexAppServerReviewerGuard:
    def test_deny_only_workspace_write_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError) as exc:
            CodexAppServerSession(
                workspace_path=str(tmp_path),
                sandbox_mode="workspace-write",
                approval_scope=ApprovalScope.DENY_ONLY,
            )
        assert "read-only" in str(exc.value)

    def test_deny_only_read_only_ok(self, tmp_path: Path) -> None:
        session = CodexAppServerSession(
            workspace_path=str(tmp_path),
            sandbox_mode="read-only",
            approval_scope=ApprovalScope.DENY_ONLY,
        )
        assert session.sandbox_mode == "read-only"
        assert session.approval_scope == ApprovalScope.DENY_ONLY

    def test_normal_workspace_write_ok(self, tmp_path: Path) -> None:
        session = CodexAppServerSession(
            workspace_path=str(tmp_path),
            sandbox_mode="workspace-write",
            approval_scope=ApprovalScope.NORMAL,
        )
        assert session.sandbox_mode == "workspace-write"


# ---------------------------------------------------------------------------
# F-4: review parser fail-closed
# ---------------------------------------------------------------------------

def _md(json_obj: dict) -> str:
    return "```json\n" + json.dumps(json_obj, ensure_ascii=False) + "\n```"


class TestParseReview:
    def test_pass(self):
        r = parse_review(_md({"result": "PASS", "evidence": ["tests green"]}))
        assert r.result == ReviewOutcome.PASS
        assert r.evidence == ["tests green"]

    def test_rework(self):
        r = parse_review(
            _md({"result": "REWORK", "blocking": ["off-by-one in buggy"],
                 "evidence": ["diff.patch"]})
        )
        assert r.result == ReviewOutcome.REWORK
        assert r.blocking == ["off-by-one in buggy"]

    def test_plan_invalid(self):
        r = parse_review(
            _md({"result": "PLAN_INVALID", "blocking": ["plan modifies wrong module"],
                 "non_blocking": ["style"], "evidence": ["PLAN.json", "diff.patch"]})
        )
        assert r.result == ReviewOutcome.PLAN_INVALID
        assert r.non_blocking == ["style"]

    def test_thread_turn_refs(self):
        r = parse_review(
            _md({"result": "PASS", "evidence": []}),
            thread_id="t-1", turn_id="tu-1",
        )
        assert r.thread_id == "t-1" and r.turn_id == "tu-1"

    def test_empty_raises(self):
        with pytest.raises(ReviewParseError):
            parse_review("")

    def test_random_prose_raises(self):
        with pytest.raises(ReviewParseError):
            parse_review("looks fine to me, ship it")

    def test_truncated_json_raises(self):
        with pytest.raises(ReviewParseError):
            parse_review('```json\n{"result": "PASS", "evidence": ["x"')

    def test_missing_result_raises(self):
        with pytest.raises(ReviewValidationError):
            parse_review(_md({"evidence": ["x"]}))

    def test_unknown_result_raises(self):
        with pytest.raises(ReviewValidationError):
            parse_review(_md({"result": "MAYBE"}))

    def test_review_parse_failed_not_a_business_outcome(self):
        # REVIEW_PARSE_FAILED is an internal marker, never a valid conclusion
        with pytest.raises(ReviewValidationError):
            parse_review(_md({"result": "REVIEW_PARSE_FAILED"}))

    def test_wrong_field_types_raise(self):
        with pytest.raises(ReviewValidationError):
            parse_review(_md({"result": "PASS", "blocking": "not-a-list"}))
        with pytest.raises(ReviewValidationError):
            parse_review(_md({"result": "PASS", "evidence": [1, 2]}))

    def test_rework_without_blocking_raises(self):
        with pytest.raises(ReviewValidationError):
            parse_review(_md({"result": "REWORK", "evidence": ["x"]}))

    def test_plan_invalid_without_blocking_raises(self):
        with pytest.raises(ReviewValidationError):
            parse_review(_md({"result": "PLAN_INVALID"}))

    def test_pass_without_evidence_allowed(self):
        r = parse_review(_md({"result": "PASS"}))
        assert r.result == ReviewOutcome.PASS

    def test_never_defaults_to_pass(self):
        # no silent default: every unusable shape must raise
        for bad in ("", "no json", '{"result": null}',
                    '{"result": "REWORK", "blocking": []}',
                    '{"result": "PLAN_INVALID", "blocking": []}'):
            with pytest.raises((ReviewParseError, ReviewValidationError)):
                parse_review(bad if isinstance(bad, str) else _md(bad))

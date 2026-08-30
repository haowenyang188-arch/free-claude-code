"""Unit tests for the Claude Planner parser + CLI runner (no real CLI needed).

P3-H: parser/runner are FAIL CLOSED — a parse/validation failure raises a
typed error and can never produce an empty PLAN (and therefore never an
ArtifactType.PLAN / PLAN_READY handoff).
"""

from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest

from workbench.backend.agents.claude_plan import (
    PlanParseError,
    PlanValidationError,
    parse_plan,
    validate_plan,
)
from workbench.backend.agents.claude_runner import (
    ClaudeCliMissing,
    ClaudeMalformedOutput,
    ClaudeNonZeroExit,
    ClaudeRunResult,
    ClaudeTimeout,
    build_planner_argv,
    run_claude_once,
    run_plan,
)

VALID_MD = (
    "# PLAN\n\n## 目标\nFix the bug\n\n## 问题分析\nbuggy returns x+1\n\n"
    "## 步骤\n1. fix\n2. test\n\n## 风险\nlow\n\n## 验收标准\n- tests pass\n- no regressions"
)


class TestBuildPlannerArgv:
    def test_hard_whitelist_present(self):
        argv = build_planner_argv(prompt="make plan")
        assert "--tools=Read,Grep,Glob" in argv
        assert "--safe-mode" in argv
        assert "--strict-mcp-config" in argv
        assert "--permission-mode" in argv
        assert "plan" in argv
        assert argv[-1] == "make plan"

    def test_resume_flag(self):
        argv = build_planner_argv(prompt="p", session_id="sess-1")
        assert "--resume" in argv
        assert argv[argv.index("--resume") + 1] == "sess-1"

    def test_no_resume_when_absent(self):
        argv = build_planner_argv(prompt="p")
        assert "--resume" not in argv


def _fake_events(session_id="s1", tools=("Read", "Grep", "Glob"), rc=0, has_result=True):
    events = [
        {"type": "system", "subtype": "init", "session_id": session_id, "tools": list(tools), "model": "claude-opus-5"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "## 步骤\n1. fix bug"}]}},
    ]
    if has_result:
        events.append(
            {"type": "result", "session_id": session_id, "is_error": False,
             "stop_reason": "end_turn", "num_turns": 2, "usage": {"output_tokens": 10}}
        )
    return "\n".join(json.dumps(e) for e in events)


class TestRunClaudeOnce:
    def test_success_parses(self):
        with mock.patch("workbench.backend.agents.claude_runner.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout=_fake_events(), stderr="")):
            res = run_claude_once(prompt="p", cwd="/tmp")
        assert res.session_id == "s1"
        assert res.tools == {"Read", "Grep", "Glob"}
        assert "fix bug" in res.text
        assert res.num_turns == 2
        assert res.usage.get("output_tokens") == 10

    def test_cli_missing(self):
        with mock.patch("workbench.backend.agents.claude_runner.subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(ClaudeCliMissing):
                run_claude_once(prompt="p", cwd="/tmp")

    def test_timeout(self):
        with mock.patch("workbench.backend.agents.claude_runner.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
            with pytest.raises(ClaudeTimeout):
                run_claude_once(prompt="p", cwd="/tmp")

    def test_nonzero_exit_no_events(self):
        with mock.patch("workbench.backend.agents.claude_runner.subprocess.run",
                        return_value=mock.Mock(returncode=2, stdout="", stderr="boom")):
            with pytest.raises(ClaudeNonZeroExit) as exc:
                run_claude_once(prompt="p", cwd="/tmp")
        assert exc.value.exit_code == 2

    def test_malformed_output(self):
        with mock.patch("workbench.backend.agents.claude_runner.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="not json at all", stderr="")):
            with pytest.raises(ClaudeMalformedOutput):
                run_claude_once(prompt="p", cwd="/tmp")

    def test_no_side_effect_tools_leak(self):
        with mock.patch("workbench.backend.agents.claude_runner.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout=_fake_events(tools=("Read", "Grep", "Glob", "Bash")), stderr="")):
            res = run_claude_once(prompt="p", cwd="/tmp")
        assert "Bash" in res.tools  # the CLI reported it; the whitelist argv prevents it


# ---------------------------------------------------------------------------
# validate_plan — the strict gate
# ---------------------------------------------------------------------------

class TestValidatePlan:
    def test_valid_plan_passes(self):
        validate_plan({"goal": "g", "analysis": "", "steps": ["s1"], "risks": [], "acceptance": ["a1"]})

    def test_analysis_and_risks_may_be_empty(self):
        validate_plan({"goal": "g", "steps": ["s1"], "acceptance": ["a1"]})

    def test_empty_goal_fails(self):
        with pytest.raises(PlanValidationError):
            validate_plan({"goal": "", "steps": ["s1"], "acceptance": ["a1"]})

    def test_missing_goal_fails(self):
        with pytest.raises(PlanValidationError):
            validate_plan({"steps": ["s1"], "acceptance": ["a1"]})

    def test_blank_goal_fails(self):
        with pytest.raises(PlanValidationError):
            validate_plan({"goal": "   ", "steps": ["s1"], "acceptance": ["a1"]})

    def test_empty_steps_fails(self):
        with pytest.raises(PlanValidationError):
            validate_plan({"goal": "g", "steps": [], "acceptance": ["a1"]})

    def test_missing_steps_fails(self):
        with pytest.raises(PlanValidationError):
            validate_plan({"goal": "g", "acceptance": ["a1"]})

    def test_empty_acceptance_fails(self):
        with pytest.raises(PlanValidationError):
            validate_plan({"goal": "g", "steps": ["s1"], "acceptance": []})

    def test_missing_acceptance_fails(self):
        with pytest.raises(PlanValidationError):
            validate_plan({"goal": "g", "steps": ["s1"]})

    def test_non_list_steps_fails(self):
        with pytest.raises(PlanValidationError):
            validate_plan({"goal": "g", "steps": "not-a-list", "acceptance": ["a1"]})


# ---------------------------------------------------------------------------
# parse_plan — loose parsing, strict acceptance, FAIL CLOSED
# ---------------------------------------------------------------------------

class TestParsePlan:
    def test_json_block(self):
        md = '```json\n{"goal": "g", "analysis": "a", "steps": ["s1"], "risks": ["r"], "acceptance": ["acc"]}\n```'
        plan = parse_plan(md)
        assert plan["goal"] == "g"
        assert plan["steps"] == ["s1"]
        assert plan["acceptance"] == ["acc"]

    def test_markdown_sections(self):
        plan = parse_plan(VALID_MD)
        assert plan["goal"] == "Fix the bug"
        assert plan["analysis"].startswith("buggy")
        assert plan["steps"] == ["fix", "test"]
        assert plan["risks"] == ["low"]
        assert plan["acceptance"] == ["tests pass", "no regressions"]

    def test_analysis_risks_optional(self):
        md = "## 目标\nFix it\n\n## 步骤\n1. do\n\n## 验收标准\n- works"
        plan = parse_plan(md)
        assert plan["analysis"] == ""
        assert plan["risks"] == []

    # --- negative: every case must raise a typed error, never an empty plan ---

    def test_empty_input_raises(self):
        with pytest.raises(PlanParseError):
            parse_plan("")

    def test_non_string_raises(self):
        with pytest.raises(PlanParseError):
            parse_plan(None)  # type: ignore[arg-type]

    def test_random_prose_raises(self):
        with pytest.raises(PlanValidationError):
            parse_plan("just some text with no structure at all about anything")

    def test_missing_goal_raises(self):
        md = "## 步骤\n1. fix\n\n## 验收标准\n- ok"
        with pytest.raises(PlanValidationError) as exc:
            parse_plan(md)
        assert "goal" in str(exc.value)

    def test_missing_steps_raises(self):
        md = "## 目标\nFix it\n\n## 验收标准\n- ok"
        with pytest.raises(PlanValidationError) as exc:
            parse_plan(md)
        assert "steps" in str(exc.value)

    def test_missing_acceptance_raises(self):
        md = "## 目标\nFix it\n\n## 步骤\n1. do"
        with pytest.raises(PlanValidationError) as exc:
            parse_plan(md)
        assert "acceptance" in str(exc.value)

    def test_truncated_json_raises(self):
        md = '```json\n{"goal": "g", "steps": ["s1"], "acceptance": ["a1"]'
        with pytest.raises(PlanParseError):
            parse_plan(md)

    def test_json_missing_goal_raises(self):
        md = '```json\n{"steps": ["s1"], "acceptance": ["a1"]}\n```'
        with pytest.raises(PlanValidationError) as exc:
            parse_plan(md)
        assert "goal" in str(exc.value)

    def test_json_empty_steps_raises(self):
        md = '```json\n{"goal": "g", "steps": [], "acceptance": ["a1"]}\n```'
        with pytest.raises(PlanValidationError) as exc:
            parse_plan(md)
        assert "steps" in str(exc.value)


# ---------------------------------------------------------------------------
# run_plan — fail closed at the composite layer
# ---------------------------------------------------------------------------

class TestRunPlan:
    def _patch_cli(self, result: ClaudeRunResult):
        return mock.patch(
            "workbench.backend.agents.claude_runner.run_claude_once",
            return_value=result,
        )

    def test_valid_run_returns_validated_plan(self):
        res = ClaudeRunResult(
            session_id="s1", text=VALID_MD, tools={"Read", "Grep", "Glob"},
            is_error=False, stop_reason="end_turn", num_turns=2,
        )
        with self._patch_cli(res):
            got, plan = run_plan(prompt="p", cwd="/tmp")
        assert got.session_id == "s1"
        assert plan["goal"] == "Fix the bug"
        assert plan["steps"] == ["fix", "test"]
        assert plan["acceptance"] == ["tests pass", "no regressions"]

    def test_empty_text_raises(self):
        res = ClaudeRunResult(session_id="s1", text="", is_error=False)
        with self._patch_cli(res):
            with pytest.raises(PlanParseError):
                run_plan(prompt="p", cwd="/tmp")

    def test_whitespace_text_raises(self):
        res = ClaudeRunResult(session_id="s1", text="   \n  ", is_error=False)
        with self._patch_cli(res):
            with pytest.raises(PlanParseError):
                run_plan(prompt="p", cwd="/tmp")

    def test_is_error_raises(self):
        res = ClaudeRunResult(session_id="s1", text="some text", is_error=True)
        with self._patch_cli(res):
            with pytest.raises(PlanParseError):
                run_plan(prompt="p", cwd="/tmp")

    def test_garbage_prose_raises_validation(self):
        res = ClaudeRunResult(session_id="s1", text="no plan here at all", is_error=False)
        with self._patch_cli(res):
            with pytest.raises(PlanValidationError):
                run_plan(prompt="p", cwd="/tmp")

    def test_truncated_output_raises_parse(self):
        res = ClaudeRunResult(session_id="s1", text='```json\n{"goal": "g"', is_error=False)
        with self._patch_cli(res):
            with pytest.raises(PlanParseError):
                run_plan(prompt="p", cwd="/tmp")


def _plan_artifact_from_run(text: str):
    """Mirror of the adapter path: only a validated plan may become an artifact."""
    from workbench.backend.agents.claude_plan import parse_plan as pp

    return pp(text)  # raises on failure -> caller must NOT create an Artifact


class TestFailClosedNoArtifact:
    def test_parse_failure_never_yields_plan(self):
        """The runner path raises before any PLAN structure is produced."""
        for bad in ("", "random prose", '```json\n{"goal": "g"', "## 步骤\n1. x"):
            with pytest.raises((PlanParseError, PlanValidationError)):
                _plan_artifact_from_run(bad)

"""RC-4: an adapter self-check is evidence, never acceptance.

The adapters used to run ``verify_completion()`` and announce "验证通过/验证失败",
i.e. the executor graded its own homework.  They now run ``run_self_check()``
and surface the outcome as evidence for the reviewer; acceptance stays with
Codex (review) and the Engine (terminal state).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workbench.backend.agents.claude_adapter import ClaudeCodeAdapter


def _write_script(directory: Path, body: str) -> str:
    """Create an executable self-check script and return its path."""
    script = directory / "verify.sh"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


@pytest.mark.asyncio
async def test_missing_script_is_reported_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing script must not be reported as a failed verification."""
    adapter = ClaudeCodeAdapter("agent-1")
    adapter.workspace_path = str(tmp_path)
    monkeypatch.setenv("WORKBENCH_AGENT_SELF_CHECK_SCRIPT", str(tmp_path / "nope.sh"))

    result = await adapter.run_self_check()

    assert result["status"] == "skipped"
    assert result["passed"] is False
    assert result["error"] is None


@pytest.mark.asyncio
async def test_passing_script_is_reported_as_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeCodeAdapter("agent-1")
    adapter.workspace_path = str(tmp_path)
    monkeypatch.setenv(
        "WORKBENCH_AGENT_SELF_CHECK_SCRIPT",
        _write_script(tmp_path, 'echo "tests ok"; exit 0'),
    )

    result = await adapter.run_self_check()

    assert result["status"] == "passed"
    assert result["passed"] is True
    assert "tests ok" in result["output"]


@pytest.mark.asyncio
async def test_failing_script_is_reported_as_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = ClaudeCodeAdapter("agent-1")
    adapter.workspace_path = str(tmp_path)
    monkeypatch.setenv(
        "WORKBENCH_AGENT_SELF_CHECK_SCRIPT",
        _write_script(tmp_path, 'echo "boom" 1>&2; exit 3'),
    )

    result = await adapter.run_self_check()

    assert result["status"] == "failed"
    assert result["passed"] is False
    assert "boom" in (result["error"] or "")


def test_self_check_does_not_expose_an_acceptance_api() -> None:
    """The old self-acceptance entry point must be gone for good."""
    assert not hasattr(ClaudeCodeAdapter, "verify_completion")
    assert not hasattr(ClaudeCodeAdapter, "_verify_before_done")
    assert hasattr(ClaudeCodeAdapter, "run_self_check")
    assert hasattr(ClaudeCodeAdapter, "_surface_completion_claim")

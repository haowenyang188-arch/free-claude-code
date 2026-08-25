from __future__ import annotations

import json
from pathlib import Path

import pytest

from workbench.backend.runtime.events import EventLog
from workbench.backend.runtime.workspace import WorkspacePolicy, WorkspacePolicyError


def test_event_log_assigns_ordered_sequences_and_replays_after_cursor(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "events.jsonl"
    log = EventLog(log_path)

    first = log.append(
        run_id="run-1",
        event_type="run_started",
        payload={"message": "started"},
        backend="codex",
    )
    second = log.append(
        run_id="run-1",
        event_type="tool_started",
        payload={"tool": "shell"},
        backend="codex",
    )
    other = log.append(
        run_id="run-2",
        event_type="run_started",
        payload={},
        backend="claude_code",
    )

    assert first.sequence == 1
    assert second.sequence == 2
    assert other.sequence == 1
    assert [event.sequence for event in log.replay("run-1")] == [1, 2]
    assert [event.event_type for event in log.replay("run-1", after=1)] == [
        "tool_started"
    ]


def test_event_log_restores_sequence_after_process_restart(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    EventLog(log_path).append(
        run_id="run-1",
        event_type="run_started",
        payload={},
        backend="codex",
    )

    restored = EventLog(log_path)
    event = restored.append(
        run_id="run-1",
        event_type="run_completed",
        payload={},
        backend="codex",
    )

    assert event.sequence == 2
    assert [item.event_type for item in restored.replay("run-1")] == [
        "run_started",
        "run_completed",
    ]


def test_event_log_redacts_sensitive_nested_payload_values(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    log = EventLog(log_path)

    log.append(
        run_id="run-1",
        event_type="tool_finished",
        payload={
            "output": "safe",
            "nested": {"api_key": "secret", "items": [{"cookie": "crumb"}]},
        },
        backend="codex",
    )

    raw = log_path.read_text(encoding="utf-8")
    assert "secret" not in raw
    assert "crumb" not in raw
    stored = json.loads(raw)
    assert stored["payload"]["nested"]["api_key"] == "[redacted]"
    assert stored["payload"]["nested"]["items"][0]["cookie"] == "[redacted]"


def test_workspace_policy_allows_children_and_rejects_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (tmp_path / "outside").mkdir()
    policy = WorkspacePolicy(workspace)

    assert policy.resolve("src") == (workspace / "src").resolve()

    with pytest.raises(WorkspacePolicyError, match="inside workspace root"):
        policy.resolve(tmp_path / "outside")


def test_workspace_policy_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "link"
    link.symlink_to(outside, target_is_directory=True)
    policy = WorkspacePolicy(workspace)

    with pytest.raises(WorkspacePolicyError, match="inside workspace root"):
        policy.resolve(link)

"""Test extended EventEnvelope with SOP orchestrator fields."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from providers.common import RuntimeIdentity
from workbench.backend.runtime.events import EventEnvelope, EventLog


def test_event_envelope_includes_runtime_kind() -> None:
    """EventEnvelope should support runtime_kind field."""
    event = EventEnvelope(
        id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="task.started",
        timestamp=datetime.now(UTC),
        backend="claude_code",
        payload={"task_id": "task-1"},
        session_id="session-1",
        runtime_kind="claude_code",
    )

    assert event.runtime_kind == "claude_code"


def test_event_envelope_includes_agent_profile_id() -> None:
    """EventEnvelope should support agent_profile_id field."""
    event = EventEnvelope(
        id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="task.executed",
        timestamp=datetime.now(UTC),
        backend="codex",
        payload={"result": "done"},
        agent_profile_id="agent-profile-researcher-001",
    )

    assert event.agent_profile_id == "agent-profile-researcher-001"


def test_event_envelope_includes_step_id() -> None:
    """EventEnvelope should support step_id field for correlating events to workflow steps."""
    event = EventEnvelope(
        id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="step.completed",
        timestamp=datetime.now(UTC),
        backend="claude_code",
        payload={"status": "completed"},
        step_id="step-run-42",
    )

    assert event.step_id == "step-run-42"


def test_event_envelope_includes_artifact_ids() -> None:
    """EventEnvelope should support artifact_ids list for tracking produced artifacts."""
    event = EventEnvelope(
        id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="handoff.created",
        timestamp=datetime.now(UTC),
        backend="orchestrator",
        payload={"handoff_id": "handoff-1"},
        artifact_ids=["artifact-1", "artifact-2"],
    )

    assert event.artifact_ids == ["artifact-1", "artifact-2"]


def test_event_envelope_includes_runtime_generation() -> None:
    event = EventEnvelope(
        id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="task.started",
        timestamp=datetime.now(UTC),
        backend="codex",
        payload={},
        generation="generation-1",
    )

    assert event.generation == "generation-1"


def test_event_envelope_optional_fields_default_to_none() -> None:
    """New fields should be optional and default to None."""
    event = EventEnvelope(
        id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="system.checkpoint",
        timestamp=datetime.now(UTC),
        backend="orchestrator",
        payload={},
    )

    assert event.runtime_kind is None
    assert event.agent_profile_id is None
    assert event.step_id is None
    assert event.artifact_ids is None


def test_event_envelope_to_mapping_includes_new_fields() -> None:
    """to_mapping() should serialize all new fields."""
    event = EventEnvelope(
        id="evt-1",
        run_id="run-1",
        sequence=1,
        event_type="task.started",
        timestamp=datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC),
        backend="claude_code",
        payload={"task_id": "task-1"},
        session_id="session-1",
        runtime_kind="claude_code",
        agent_profile_id="agent-001",
        step_id="step-run-5",
        artifact_ids=["artifact-1"],
    )

    mapping = event.to_mapping()

    assert mapping["runtime_kind"] == "claude_code"
    assert mapping["agent_profile_id"] == "agent-001"
    assert mapping["step_id"] == "step-run-5"
    assert mapping["artifact_ids"] == ["artifact-1"]
    assert mapping["generation"] is None


def test_event_envelope_from_mapping_reconstructs_new_fields() -> None:
    """from_mapping() should reconstruct events with new fields."""
    mapping = {
        "id": "evt-1",
        "run_id": "run-1",
        "sequence": 1,
        "event_type": "task.completed",
        "timestamp": "2026-08-25T12:00:00+00:00",
        "backend": "codex",
        "payload": {"result": "success"},
        "session_id": "session-1",
        "runtime_kind": "codex",
        "agent_profile_id": "agent-002",
        "step_id": "step-run-10",
        "artifact_ids": ["artifact-2", "artifact-3"],
        "generation": "generation-2",
    }

    event = EventEnvelope.from_mapping(mapping)

    assert event.runtime_kind == "codex"
    assert event.agent_profile_id == "agent-002"
    assert event.step_id == "step-run-10"
    assert event.artifact_ids == ["artifact-2", "artifact-3"]
    assert event.generation == "generation-2"


def test_event_envelope_from_mapping_handles_missing_new_fields() -> None:
    """from_mapping() should handle events without new fields (backward compatibility)."""
    mapping = {
        "id": "evt-1",
        "run_id": "run-1",
        "sequence": 1,
        "event_type": "legacy.event",
        "timestamp": "2026-08-25T12:00:00+00:00",
        "backend": "legacy",
        "payload": {},
    }

    event = EventEnvelope.from_mapping(mapping)

    assert event.runtime_kind is None
    assert event.agent_profile_id is None
    assert event.step_id is None
    assert event.artifact_ids is None
    assert event.generation is None


def test_event_log_persists_and_replays_extended_fields(tmp_path) -> None:
    """EventLog should persist and replay events with extended fields."""
    log_path = tmp_path / "events.jsonl"
    log = EventLog(log_path)

    # Append event with extended fields
    event1 = log.append(
        run_id="run-1",
        event_type="task.started",
        payload={"task_id": "task-1"},
        backend="claude_code",
        session_id="session-1",
        runtime_kind="claude_code",
        agent_profile_id="agent-001",
        step_id="step-run-1",
        artifact_ids=["artifact-upstream-1"],
        generation="generation-1",
    )

    assert event1.runtime_kind == "claude_code"
    assert event1.agent_profile_id == "agent-001"
    assert event1.step_id == "step-run-1"
    assert event1.artifact_ids == ["artifact-upstream-1"]
    assert event1.generation == "generation-1"

    # Reload from disk
    log2 = EventLog(log_path)
    replayed = log2.replay("run-1")

    assert len(replayed) == 1
    assert replayed[0].runtime_kind == "claude_code"
    assert replayed[0].agent_profile_id == "agent-001"
    assert replayed[0].step_id == "step-run-1"
    assert replayed[0].artifact_ids == ["artifact-upstream-1"]
    assert replayed[0].generation == "generation-1"


def test_event_log_append_accepts_optional_extended_fields(tmp_path) -> None:
    """EventLog.append() should accept new fields as optional parameters."""
    log_path = tmp_path / "events.jsonl"
    log = EventLog(log_path)

    # Without extended fields
    event1 = log.append(
        run_id="run-1",
        event_type="system.checkpoint",
        payload={},
        backend="orchestrator",
    )
    assert event1.runtime_kind is None

    # With some extended fields
    event2 = log.append(
        run_id="run-1",
        event_type="task.completed",
        payload={"status": "completed"},
        backend="claude_code",
        step_id="step-run-2",
    )
    assert event2.step_id == "step-run-2"
    assert event2.runtime_kind is None


def test_event_log_merges_explicit_identity_with_legacy_fallbacks(tmp_path) -> None:
    """Explicit identity wins while legacy fields fill only missing values."""
    log_path = tmp_path / "events.jsonl"
    log = EventLog(log_path)

    event = log.append(
        run_id="run-1",
        event_type="approval.requested",
        payload={},
        backend="codex",
        identity=RuntimeIdentity(
            provider="codex",
            thread_id="thread-explicit",
            approval_id="approval-explicit",
            run_id="run-1",
        ),
        provider="legacy-provider",
        runtime_id="runtime-legacy",
        thread_id="thread-legacy",
        turn_id="turn-legacy",
        approval_id="approval-legacy",
    )

    assert event.provider == "codex"
    assert event.runtime_id == "runtime-legacy"
    assert event.thread_id == "thread-explicit"
    assert event.turn_id == "turn-legacy"
    assert event.approval_id == "approval-explicit"
    assert event.message_id is None

    replayed = EventLog(log_path).replay("run-1")
    assert len(replayed) == 1
    assert replayed[0].to_mapping()["identity"] == {
        "provider": "codex",
        "runtime_id": "runtime-legacy",
        "thread_id": "thread-explicit",
        "turn_id": "turn-legacy",
        "approval_id": "approval-explicit",
        "run_id": "run-1",
    }


def test_event_log_append_validates_artifact_ids_is_list(tmp_path) -> None:
    """EventLog.append() should validate that artifact_ids is a list when provided."""
    log_path = tmp_path / "events.jsonl"
    log = EventLog(log_path)

    with pytest.raises(TypeError, match="artifact_ids must be a list"):
        log.append(
            run_id="run-1",
            event_type="test",
            payload={},
            backend="test",
            artifact_ids="not-a-list",  # Invalid
        )

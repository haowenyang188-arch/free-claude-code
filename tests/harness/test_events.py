"""Pure contract tests for the DeepSeek Harness event projection."""

from __future__ import annotations

import json

from harness.events import (
    SOURCE,
    project_notification,
    project_sse,
    safe_log_context,
)
from providers.common import RuntimeIdentity


def _sse_payload(frame: str) -> tuple[str, dict]:
    """Parse the small SSE frame emitted by ``project_sse``."""
    lines = frame.splitlines()
    event_line = next(line for line in lines if line.startswith("event: "))
    data_line = next(line for line in lines if line.startswith("data: "))
    return event_line.removeprefix("event: "), json.loads(
        data_line.removeprefix("data: ")
    )


def test_session_event_envelope_preserves_full_runtime_event() -> None:
    event = {
        "type": "assistant/message",
        "seq": 7,
        "data": {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
            }
        },
    }

    envelope = project_notification(
        "session.event", {"sessionId": "root", "event": event}
    )

    assert envelope["type"] == "session_event"
    assert envelope["source"] == SOURCE == "deepseek_harness"
    assert envelope["method"] == "session.event"
    assert envelope["session_id"] == "root"
    assert envelope["runtime_session_id"] == "root"
    assert envelope["event_type"] == "assistant/message"
    assert envelope["event"] == event
    assert envelope["payload"] == {"sessionId": "root", "event": event}

    # The projection owns its copy and cannot mutate a runtime notification.
    event["data"]["message"]["content"][0]["text"] = "changed"
    projected_event = envelope["event"]
    assert isinstance(projected_event, dict)
    assert projected_event["data"]["message"]["content"][0]["text"] == "hello"


def test_session_status_envelope_normalizes_status_without_dropping_payload() -> None:
    params = {"sessionId": "root", "status": "running", "extra": {"n": 1}}

    envelope = project_notification("session.status", params)

    assert envelope["type"] == "session_status"
    assert envelope["session_id"] == "root"
    assert envelope["runtime_session_id"] == "root"
    assert envelope["status"] == "running"
    assert envelope["payload"] == params


def test_subagent_lifecycle_envelopes_keep_parent_and_child_ids() -> None:
    started = project_notification(
        "subagent.started",
        {"parentSessionId": "root", "childSessionId": "child"},
    )
    finished = project_notification(
        "subagent.finished",
        {
            "provider": "deepseek-official",
            "agentId": "agent-1",
            "parentSessionId": "root",
            "childSessionId": "child",
            "status": "ok",
            "stopReason": {"kind": "completed"},
            "lastAssistantMessage": [{"type": "text", "text": "done"}],
        },
    )

    assert started["type"] == "subagent_started"
    assert started["session_id"] == "root"
    assert started["runtime_session_id"] == "child"
    assert started["parent_session_id"] == "root"
    assert started["child_session_id"] == "child"

    assert finished["type"] == "subagent_finished"
    assert finished["session_id"] == "root"
    assert finished["runtime_session_id"] == "child"
    assert finished["provider"] == "deepseek-official"
    assert finished["agent_id"] == "agent-1"
    assert finished["status"] == "ok"
    assert finished["stop_reason"] == {"kind": "completed"}
    assert finished["last_assistant_message"] == [{"type": "text", "text": "done"}]


def test_unknown_notification_keeps_raw_payload_and_is_not_logged() -> None:
    params = {
        "sessionId": "root",
        "secret": "do-not-log",
        "nested": {"value": [1, True]},
    }

    envelope = project_notification("future.notification", params)

    assert envelope["type"] == "unknown"
    assert envelope["method"] == "future.notification"
    assert envelope["session_id"] == "root"
    assert envelope["raw"] == params
    assert envelope["payload"] == params

    context = safe_log_context(envelope)
    assert context["type"] == "unknown"
    assert context["method"] == "future.notification"
    assert "secret" not in context
    assert "raw" not in context
    assert "payload" not in context


def test_malformed_notification_is_safe_and_retains_raw_value() -> None:
    envelope = project_notification("session.event", ["not", "an", "object"])

    assert envelope["type"] == "session_event"
    assert envelope["session_id"] is None
    assert envelope["runtime_session_id"] is None
    assert envelope["event"] is None
    assert envelope["event_type"] is None
    assert envelope["raw"] == ["not", "an", "object"]
    assert envelope["payload"] == ["not", "an", "object"]


def test_malformed_fields_are_not_coerced_into_untrusted_identifiers() -> None:
    envelope = project_notification(
        "subagent.finished",
        {
            "parentSessionId": 123,
            "childSessionId": {"id": "child"},
            "status": ["ok"],
            "stopReason": object(),
            "lastAssistantMessage": {"text": "not-a-list"},
        },
    )

    assert envelope["session_id"] is None
    assert envelope["runtime_session_id"] is None
    assert envelope["parent_session_id"] is None
    assert envelope["child_session_id"] is None
    assert envelope["status"] is None
    assert envelope["stop_reason"] is None
    assert envelope["last_assistant_message"] is None
    raw = envelope["raw"]
    assert isinstance(raw, dict)
    assert raw["parentSessionId"] == 123


def test_session_event_with_unknown_event_type_keeps_event_verbatim() -> None:
    event = {"type": "plugin/private", "data": {"token": "opaque"}}
    envelope = project_notification(
        "session.event", {"sessionId": "root", "event": event}
    )

    assert envelope["event_type"] == "plugin/private"
    assert envelope["event"] == event
    assert envelope["raw_event"] == event


def test_session_event_projects_nested_runtime_identity_without_content_ids() -> None:
    params = {
        "sessionId": "runtime-session",
        "event": {
            "type": "tool/call",
            "turnId": "turn-1",
            "item": {"id": "item-1"},
            "approval": {"id": "approval-1"},
            "agent": {"id": "agent-1"},
            "tool": {"id": "tool-1"},
            "call": {"id": "call-1"},
            "oneShotId": "runtime-one-shot",
            "content": [{"messageId": "content-message-must-not-bind"}],
            "input": {"callId": "input-call-must-not-bind"},
        },
    }
    envelope = project_notification(
        "session.event",
        params,
        session_id="host-session",
        provider="deepseek-official",
        identity=RuntimeIdentity(
            run_id="host-run",
            message_id="host-message",
            one_shot_id="host-one-shot",
        ),
    )

    assert envelope["provider"] == "deepseek-official"
    assert envelope["runtime_id"] is None
    assert envelope["runtime_session_id"] == "runtime-session"
    assert envelope["turn_id"] == "turn-1"
    assert envelope["item_id"] == "item-1"
    assert envelope["approval_id"] == "approval-1"
    assert envelope["agent_id"] == "agent-1"
    assert envelope["tool_id"] == "tool-1"
    assert envelope["call_id"] == "call-1"
    assert envelope["run_id"] == "host-run"
    assert envelope["message_id"] == "host-message"
    assert envelope["one_shot_id"] == "host-one-shot"

    context = safe_log_context(envelope)
    assert context["run_id"] == "host-run"
    assert context["approval_id"] == "approval-1"
    assert "content" not in context
    assert "input" not in context
    assert "payload" not in context


def test_host_identity_fields_override_runtime_projection() -> None:
    envelope = project_notification(
        "session.event",
        {
            "provider": "runtime-provider",
            "sessionId": "runtime-session",
            "generation": "runtime-generation",
            "event": {
                "type": "tool/call",
                "agentId": "runtime-agent",
                "runId": "runtime-run",
                "turnId": "turn-1",
                "itemId": "item-1",
                "toolId": "tool-1",
                "callId": "call-1",
                "oneShotId": "runtime-one-shot",
            },
        },
        provider="host-provider",
        session_id="host-session",
        identity=RuntimeIdentity(
            provider="host-provider",
            session_id="host-session",
            generation="host-generation",
            agent_id="host-agent",
            run_id="host-run",
        ),
    )

    assert envelope["provider"] == "host-provider"
    assert envelope["session_id"] == "host-session"
    assert envelope["generation"] == "host-generation"
    assert envelope["agent_id"] == "host-agent"
    assert envelope["run_id"] == "host-run"
    assert envelope["runtime_session_id"] == "runtime-session"
    assert envelope["turn_id"] == "turn-1"
    assert envelope["item_id"] == "item-1"
    assert envelope["tool_id"] == "tool-1"
    assert envelope["call_id"] == "call-1"
    assert envelope["one_shot_id"] == "runtime-one-shot"


def test_runtime_event_ids_are_not_reused_from_a_previous_event() -> None:
    first = project_notification(
        "session.event",
        {
            "sessionId": "runtime-session",
            "event": {
                "type": "tool/call",
                "itemId": "item-1",
                "toolId": "tool-1",
                "callId": "call-1",
                "oneShotId": "one-shot-1",
            },
        },
        provider="host-provider",
        session_id="host-session",
        identity=RuntimeIdentity(
            provider="host-provider", session_id="host-session", run_id="host-run"
        ),
    )
    second = project_notification(
        "session.event",
        {
            "sessionId": "runtime-session",
            "event": {
                "type": "tool/call",
                "itemId": "item-2",
                "toolId": "tool-2",
                "callId": "call-2",
                "oneShotId": "one-shot-2",
            },
        },
        provider="host-provider",
        session_id="host-session",
        identity=RuntimeIdentity(
            provider="host-provider", session_id="host-session", run_id="host-run"
        ),
    )

    assert (
        first["item_id"],
        first["tool_id"],
        first["call_id"],
        first["one_shot_id"],
    ) == (
        "item-1",
        "tool-1",
        "call-1",
        "one-shot-1",
    )
    assert (
        second["item_id"],
        second["tool_id"],
        second["call_id"],
        second["one_shot_id"],
    ) == (
        "item-2",
        "tool-2",
        "call-2",
        "one-shot-2",
    )


def test_subagent_projection_keeps_host_identity_authoritative() -> None:
    envelope = project_notification(
        "subagent.finished",
        {
            "provider": "runtime-provider",
            "agentId": "runtime-agent",
            "parentSessionId": "host-session",
            "childSessionId": "child-session",
            "status": "ok",
        },
        provider="host-provider",
        session_id="host-session",
        identity=RuntimeIdentity(
            provider="host-provider",
            session_id="host-session",
            generation="host-generation",
            agent_id="host-agent",
            run_id="host-run",
        ),
    )

    assert envelope["provider"] == "host-provider"
    assert envelope["agent_id"] == "host-agent"
    assert envelope["session_id"] == "host-session"
    assert envelope["runtime_session_id"] == "child-session"
    assert envelope["run_id"] == "host-run"


def test_project_sse_emits_one_safe_custom_frame_without_fake_anthropic_turn() -> None:
    envelope = project_notification(
        "session.status", {"sessionId": "root", "status": "idle"}
    )

    frames = project_sse(envelope)

    assert len(frames) == 1
    event_name, data = _sse_payload(frames[0])
    assert event_name == "harness_session_status"
    assert data["type"] == "session_status"
    assert data["session_id"] == "root"
    assert data["status"] == "idle"
    assert "message_start" not in frames[0]
    assert frames[0].endswith("\n\n")


def test_project_sse_preserves_unknown_raw_event_data() -> None:
    envelope = project_notification(
        "session.event",
        {
            "sessionId": "root",
            "event": {"type": "future/event", "data": {"secret": "opaque"}},
        },
    )

    frames = project_sse(envelope)

    event_name, data = _sse_payload(frames[0])
    assert event_name == "harness_session_event"
    assert data["event"] == envelope["event"]
    assert data["event"]["data"]["secret"] == "opaque"


def test_project_sse_handles_non_mapping_input() -> None:
    frames = project_sse(None)

    assert len(frames) == 1
    event_name, data = _sse_payload(frames[0])
    assert event_name == "harness_unknown"
    assert data["type"] == "unknown"

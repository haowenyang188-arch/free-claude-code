"""Contract tests for provider-neutral runtime identity metadata."""

from __future__ import annotations

from providers.common import RuntimeIdentity, normalize_identity


def test_from_mapping_normalizes_provider_aliases_without_copying_payload() -> None:
    identity = RuntimeIdentity.from_mapping(
        {
            "provider": "codex",
            "runtimeId": "runtime-1",
            "sessionId": "session-1",
            "generation": "generation-1",
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "approvalId": "approval-1",
            "oneShotId": "one-shot-1",
            "agentId": "agent-1",
            "toolUseId": "tool-1",
            "callId": "call-1",
            "messageId": "message-1",
            "runId": "run-1",
            "secret": "must-not-be-copied",
        }
    )

    assert identity.to_mapping(include_unknown=False) == {
        "provider": "codex",
        "runtime_id": "runtime-1",
        "session_id": "session-1",
        "generation": "generation-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "item_id": "item-1",
        "approval_id": "approval-1",
        "one_shot_id": "one-shot-1",
        "agent_id": "agent-1",
        "tool_id": "tool-1",
        "call_id": "call-1",
        "message_id": "message-1",
        "run_id": "run-1",
    }


def test_from_mapping_rejects_invalid_identifiers() -> None:
    identity = RuntimeIdentity.from_mapping(
        {
            "session_id": " ",
            "thread_id": "contains\nnewline",
            "turn_id": "\x00control",
            "item_id": "x" * 513,
            "approval_id": 123,
            "one_shot_id": None,
        }
    )

    assert identity.to_mapping(include_unknown=False) == {}


def test_merge_fills_missing_fields_without_overwriting_existing_identity() -> None:
    primary = RuntimeIdentity(session_id="real-session", call_id="real-call")
    fallback = RuntimeIdentity(
        session_id="fallback-session",
        call_id="fallback-call",
        approval_id="approval-1",
        one_shot_id="one-shot-1",
    )

    merged = primary.merge(fallback)

    assert merged.session_id == "real-session"
    assert merged.call_id == "real-call"
    assert merged.approval_id == "approval-1"
    assert merged.one_shot_id == "one-shot-1"


def test_normalize_identity_merges_mapping_with_explicit_fallback() -> None:
    identity = normalize_identity(
        {"threadId": "thread-1", "secret": "opaque"},
        RuntimeIdentity(provider="deepseek_harness", run_id="run-1"),
    )

    assert identity.thread_id == "thread-1"
    assert identity.provider == "deepseek_harness"
    assert identity.run_id == "run-1"
    assert "secret" not in identity.to_mapping()


def test_normalize_identity_handles_non_mapping_values() -> None:
    identity = normalize_identity(object(), RuntimeIdentity(session_id="fallback"))

    assert identity.session_id == "fallback"

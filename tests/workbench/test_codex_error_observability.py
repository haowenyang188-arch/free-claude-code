"""Codex app-server error-notification observability (hotfix).

Scope: prove that an ``error`` notification preserves enough diagnostic
evidence to tell an EXTERNAL provider outage from a CODE/PROTOCOL defect,
without leaking credentials and without breaking the generic message that
existing callers already depend on.

Fault CLASSIFICATION is deliberately absent from this module and from the
production module under test: the caller decides BLOCKED_EXTERNAL vs
CODE_FAILURE from HTTP status, codexErrorInfo, provider error category and the
terminal event.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from workbench.backend.agents.codex_app_server import (
    CodexAppServerSession,
    _error_detail,
)

GENERIC = "Codex app-server error"


def _notification(error: Any, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"error": error}
    params.update(extra)
    return {"method": "error", "params": params}


def _blob(payload: dict[str, Any]) -> str:
    """Lower-cased JSON of the payload, used only to assert leakage."""
    return json.dumps(payload, ensure_ascii=False).lower()


# --------------------------------------------------------------------------
# provider 503 — the exact shape observed during the 2026-08-30 Health Gate
# --------------------------------------------------------------------------


def test_provider_503_preserves_status_and_upstream_message() -> None:
    detail = _error_detail(
        {
            "error": {
                "message": (
                    "unexpected status 503 Service Unavailable: 所有供应商已熔断，"
                    "无可用渠道, url: http://127.0.0.1:15721/v1/responses"
                ),
                "codexErrorInfo": {
                    "responseStreamDisconnected": {"httpStatusCode": 503}
                },
                "additionalDetails": None,
            },
            "willRetry": False,
            "threadId": "thr-1",
            "turnId": "turn-1",
        }
    )

    # Backwards compatibility: the generic message is unchanged.
    assert detail["message"] == GENERIC
    # Root cause is now visible.
    assert "503" in detail["upstream_message"]
    assert detail["upstream_error_info"] == {
        "responseStreamDisconnected": {"httpStatusCode": 503}
    }
    assert detail["retryable"] is False
    # No provider-independent classification string is baked in.
    assert detail.get("additional_details") is None


def test_provider_503_status_reachable_without_chinese_string_matching() -> None:
    """Classification must be possible from structure, not localised text."""
    detail = _error_detail(
        {
            "error": {
                "message": "stream disconnected",
                "codexErrorInfo": {
                    "responseStreamDisconnected": {"httpStatusCode": 503}
                },
            }
        }
    )
    status = detail["upstream_error_info"]["responseStreamDisconnected"][
        "httpStatusCode"
    ]
    assert status == 503


# --------------------------------------------------------------------------
# provider 403
# --------------------------------------------------------------------------


def test_provider_403_insufficient_balance_is_identifiable() -> None:
    detail = _error_detail(
        {
            "error": {
                "message": (
                    "CC Switch local proxy failed while handling Codex endpoint "
                    "/responses. Provider: tkapi; model: gpt-5.6-sol; "
                    "upstream_status: HTTP 403; cause: Insufficient account balance"
                ),
                "codexErrorInfo": "other",
                "additionalDetails": "upstream_status: HTTP 403",
            },
            "willRetry": False,
        }
    )

    blob = json.dumps(detail, ensure_ascii=False)
    assert "403" in blob
    assert "Insufficient account balance" in blob
    assert detail["upstream_error_info"] == "other"
    assert detail["additional_details"] == "upstream_status: HTTP 403"


# --------------------------------------------------------------------------
# minimal / malformed error — must degrade safely, never raise
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"error": {}},
        {"error": None},
        {"error": "boom"},
        {"error": []},
        {"error": {"message": None, "codexErrorInfo": None}},
        None,
        "not-a-mapping",
    ],
)
def test_minimal_and_malformed_errors_degrade_to_generic_message(
    params: Any,
) -> None:
    detail = _error_detail(params)
    assert detail["message"] == GENERIC
    # Diagnostic keys are omitted rather than filled with nulls.
    for key in ("upstream_message", "upstream_error_info", "additional_details"):
        assert key not in detail
    # The payload is always JSON-serialisable.
    json.dumps(detail)


# --------------------------------------------------------------------------
# sensitive fields must never be echoed
# --------------------------------------------------------------------------


def test_sensitive_extra_fields_are_not_propagated() -> None:
    params: dict[str, Any] = {
        "error": {
            "message": "auth rejected",
            "codexErrorInfo": {
                "token": "tok-abc",
                "Authorization": "Bearer sk-live-xyz",
                "api_key": "key-123",
                "secret": "s3cr3t",
                "cookie": "session=deadbeef",
                "reason": "upstream rejected",
            },
            "additionalDetails": "token=tok-abc",
        },
        "token": "top-level-token",
    }
    detail = _error_detail(params)

    blob = _blob(detail)
    for leaked in (
        "tok-abc",
        "sk-live-xyz",
        "key-123",
        "s3cr3t",
        "deadbeef",
        "top-level-token",
    ):
        assert leaked not in blob
    # Structural, non-sensitive data survives.
    assert detail["upstream_error_info"] == {"reason": "upstream rejected"}
    assert detail["upstream_message"] == "auth rejected"
    # The credential embedded in free-form text is redacted, not the whole field.
    assert detail["additional_details"] == "token=[redacted]"


def test_credential_shapes_in_free_text_are_redacted() -> None:
    """Credentials appear as VALUES in diagnostic text, not as key names."""
    detail = _error_detail(
        {
            "error": {
                "message": (
                    "GET /v1/responses?token=abc123&x=1 "
                    "Authorization: Bearer sk-live-999 "
                    "\"api_key\": \"key-777\""
                ),
                "additionalDetails": "cookie: session=deadbeef",
            }
        }
    )
    for leaked in ("abc123", "sk-live-999", "key-777", "deadbeef"):
        assert leaked not in _blob(detail)
    assert "[redacted]" in detail["upstream_message"]
    assert "[redacted]" in detail["additional_details"]


def test_benign_wording_is_not_destroyed_by_redaction() -> None:
    """No false positives: ordinary phrasing must survive intact."""
    message = (
        "unexpected status 503 Service Unavailable: 所有供应商已熔断，无可用渠道, "
        "url: http://127.0.0.1:15721/v1/responses"
    )
    detail = _error_detail({"error": {"message": message}})
    assert detail["upstream_message"] == message
    assert _error_detail({"error": {"message": "token bucket exhausted"}})[
        "upstream_message"
    ] == "token bucket exhausted"


def test_sensitive_keys_are_scrubbed_at_every_depth() -> None:
    detail = _error_detail(
        {
            "error": {
                "message": "nested",
                "codexErrorInfo": {
                    "outer": [
                        {"inner_token": "deep-secret", "status": 503},
                        {"SECRET_VALUE": "also-hidden", "httpStatusCode": 503},
                    ]
                },
            }
        }
    )
    blob = _blob(detail)
    assert "deep-secret" not in blob
    assert "also-hidden" not in blob
    assert detail["upstream_error_info"]["outer"][0] == {"status": 503}
    assert detail["upstream_error_info"]["outer"][1] == {"httpStatusCode": 503}


def test_no_sensitive_key_name_is_echoed_even_when_value_is_benign() -> None:
    detail = _error_detail(
        {"error": {"message": "m", "codexErrorInfo": {"apiKey": "none"}}}
    )
    assert detail["upstream_error_info"] == {}


# --------------------------------------------------------------------------
# retryable is only taken from the real notification when present
# --------------------------------------------------------------------------


def test_retryable_absent_when_notification_has_no_will_retry() -> None:
    assert "retryable" not in _error_detail({"error": {"message": "m"}})


def test_retryable_is_coerced_to_bool() -> None:
    assert _error_detail({"error": {"message": "m"}, "willRetry": True})[
        "retryable"
    ] is True


def test_envelope_fields_outside_error_are_not_dumped() -> None:
    """Only params.error is inspected; the envelope is never echoed wholesale."""
    detail = _error_detail(
        {
            "error": {"message": "m"},
            "threadId": "thr-should-not-appear-literal",
            "turnId": "turn-should-not-appear-literal",
            "unexpectedEnvelopeKey": "surprise",
        }
    )
    blob = _blob(detail)
    assert "thr-should-not-appear-literal" not in blob
    assert "surprise" not in blob


def test_unserialisable_values_degrade_to_repr_instead_of_raising() -> None:
    detail = _error_detail(
        {"error": {"message": "m", "codexErrorInfo": {"obj": object()}}}
    )
    json.dumps(detail)
    assert isinstance(detail["upstream_error_info"]["obj"], str)


# --------------------------------------------------------------------------
# end-to-end through the notification path (no model, no process)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notification_path_emits_event_carrying_diagnostics(
    tmp_path: Path,
) -> None:
    """End-to-end through the real notification handler (no model, no process)."""
    session = CodexAppServerSession(workspace_path=tmp_path)
    session.current_session_id = "thr-1"
    event = await session._notification_event(
        _notification(
            {
                "message": "unexpected status 503 Service Unavailable",
                "codexErrorInfo": {
                    "responseStreamDisconnected": {"httpStatusCode": 503}
                },
            },
            willRetry=True,
            threadId="thr-1",
            turnId="turn-1",
        )
    )

    assert event is not None
    assert event["type"] == "error"
    assert event["error"]["message"] == GENERIC
    assert "503" in event["error"]["upstream_message"]
    assert event["error"]["retryable"] is True
    # Identity fields still travel with the event.
    assert event["thread_id"] == "thr-1"
    assert event["turn_id"] == "turn-1"
    json.dumps(event)

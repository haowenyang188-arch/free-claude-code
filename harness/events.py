"""Pure projections for DeepSeek Harness SDK notifications.

The Harness SDK deliberately leaves the ``session.event`` vocabulary open for
plugins.  This module therefore keeps the wire payload intact while exposing a
small, stable envelope for the Python host.  It has no process, logging, or
framework dependencies; callers decide whether an envelope is sent to an SSE
client, persisted, or inspected locally.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, TypedDict, cast

SOURCE = "deepseek_harness"

SESSION_EVENT_METHOD = "session.event"
SESSION_STATUS_METHOD = "session.status"
SUBAGENT_STARTED_METHOD = "subagent.started"
SUBAGENT_FINISHED_METHOD = "subagent.finished"

KNOWN_NOTIFICATION_METHODS = frozenset(
    {
        SESSION_EVENT_METHOD,
        SESSION_STATUS_METHOD,
        SUBAGENT_STARTED_METHOD,
        SUBAGENT_FINISHED_METHOD,
    }
)

_SSE_EVENT_NAMES: dict[str, str] = {
    "session_event": "harness_session_event",
    "session_status": "harness_session_status",
    "subagent_started": "harness_subagent_started",
    "subagent_finished": "harness_subagent_finished",
    "unknown": "harness_unknown",
}
_VALID_SESSION_STATUSES = frozenset({"idle", "running"})
_VALID_SUBAGENT_STATUSES = frozenset({"ok", "error"})
_MISSING = object()

type JsonValue = (
    str | int | float | bool | None | dict[str, "JsonValue"] | list["JsonValue"]
)


class HarnessEventEnvelope(TypedDict, total=False):
    """Dictionary contract shared by the bridge and downstream adapters.

    The common keys are always present in envelopes returned by
    :func:`project_notification`.  Method-specific keys are optional and are
    listed here to make the contract discoverable without imposing a runtime
    validation dependency.
    """

    type: str
    source: str
    method: str
    session_id: str | None
    runtime_session_id: str | None
    payload: Any
    raw: Any
    event: dict[str, Any] | None
    raw_event: Any
    event_type: str | None
    status: str | None
    parent_session_id: str | None
    child_session_id: str | None
    provider: str | None
    agent_id: str | None
    stop_reason: Any
    last_assistant_message: list[Any] | None


class SSEFrame(TypedDict):
    """Structured representation of one serialized server-sent event."""

    event: str
    data: dict[str, Any]


def project_notification(
    method: str | Mapping[str, Any],
    params: Any = _MISSING,
    *,
    session_id: str | None = None,
) -> HarnessEventEnvelope:
    """Project one SDK notification into a stable internal envelope.

    ``method`` may be the JSON-RPC method string or a complete notification
    mapping (with ``method`` and ``params``/``payload`` keys).  The function is
    intentionally permissive at this boundary: malformed runtime data is
    represented as ``None`` in normalized fields and retained under ``raw``
    instead of raising or being silently discarded.

    Args:
        method: SDK notification method or a complete notification object.
        params: JSON-RPC ``params`` payload.  Omit it when ``method`` is a
            complete notification object.
        session_id: Optional host-side fallback when the runtime payload does
            not carry a session id.
    """
    if isinstance(method, Mapping):
        notification = cast(Mapping[str, Any], method)
        method_value = notification.get("method")
        if params is _MISSING:
            params = notification.get("params", notification.get("payload"))
    else:
        method_value = method
        if params is _MISSING:
            params = None

    method_name = _method_name(method_value)
    fallback_session_id = _identifier(session_id)
    raw_payload = _clone_json(params)
    payload_session_id = _payload_session_id(params)
    default_session_id = payload_session_id or fallback_session_id
    envelope: HarnessEventEnvelope = {
        "type": "unknown",
        "source": SOURCE,
        "method": method_name,
        "session_id": default_session_id,
        "runtime_session_id": default_session_id,
        "payload": raw_payload,
        "raw": raw_payload,
    }

    if method_name == SESSION_EVENT_METHOD:
        return _project_session_event(envelope, params, fallback_session_id)
    if method_name == SESSION_STATUS_METHOD:
        return _project_session_status(envelope, params, fallback_session_id)
    if method_name == SUBAGENT_STARTED_METHOD:
        return _project_subagent_started(envelope, params, fallback_session_id)
    if method_name == SUBAGENT_FINISHED_METHOD:
        return _project_subagent_finished(envelope, params, fallback_session_id)
    return envelope


def project_sse(envelope: Mapping[str, Any] | Any) -> list[str]:
    """Serialize an envelope as one conservative, custom SSE frame.

    The DSH protocol does not define an Anthropic message lifecycle.  Emitting
    ``message_start``/``message_stop`` here would manufacture state that may
    not exist, especially when multiple prompts share a session.  Instead,
    each notification is projected to a namespaced ``harness_*`` event and the
    complete envelope remains in its JSON data.  A future adapter can consume
    these frames and build a protocol-specific stream with its own lifecycle.
    """
    frame = _frame_from_envelope(envelope)
    return [serialize_sse(frame["event"], frame["data"])]


def project_sse_frames(envelope: Mapping[str, Any] | Any) -> list[SSEFrame]:
    """Return the structured SSE frame used by :func:`project_sse`."""
    return [_frame_from_envelope(envelope)]


def serialize_sse(
    event: str,
    data: Mapping[str, Any] | Any,
    *,
    event_id: str | None = None,
) -> str:
    """Serialize one JSON SSE frame without permitting line injection."""
    event_name = _safe_event_name(event)
    payload = _clone_json(data)
    if not isinstance(payload, Mapping):
        payload = {"value": payload}
    # ``_clone_json`` removes non-finite numbers, so this is deterministic and
    # safe for arbitrary values received from a malformed plugin.
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    lines = [f"event: {event_name}", f"data: {encoded}"]
    safe_id = _identifier(event_id)
    if safe_id is not None:
        lines.insert(1, f"id: {_safe_sse_line(safe_id)}")
    return "\n".join(lines) + "\n\n"


def safe_log_context(envelope: Mapping[str, Any] | Any) -> dict[str, str | None]:
    """Return metadata suitable for logs without copying runtime payloads.

    Harness events can contain prompts, credentials, file contents, and tool
    arguments.  This helper deliberately returns only routing metadata; it is
    safe for callers to pass to a logger without accidentally serializing the
    raw event.
    """
    if not isinstance(envelope, Mapping):
        return {"source": SOURCE, "type": "unknown", "method": None}

    context: dict[str, str | None] = {
        "source": _identifier(envelope.get("source")) or SOURCE,
        "type": _identifier(envelope.get("type")) or "unknown",
        "method": _identifier(envelope.get("method")),
        "session_id": _identifier(envelope.get("session_id")),
        "runtime_session_id": _identifier(envelope.get("runtime_session_id")),
        "event_type": _identifier(envelope.get("event_type")),
        "status": _identifier(envelope.get("status")),
    }
    return context


def _project_session_event(
    envelope: HarnessEventEnvelope,
    params: Any,
    fallback_session_id: str | None,
) -> HarnessEventEnvelope:
    envelope["type"] = "session_event"
    if not isinstance(params, Mapping):
        envelope.update(
            {"event": None, "raw_event": envelope["raw"], "event_type": None}
        )
        return envelope

    runtime_session_id = _identifier(params.get("sessionId", params.get("session_id")))
    target = runtime_session_id or fallback_session_id
    envelope["session_id"] = target
    envelope["runtime_session_id"] = target

    raw_event = _clone_json(params.get("event"))
    envelope["raw_event"] = raw_event
    if not isinstance(params.get("event"), Mapping):
        envelope.update({"event": None, "event_type": None})
        return envelope

    event = _clone_json(params["event"])
    # The input came from a Mapping, so a JSON object remains a dict after the
    # defensive clone.  Keep a type guard for malformed custom Mapping values.
    if not isinstance(event, dict):
        envelope.update({"event": None, "event_type": None})
        return envelope
    envelope["event"] = event
    envelope["event_type"] = _identifier(params["event"].get("type"))
    return envelope


def _project_session_status(
    envelope: HarnessEventEnvelope,
    params: Any,
    fallback_session_id: str | None,
) -> HarnessEventEnvelope:
    envelope["type"] = "session_status"
    if not isinstance(params, Mapping):
        envelope["status"] = None
        return envelope

    runtime_session_id = _identifier(params.get("sessionId", params.get("session_id")))
    target = runtime_session_id or fallback_session_id
    envelope["session_id"] = target
    envelope["runtime_session_id"] = target
    status = _identifier(params.get("status"))
    envelope["status"] = status if status in _VALID_SESSION_STATUSES else None
    return envelope


def _project_subagent_started(
    envelope: HarnessEventEnvelope,
    params: Any,
    fallback_session_id: str | None,
) -> HarnessEventEnvelope:
    envelope["type"] = "subagent_started"
    parent, child = _subagent_ids(params)
    target_parent = parent or fallback_session_id
    envelope.update(
        {
            "session_id": target_parent,
            "runtime_session_id": child or target_parent,
            "parent_session_id": parent,
            "child_session_id": child,
        }
    )
    return envelope


def _project_subagent_finished(
    envelope: HarnessEventEnvelope,
    params: Any,
    fallback_session_id: str | None,
) -> HarnessEventEnvelope:
    envelope["type"] = "subagent_finished"
    if not isinstance(params, Mapping):
        envelope.update(
            {
                "session_id": fallback_session_id,
                "runtime_session_id": fallback_session_id,
                "parent_session_id": None,
                "child_session_id": None,
                "provider": None,
                "agent_id": None,
                "status": None,
                "stop_reason": None,
                "last_assistant_message": None,
            }
        )
        return envelope

    parent, child = _subagent_ids(params)
    target_parent = parent or fallback_session_id
    status = _identifier(params.get("status"))
    stop_reason = params.get("stopReason", params.get("stop_reason"))
    if isinstance(stop_reason, Mapping):
        normalized_stop_reason = _clone_json(stop_reason)
    elif isinstance(stop_reason, str) and stop_reason.strip():
        normalized_stop_reason = stop_reason.strip()
    else:
        normalized_stop_reason = None

    assistant_message = params.get(
        "lastAssistantMessage", params.get("last_assistant_message")
    )
    normalized_assistant_message = (
        _clone_json(assistant_message) if isinstance(assistant_message, list) else None
    )
    if not isinstance(normalized_assistant_message, list):
        normalized_assistant_message = None

    envelope.update(
        {
            "session_id": target_parent,
            "runtime_session_id": child or target_parent,
            "parent_session_id": parent,
            "child_session_id": child,
            "provider": _identifier(params.get("provider")),
            "agent_id": _identifier(params.get("agentId", params.get("agent_id"))),
            "status": status if status in _VALID_SUBAGENT_STATUSES else None,
            "stop_reason": normalized_stop_reason,
            "last_assistant_message": normalized_assistant_message,
        }
    )
    return envelope


def _subagent_ids(params: Any) -> tuple[str | None, str | None]:
    if not isinstance(params, Mapping):
        return None, None
    return (
        _identifier(params.get("parentSessionId", params.get("parent_session_id"))),
        _identifier(params.get("childSessionId", params.get("child_session_id"))),
    )


def _payload_session_id(params: Any) -> str | None:
    """Extract the common target id for unknown notification methods."""
    if not isinstance(params, Mapping):
        return None
    return _identifier(params.get("sessionId", params.get("session_id")))


def _frame_from_envelope(envelope: Mapping[str, Any] | Any) -> SSEFrame:
    if not isinstance(envelope, Mapping):
        data: dict[str, Any] = {
            "type": "unknown",
            "source": SOURCE,
            "method": "unknown",
            "session_id": None,
            "runtime_session_id": None,
            "payload": _clone_json(envelope),
            "raw": _clone_json(envelope),
        }
    else:
        cloned = _clone_json(envelope)
        data = cloned if isinstance(cloned, dict) else {}
        if not isinstance(data, dict):
            data = {
                "type": "unknown",
                "source": SOURCE,
                "method": "unknown",
                "session_id": None,
                "runtime_session_id": None,
                "payload": None,
                "raw": None,
            }
        data.setdefault("type", "unknown")
        data.setdefault("source", SOURCE)
        data.setdefault("method", "unknown")
        data.setdefault("session_id", None)
        data.setdefault("runtime_session_id", None)
    event_type = data.get("type")
    event_name = _SSE_EVENT_NAMES.get(
        event_type if isinstance(event_type, str) else "unknown", "harness_unknown"
    )
    return {"event": event_name, "data": data}


def _method_name(value: Any) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return "unknown"


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _clone_json(value: Any) -> Any:
    """Copy JSON values and replace malformed values with inert markers."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = key if isinstance(key, str) else f"<{type(key).__name__}>"
            result[safe_key] = _clone_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_clone_json(item) for item in value]
    return {"__non_json_type__": type(value).__name__}


def _safe_event_name(value: Any) -> str:
    raw = _identifier(value) or "harness_unknown"
    chars = [char if (char.isalnum() or char in "_.:-") else "_" for char in raw]
    return "".join(chars) or "harness_unknown"


def _safe_sse_line(value: str) -> str:
    return value.replace("\r", "_").replace("\n", "_")

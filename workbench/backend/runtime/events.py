"""Durable, ordered events for workbench runs.

The event log is intentionally small and file-backed for the local-first
workbench.  A later database implementation can preserve the same envelope
and replay contract.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from providers.common.identity import RuntimeIdentity

_REDACTED = "[redacted]"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "api_token",
    "access_token",
    "auth_token",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "secret",
)
_SENSITIVE_KEY_NAMES = {
    "token",
    "id_token",
    "refresh_token",
    "session_token",
    "bearer_token",
}


class EventLogError(RuntimeError):
    """Raised when a persisted event log cannot be loaded or written."""


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """A replayable event emitted by an agent run."""

    id: str
    run_id: str
    sequence: int
    event_type: str
    timestamp: datetime
    backend: str
    payload: dict[str, Any]
    session_id: str | None = None
    runtime_kind: str | None = None
    agent_profile_id: str | None = None
    step_id: str | None = None
    artifact_ids: list[str] | None = None
    generation: str | None = None
    # Provider-neutral execution identity.  These fields intentionally stay
    # outside ``payload`` so correlation never requires copying provider data.
    provider: str | None = None
    runtime_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    approval_id: str | None = None
    one_shot_id: str | None = None
    agent_id: str | None = None
    tool_id: str | None = None
    call_id: str | None = None
    message_id: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        """Return a JSON-compatible mapping for APIs and persistence."""
        identity = RuntimeIdentity(
            provider=self.provider,
            runtime_id=self.runtime_id,
            session_id=self.session_id,
            generation=self.generation,
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            item_id=self.item_id,
            approval_id=self.approval_id,
            one_shot_id=self.one_shot_id,
            agent_id=self.agent_id,
            tool_id=self.tool_id,
            call_id=self.call_id,
            message_id=self.message_id,
            run_id=self.run_id,
        ).to_mapping(include_unknown=False)
        return {
            "id": self.id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "backend": self.backend,
            "payload": _redact(self.payload),
            "session_id": self.session_id,
            "runtime_kind": self.runtime_kind,
            "agent_profile_id": self.agent_profile_id,
            "step_id": self.step_id,
            "artifact_ids": self.artifact_ids,
            "generation": self.generation,
            "provider": self.provider,
            "runtime_id": self.runtime_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "approval_id": self.approval_id,
            "one_shot_id": self.one_shot_id,
            "agent_id": self.agent_id,
            "tool_id": self.tool_id,
            "call_id": self.call_id,
            "message_id": self.message_id,
            "identity": identity,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EventEnvelope:
        """Reconstruct an envelope loaded from JSONL."""
        try:
            event_id = _required_text(value["id"], "id")
            run_id = _required_text(value["run_id"], "run_id")
            sequence = value["sequence"]
            event_type = _required_text(value["event_type"], "event_type")
            backend = _required_text(value["backend"], "backend")
            timestamp = datetime.fromisoformat(
                _required_text(value["timestamp"], "timestamp")
            )
            payload = value["payload"]
            if not isinstance(payload, Mapping):
                raise TypeError("payload must be an object")
            raw_identity = value.get("identity")
            if raw_identity is not None and not isinstance(raw_identity, Mapping):
                raise TypeError("identity must be an object")
            identity_source = raw_identity if isinstance(raw_identity, Mapping) else {}

            def identity_field(field: str) -> str | None:
                candidate = identity_source.get(field)
                if candidate is None:
                    candidate = value.get(field)
                return (
                    _identity_text(candidate, field)
                    if candidate is not None
                    else None
                )

            session_id = identity_field("session_id")

            # Extended fields (optional, for backward compatibility)
            runtime_kind = value.get("runtime_kind")
            if runtime_kind is not None:
                runtime_kind = _required_text(runtime_kind, "runtime_kind")

            agent_profile_id = value.get("agent_profile_id")
            if agent_profile_id is not None:
                agent_profile_id = _required_text(agent_profile_id, "agent_profile_id")

            step_id = value.get("step_id")
            if step_id is not None:
                step_id = _required_text(step_id, "step_id")

            artifact_ids = value.get("artifact_ids")
            if artifact_ids is not None:
                if not isinstance(artifact_ids, list):
                    raise TypeError("artifact_ids must be a list")
                artifact_ids = [
                    _required_text(aid, "artifact_id") for aid in artifact_ids
                ]

            generation = identity_field("generation")
            provider = identity_field("provider")
            runtime_id = identity_field("runtime_id")
            thread_id = identity_field("thread_id")
            turn_id = identity_field("turn_id")
            item_id = identity_field("item_id")
            approval_id = identity_field("approval_id")
            one_shot_id = identity_field("one_shot_id")
            agent_id = identity_field("agent_id")
            tool_id = identity_field("tool_id")
            call_id = identity_field("call_id")
            message_id = identity_field("message_id")
        except (KeyError, TypeError, ValueError) as exc:
            raise EventLogError("invalid event envelope") from exc

        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise EventLogError("event sequence must be a positive integer")

        return cls(
            id=event_id,
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            timestamp=timestamp,
            backend=backend,
            payload=dict(_redact(payload)),
            session_id=session_id,
            runtime_kind=runtime_kind,
            agent_profile_id=agent_profile_id,
            step_id=step_id,
            artifact_ids=artifact_ids,
            generation=generation,
            provider=provider,
            runtime_id=runtime_id,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            approval_id=approval_id,
            one_shot_id=one_shot_id,
            agent_id=agent_id,
            tool_id=tool_id,
            call_id=call_id,
            message_id=message_id,
        )


class EventLog:
    """Append and replay run events from a newline-delimited JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = RLock()
        self._events: dict[str, list[EventEnvelope]] = {}
        self._sequences: dict[str, int] = {}
        self._load()

    def append(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None,
        backend: str,
        session_id: str | None = None,
        runtime_kind: str | None = None,
        agent_profile_id: str | None = None,
        step_id: str | None = None,
        artifact_ids: list[str] | None = None,
        generation: str | None = None,
        identity: RuntimeIdentity | None = None,
        provider: str | None = None,
        runtime_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        approval_id: str | None = None,
        one_shot_id: str | None = None,
        agent_id: str | None = None,
        tool_id: str | None = None,
        call_id: str | None = None,
        message_id: str | None = None,
    ) -> EventEnvelope:
        """Append one event and return its assigned sequence."""
        run_id = _required_text(run_id, "run_id")
        event_type = _required_text(event_type, "event_type")
        backend = _required_text(backend, "backend")
        if session_id is not None:
            session_id = _required_text(session_id, "session_id")
        if runtime_kind is not None:
            runtime_kind = _required_text(runtime_kind, "runtime_kind")
        if agent_profile_id is not None:
            agent_profile_id = _required_text(agent_profile_id, "agent_profile_id")
        if step_id is not None:
            step_id = _required_text(step_id, "step_id")
        if artifact_ids is not None:
            if not isinstance(artifact_ids, list):
                raise TypeError("artifact_ids must be a list")
            artifact_ids = [_required_text(aid, "artifact_id") for aid in artifact_ids]
        if generation is not None:
            generation = _required_text(generation, "generation")
        if identity is not None and not isinstance(identity, RuntimeIdentity):
            raise TypeError("identity must be a RuntimeIdentity")
        if identity is not None and identity.run_id not in (None, run_id):
            raise ValueError("identity run_id does not match event run_id")
        identity_values: dict[str, str | None] = {
            field: None
            for field in (
                "provider",
                "runtime_id",
                "thread_id",
                "turn_id",
                "item_id",
                "approval_id",
                "one_shot_id",
                "agent_id",
                "tool_id",
                "call_id",
                "message_id",
            )
        }
        if identity is not None:
            for field, value in identity.to_mapping(include_unknown=False).items():
                if field in identity_values:
                    identity_values[field] = value
        # Explicit identity is authoritative.  Legacy flat arguments are only
        # compatibility fallbacks and must never overwrite a provider identity
        # with their default ``None`` values (or with stale adapter metadata).
        legacy_values = {
            "provider": provider,
            "runtime_id": runtime_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "item_id": item_id,
            "approval_id": approval_id,
            "one_shot_id": one_shot_id,
            "agent_id": agent_id,
            "tool_id": tool_id,
            "call_id": call_id,
            "message_id": message_id,
        }
        for field, value in legacy_values.items():
            if value is not None and identity_values[field] is None:
                identity_values[field] = _identity_text(value, field)
        for field, value in tuple(identity_values.items()):
            if value is not None:
                identity_values[field] = _identity_text(value, field)
        if identity is not None and identity.session_id is not None:
            session_id = identity.session_id
        if session_id is not None:
            session_id = _identity_text(session_id, "session_id")
        if identity is not None and identity.generation is not None:
            generation = identity.generation
        if generation is not None:
            generation = _identity_text(generation, "generation")
        if payload is not None and not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")

        with self._lock:
            sequence = self._sequences.get(run_id, 0) + 1
            event = EventEnvelope(
                id=str(uuid.uuid4()),
                run_id=run_id,
                sequence=sequence,
                event_type=event_type,
                timestamp=datetime.now(UTC),
                backend=backend,
                payload=dict(_redact(payload or {})),
                session_id=session_id,
                runtime_kind=runtime_kind,
                agent_profile_id=agent_profile_id,
                step_id=step_id,
                artifact_ids=artifact_ids,
                generation=generation,
                provider=identity_values["provider"],
                runtime_id=identity_values["runtime_id"],
                thread_id=identity_values["thread_id"],
                turn_id=identity_values["turn_id"],
                item_id=identity_values["item_id"],
                approval_id=identity_values["approval_id"],
                one_shot_id=identity_values["one_shot_id"],
                agent_id=identity_values["agent_id"],
                tool_id=identity_values["tool_id"],
                call_id=identity_values["call_id"],
                message_id=identity_values["message_id"],
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event.to_mapping(), ensure_ascii=False))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise EventLogError(f"unable to append event log: {self.path}") from exc
            self._events.setdefault(run_id, []).append(event)
            self._sequences[run_id] = sequence
            return event

    def replay(self, run_id: str, *, after: int = 0) -> list[EventEnvelope]:
        """Return events for ``run_id`` after an optional sequence cursor."""
        run_id = _required_text(run_id, "run_id")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative integer")
        with self._lock:
            return [
                event
                for event in self._events.get(run_id, [])
                if event.sequence > after
            ]

    def _load(self) -> None:
        if not self.path.exists():
            return
        if not self.path.is_file():
            raise EventLogError(f"event log path is not a file: {self.path}")
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                        if not isinstance(raw, Mapping):
                            raise TypeError("event must be an object")
                        event = EventEnvelope.from_mapping(raw)
                    except (json.JSONDecodeError, TypeError, EventLogError) as exc:
                        raise EventLogError(
                            f"invalid event at {self.path}:{line_number}"
                        ) from exc
                    previous = self._sequences.get(event.run_id, 0)
                    if event.sequence <= previous:
                        raise EventLogError(
                            f"event sequence is not increasing for run {event.run_id}"
                        )
                    self._events.setdefault(event.run_id, []).append(event)
                    self._sequences[event.run_id] = event.sequence
        except OSError as exc:
            raise EventLogError(f"unable to read event log: {self.path}") from exc


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field} must be a non-empty string")
    return value.strip()


def _identity_text(value: object, field: str) -> str:
    """Validate a bounded lifecycle identifier without accepting controls."""
    normalized = _required_text(value, field)
    if len(normalized) > 512 or any(ord(char) < 0x20 for char in normalized):
        raise TypeError(f"{field} contains invalid identifier text")
    return normalized


def redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


_redact = redact


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEY_NAMES or any(
        part in normalized for part in _SENSITIVE_KEY_PARTS
    )

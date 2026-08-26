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

    def to_mapping(self) -> dict[str, Any]:
        """Return a JSON-compatible mapping for APIs and persistence."""
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
            session_id = value.get("session_id")
            if session_id is not None:
                session_id = _required_text(session_id, "session_id")

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

            generation = value.get("generation")
            if generation is not None:
                generation = _required_text(generation, "generation")
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

"""Repository seam for the local JSON/JSONL workflow store."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import BaseModel

from ..runtime.events import redact


class WorkflowStoreError(RuntimeError):
    """Raised when workflow state or events cannot be persisted safely."""


class StoredEvent(BaseModel):
    id: str
    stream_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any] = {}
    occurred_at: datetime


class JsonWorkflowStore:
    """Persist domain snapshots and replayable events behind one interface."""

    def __init__(self, state_path: str | Path, event_path: str | Path) -> None:
        self.state_path = Path(state_path).expanduser()
        self.event_path = Path(event_path).expanduser()
        self._lock = RLock()
        self._events: dict[str, list[StoredEvent]] = {}
        self._sequences: dict[str, int] = {}
        self._load_events()

    def save_entity(
        self, collection: str, entity: BaseModel | Mapping[str, Any]
    ) -> None:
        if not collection or collection.startswith("_"):
            raise WorkflowStoreError("collection must be a public non-empty name")
        mapping = (
            entity.model_dump(mode="json")
            if isinstance(entity, BaseModel)
            else dict(entity)
        )
        entity_id = mapping.get("id")
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise WorkflowStoreError("stored entity requires a non-empty id")
        with self._lock:
            document = self._load_state()
            values = document.setdefault(collection, [])
            if not isinstance(values, list):
                raise WorkflowStoreError(
                    f"state collection is not a list: {collection}"
                )
            values[:] = [item for item in values if item.get("id") != entity_id]
            values.append(redact(mapping))
            self._save_state(document)

    def get_entity(self, collection: str, entity_id: str) -> dict[str, Any]:
        document = self._load_state()
        for item in document.get(collection, []):
            if item.get("id") == entity_id:
                return dict(item)
        raise KeyError(f"{collection}/{entity_id}")

    def list_entities(self, collection: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._load_state().get(collection, [])]

    def append_event(
        self,
        *,
        stream_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> StoredEvent:
        if not stream_id.strip() or not event_type.strip():
            raise WorkflowStoreError("stream_id and event_type are required")
        with self._lock:
            sequence = self._sequences.get(stream_id, 0) + 1
            event = StoredEvent(
                id=str(uuid.uuid4()),
                stream_id=stream_id,
                sequence=sequence,
                event_type=event_type,
                payload=dict(redact(payload or {})),
                occurred_at=datetime.now(UTC),
            )
            self.event_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.event_path.open("a", encoding="utf-8") as stream:
                    stream.write(event.model_dump_json())
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise WorkflowStoreError(
                    f"unable to append events: {self.event_path}"
                ) from exc
            self._events.setdefault(stream_id, []).append(event)
            self._sequences[stream_id] = sequence
            return event

    def replay_events(self, stream_id: str, *, after: int = 0) -> list[StoredEvent]:
        if after < 0:
            raise ValueError("after must be non-negative")
        return [
            event for event in self._events.get(stream_id, []) if event.sequence > after
        ]

    def _load_state(self) -> dict[str, list[dict[str, Any]]]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowStoreError(
                f"unable to read state: {self.state_path}"
            ) from exc
        if not isinstance(raw, dict):
            raise WorkflowStoreError("workflow state must be an object")
        return raw

    def _save_state(self, document: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_path.parent,
                prefix=f".{self.state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = stream.name
                json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.state_path)
            temporary_path = None
        except (OSError, TypeError, ValueError) as exc:
            raise WorkflowStoreError(
                f"unable to write state: {self.state_path}"
            ) from exc
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    os.unlink(temporary_path)

    def _load_events(self) -> None:
        if not self.event_path.exists():
            return
        try:
            lines = self.event_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise WorkflowStoreError(
                f"unable to read events: {self.event_path}"
            ) from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = StoredEvent.model_validate_json(line)
            except ValueError as exc:
                raise WorkflowStoreError(
                    f"invalid event at line {line_number}"
                ) from exc
            previous = self._sequences.get(event.stream_id, 0)
            if event.sequence <= previous:
                raise WorkflowStoreError(
                    f"event sequence is not increasing: {event.stream_id}"
                )
            self._events.setdefault(event.stream_id, []).append(event)
            self._sequences[event.stream_id] = event.sequence

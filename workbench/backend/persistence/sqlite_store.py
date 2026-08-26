"""SQLite metadata persistence with the existing JSONL event contract."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import BaseModel

from ..runtime.events import EventEnvelope, EventLog, redact
from .store import StoredEvent, WorkflowStoreError

_COLLECTION = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class SQLiteWorkflowStore:
    """Store domain snapshots in SQLite and events in newline-delimited JSON."""

    def __init__(self, database_path: str | Path, event_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser()
        self.event_path = Path(event_path).expanduser()
        self._lock = RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize_schema()
        self.event_log = EventLog(self.event_path)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_entity(
        self, collection: str, entity: BaseModel | Mapping[str, Any]
    ) -> None:
        self._validate_collection(collection)
        mapping = (
            entity.model_dump(mode="json")
            if isinstance(entity, BaseModel)
            else dict(entity)
        )
        entity_id = mapping.get("id")
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise WorkflowStoreError("stored entity requires a non-empty id")
        payload = json.dumps(redact(mapping), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO entities(collection, entity_id, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(collection, entity_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (collection, entity_id, payload),
            )
            self._connection.commit()

    def get_entity(self, collection: str, entity_id: str) -> dict[str, Any]:
        self._validate_collection(collection)
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM entities WHERE collection = ? AND entity_id = ?",
                (collection, entity_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"{collection}/{entity_id}")
        return self._decode_payload(row["payload"])

    def list_entities(self, collection: str) -> list[dict[str, Any]]:
        self._validate_collection(collection)
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM entities WHERE collection = ? ORDER BY entity_id",
                (collection,),
            ).fetchall()
        return [self._decode_payload(row["payload"]) for row in rows]

    def append_event(
        self,
        *,
        stream_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        backend: str = "workbench",
        session_id: str | None = None,
    ) -> StoredEvent:
        event: EventEnvelope = self.event_log.append(
            run_id=stream_id,
            event_type=event_type,
            payload=payload,
            backend=backend,
            session_id=session_id,
        )
        return StoredEvent(
            id=event.id,
            stream_id=event.run_id,
            sequence=event.sequence,
            event_type=event.event_type,
            payload=dict(event.payload),
            occurred_at=event.timestamp,
        )

    def replay_events(self, stream_id: str, *, after: int = 0) -> list[StoredEvent]:
        return [
            StoredEvent(
                id=event.id,
                stream_id=event.run_id,
                sequence=event.sequence,
                event_type=event.event_type,
                payload=dict(event.payload),
                occurred_at=event.timestamp,
            )
            for event in self.event_log.replay(stream_id, after=after)
        ]

    def _initialize_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS entities (
                    collection TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (collection, entity_id)
                );
                CREATE INDEX IF NOT EXISTS idx_entities_collection
                    ON entities(collection);
                """
            )

    @staticmethod
    def _validate_collection(collection: str) -> None:
        if not _COLLECTION.fullmatch(collection):
            raise WorkflowStoreError("collection contains unsupported characters")

    @staticmethod
    def _decode_payload(payload: str) -> dict[str, Any]:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise WorkflowStoreError("stored entity payload is invalid JSON") from exc
        if not isinstance(value, dict):
            raise WorkflowStoreError("stored entity payload must be an object")
        return value

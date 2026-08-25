"""Atomic local snapshots for workbench task and run state."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import Any

from .events import redact


class StateStoreError(RuntimeError):
    """Raised when the workbench state snapshot is invalid or unavailable."""


class StateStore:
    """Persist task/run mappings as one atomically replaced JSON document."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = RLock()

    def load(self) -> dict[str, list[dict[str, Any]]]:
        """Load and validate the snapshot shape without mutating caller state."""
        if not self.path.exists():
            return {"tasks": [], "runs": []}
        if not self.path.is_file():
            raise StateStoreError(f"state path is not a file: {self.path}")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateStoreError(f"unable to read state: {self.path}") from exc
        if not isinstance(raw, Mapping):
            raise StateStoreError("state snapshot must be an object")
        result: dict[str, list[dict[str, Any]]] = {}
        for key in ("tasks", "runs"):
            values = raw.get(key, [])
            if not isinstance(values, list):
                raise StateStoreError(f"state field {key!r} must be a list")
            if any(not isinstance(item, Mapping) for item in values):
                raise StateStoreError(f"state field {key!r} contains a non-object")
            result[key] = [dict(redact(item)) for item in values]
        return result

    def save(
        self,
        tasks: Iterable[Mapping[str, Any]],
        runs: Iterable[Mapping[str, Any]],
    ) -> None:
        """Atomically write a redacted snapshot of task and run mappings."""
        document = {
            "version": 1,
            "tasks": [dict(redact(task)) for task in tasks],
            "runs": [dict(redact(run)) for run in runs],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary_path = stream.name
                    json.dump(
                        document, stream, ensure_ascii=False, separators=(",", ":")
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.path)
                temporary_path = None
            except (OSError, TypeError, ValueError) as exc:
                raise StateStoreError(f"unable to write state: {self.path}") from exc
            finally:
                if temporary_path is not None:
                    with suppress(OSError):
                        os.unlink(temporary_path)

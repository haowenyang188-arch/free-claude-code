"""Fail-closed checkpoint manifests for resumable CLI workflows.

This module stores execution identity, not arbitrary model state.  A checkpoint
is eligible for resume only when the runtime, session generation, workspace,
and policy still match exactly.  Side-effecting steps are deliberately excluded
so replay cannot silently repeat a write or external action.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime_registry import RuntimeBackend

SCHEMA_VERSION = 1
_MAX_TEXT_BYTES = 4096


class CheckpointError(RuntimeError):
    """Raised when a checkpoint cannot be trusted or persisted."""


@dataclass(frozen=True, slots=True)
class CheckpointManifest:
    """Identity required to resume one deterministic workflow step."""

    checkpoint_id: str
    run_id: str
    backend: str | RuntimeBackend
    runtime_version: str
    session_id: str
    generation: str
    workspace_digest: str
    policy_digest: str
    step_id: str
    input_digest: str
    side_effects_allowed: bool = False

    def __post_init__(self) -> None:
        fields = (
            "checkpoint_id",
            "run_id",
            "runtime_version",
            "session_id",
            "generation",
            "workspace_digest",
            "policy_digest",
            "step_id",
            "input_digest",
        )
        for field in fields:
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        try:
            backend = RuntimeBackend(self.backend)
        except ValueError as exc:
            raise CheckpointError("backend is not supported") from exc
        object.__setattr__(self, "backend", backend.value)
        if not isinstance(self.side_effects_allowed, bool):
            raise CheckpointError("side_effects_allowed must be a boolean")
        if self.side_effects_allowed:
            raise CheckpointError(
                "side_effects_allowed checkpoints cannot be resumed safely"
            )

    def to_mapping(self) -> dict[str, Any]:
        """Return the stable checkpoint schema used for hashing and storage."""
        return {
            "schema_version": SCHEMA_VERSION,
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "backend": self.backend,
            "runtime_version": self.runtime_version,
            "session_id": self.session_id,
            "generation": self.generation,
            "workspace_digest": self.workspace_digest,
            "policy_digest": self.policy_digest,
            "step_id": self.step_id,
            "input_digest": self.input_digest,
            "side_effects_allowed": self.side_effects_allowed,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CheckpointManifest:
        """Validate and reconstruct a checkpoint manifest."""
        if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
            raise CheckpointError("invalid checkpoint schema")
        expected = set(cls._field_names()) | {"schema_version"}
        if set(value) != expected:
            raise CheckpointError("invalid checkpoint fields")
        try:
            return cls(**{field: value[field] for field in cls._field_names()})
        except (KeyError, TypeError, ValueError, CheckpointError) as exc:
            if isinstance(exc, CheckpointError):
                raise
            raise CheckpointError("invalid checkpoint manifest") from exc

    @staticmethod
    def _field_names() -> tuple[str, ...]:
        return (
            "checkpoint_id",
            "run_id",
            "backend",
            "runtime_version",
            "session_id",
            "generation",
            "workspace_digest",
            "policy_digest",
            "step_id",
            "input_digest",
            "side_effects_allowed",
        )

    def is_compatible(
        self,
        *,
        backend: str | RuntimeBackend,
        runtime_version: str,
        session_id: str,
        generation: str,
        workspace_digest: str,
        policy_digest: str,
    ) -> bool:
        """Return whether the current execution identity can resume this step."""
        try:
            runtime = RuntimeBackend(backend).value
        except ValueError:
            return False
        return (
            self.backend == runtime
            and self.runtime_version == runtime_version
            and self.session_id == session_id
            and self.generation == generation
            and self.workspace_digest == workspace_digest
            and self.policy_digest == policy_digest
            and not self.side_effects_allowed
        )


class CheckpointStore:
    """Atomically persist one integrity-checked checkpoint manifest."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def save(self, manifest: CheckpointManifest) -> None:
        if not isinstance(manifest, CheckpointManifest):
            raise TypeError("manifest must be a CheckpointManifest")
        body = manifest.to_mapping()
        document = {"manifest": body, "sha256": _mapping_digest(body)}
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
                json.dump(document, stream, ensure_ascii=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except (OSError, TypeError, ValueError) as exc:
            raise CheckpointError(f"unable to write checkpoint: {self.path}") from exc
        finally:
            if temporary_path is not None:
                with suppress(OSError):
                    os.unlink(temporary_path)

    def load(self) -> CheckpointManifest:
        if not self.path.is_file():
            raise CheckpointError(f"checkpoint is missing: {self.path}")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError("invalid checkpoint document") from exc
        if not isinstance(document, Mapping) or set(document) != {"manifest", "sha256"}:
            raise CheckpointError("invalid checkpoint envelope")
        body = document.get("manifest")
        digest = document.get("sha256")
        if not isinstance(body, Mapping) or not isinstance(digest, str):
            raise CheckpointError("invalid checkpoint envelope")
        if digest != _mapping_digest(body):
            raise CheckpointError("checkpoint integrity check failed")
        try:
            return CheckpointManifest.from_mapping(body)
        except CheckpointError as exc:
            raise CheckpointError("invalid checkpoint manifest") from exc


def _mapping_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckpointError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if any(character in normalized for character in ("\x00", "\r", "\n")):
        raise CheckpointError(f"{field} must not contain control characters")
    if len(normalized.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise CheckpointError(f"{field} exceeds {_MAX_TEXT_BYTES} bytes")
    return normalized


__all__ = ["CheckpointError", "CheckpointManifest", "CheckpointStore"]

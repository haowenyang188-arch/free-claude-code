"""Immutable, content-addressed file storage for workflow artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..domain.models import Artifact


class ArtifactStoreError(RuntimeError):
    """Raised when an artifact cannot be safely stored or read."""


class FileArtifactStore:
    """Persist artifact payloads and manifests under a controlled root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, artifact: Artifact, content: str | bytes) -> Artifact:
        if artifact.accepted:
            raise ArtifactStoreError("accepted artifacts are immutable")
        payload = (
            content.encode("utf-8") if isinstance(content, str) else bytes(content)
        )
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = self._safe_id(artifact.id)
        directory = (self.root / artifact.type.value / artifact_id).resolve()
        if not directory.is_relative_to(self.root):
            raise ArtifactStoreError("artifact path escapes store root")
        if directory.exists():
            raise ArtifactStoreError(f"artifact already exists: {artifact.id}")
        directory.mkdir(parents=True, exist_ok=False)

        payload_name = (
            "payload.json" if artifact.payload_format == "json" else "payload"
        )
        payload_path = directory / payload_name
        manifest_path = directory / "manifest.json"
        stored = artifact.model_copy(
            update={
                "uri": str(payload_path.relative_to(self.root)),
                "sha256": digest,
            }
        )
        try:
            self._atomic_bytes(payload_path, payload)
            self._atomic_text(
                manifest_path,
                json.dumps(stored.model_dump(mode="json"), ensure_ascii=False, indent=2)
                + "\n",
            )
        except OSError as exc:
            self._remove_directory(directory)
            raise ArtifactStoreError(
                f"unable to store artifact: {artifact.id}"
            ) from exc
        return stored

    def get(self, artifact: Artifact) -> bytes:
        if not artifact.uri or not artifact.sha256:
            raise ArtifactStoreError("artifact has no persisted payload reference")
        path = (self.root / artifact.uri).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ArtifactStoreError("artifact payload is outside the store or missing")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != artifact.sha256:
            raise ArtifactStoreError("artifact checksum mismatch")
        return payload

    def get_manifest(self, artifact_type: str, artifact_id: str) -> dict[str, Any]:
        directory = (self.root / artifact_type / self._safe_id(artifact_id)).resolve()
        manifest = directory / "manifest.json"
        if not directory.is_relative_to(self.root) or not manifest.is_file():
            raise ArtifactStoreError("artifact manifest is missing")
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ArtifactStoreError("artifact manifest must be an object")
        return value

    @staticmethod
    def _safe_id(value: str) -> str:
        if not value or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in value
        ):
            raise ArtifactStoreError("artifact id contains unsupported characters")
        return value

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    @classmethod
    def _atomic_text(cls, path: Path, content: str) -> None:
        cls._atomic_bytes(path, content.encode("utf-8"))

    @staticmethod
    def _remove_directory(directory: Path) -> None:
        for path in sorted(directory.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
        directory.rmdir()

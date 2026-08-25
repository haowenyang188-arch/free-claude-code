"""Workspace boundary checks for agent runs."""

from __future__ import annotations

from pathlib import Path


class WorkspacePolicyError(ValueError):
    """Raised when a requested workspace violates the local policy."""


class WorkspacePolicy:
    """Resolve workspaces without allowing symlink or path escapes."""

    def __init__(self, root: str | Path) -> None:
        candidate = Path(root).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise WorkspacePolicyError("workspace root must exist") from exc
        if not resolved.is_dir():
            raise WorkspacePolicyError("workspace root must be a directory")
        self.root = resolved

    def resolve(self, workspace: str | Path) -> Path:
        """Resolve an existing directory inside the configured root."""
        candidate = Path(workspace).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise WorkspacePolicyError("workspace path must exist") from exc
        if not resolved.is_dir():
            raise WorkspacePolicyError("workspace path must be a directory")
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePolicyError(
                "workspace path must stay inside workspace root"
            ) from exc
        return resolved

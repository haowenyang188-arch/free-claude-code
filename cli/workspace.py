"""Workspace boundary checks for local agent processes."""

from __future__ import annotations

from pathlib import Path


class WorkspacePolicyError(ValueError):
    """Raised when a configured agent workspace is unsafe or unavailable."""


def resolve_workspace_root(
    configured: str | None = None, *, create: bool = False
) -> Path:
    """Return a canonical directory for agent execution.

    ``create`` is limited to a missing final directory whose parent already
    exists; callers cannot silently create an arbitrary directory tree.
    """
    candidate = Path(configured).expanduser() if configured else Path.cwd()
    if not candidate.exists():
        if not create or not candidate.parent.is_dir():
            raise WorkspacePolicyError(f"workspace does not exist: {candidate}")
        try:
            candidate.mkdir()
        except OSError as exc:
            raise WorkspacePolicyError(
                f"workspace cannot be created: {candidate}"
            ) from exc
    if not candidate.is_dir():
        raise WorkspacePolicyError(f"workspace is not a directory: {candidate}")
    return candidate.resolve()

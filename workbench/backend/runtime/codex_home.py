"""Prepare an isolated Codex state root for Workbench-owned sessions."""

from __future__ import annotations

import os
from pathlib import Path


class CodexHomeError(ValueError):
    """Raised when a Workbench Codex home cannot be prepared safely."""


def prepare_codex_home(
    target: str | Path,
    *,
    source: str | Path | None = None,
) -> Path:
    """Create a small state root without copying provider credentials.

    The target owns Codex's SQLite/history files.  Static configuration and
    authentication are linked from the user's existing Codex home so the
    provider setup stays native and secrets are not duplicated.
    """
    target_path = Path(target).expanduser()
    source_path = Path(
        source or os.environ.get("CODEX_HOME", Path.home() / ".codex")
    ).expanduser()
    try:
        source_path = source_path.resolve(strict=True)
    except OSError as exc:
        raise CodexHomeError("source Codex home is unavailable") from exc
    if not source_path.is_dir():
        raise CodexHomeError("source Codex home must be a directory")

    try:
        target_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_path = target_path.resolve(strict=True)
    except OSError as exc:
        raise CodexHomeError("Workbench Codex home cannot be created") from exc
    if not target_path.is_dir():
        raise CodexHomeError("Workbench Codex home must be a directory")
    if target_path == source_path:
        raise CodexHomeError("Workbench Codex home must be separate from source home")

    _link_static_file(source_path / "config.toml", target_path / "config.toml")
    auth_source = source_path / "auth.json"
    if auth_source.exists():
        _link_static_file(auth_source, target_path / "auth.json")
    return target_path


def _link_static_file(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            try:
                if target.resolve(strict=True) == source.resolve(strict=True):
                    return
            except OSError:
                pass
        if target.is_file():
            return
        raise CodexHomeError(f"Codex home entry is not a file: {target.name}")
    if not source.is_file():
        raise CodexHomeError(f"Codex source file is unavailable: {source.name}")
    try:
        target.symlink_to(source)
    except OSError as exc:
        raise CodexHomeError(f"cannot link Codex source file: {source.name}") from exc


__all__ = ["CodexHomeError", "prepare_codex_home"]

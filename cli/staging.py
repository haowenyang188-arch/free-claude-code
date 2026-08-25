"""Temporary workspace snapshots for explicit write approval."""

from __future__ import annotations

import difflib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


class StagingError(RuntimeError):
    """Raised when a staged workspace cannot be safely applied."""


@dataclass(frozen=True, slots=True)
class StagedChange:
    path: str
    before: bytes | None
    after: bytes | None


class StagedWorkspace:
    """Copy a workspace and apply only the approved file delta."""

    def __init__(
        self,
        source: Path,
        temp_root: Path,
        staged_path: Path,
        before: dict[str, bytes],
    ) -> None:
        self.source = source
        self._temp_root = temp_root
        self.path = staged_path
        self._before = before

    @classmethod
    def create(cls, source: str | Path) -> StagedWorkspace:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_dir():
            raise StagingError(f"staging source is not a directory: {source_path}")

        temp_root = Path(tempfile.mkdtemp(prefix="fcc-stage-"))
        staged_path = temp_root / source_path.name
        try:
            before = cls._read_files(source_path)
            staged_path.mkdir()
            for relative, content in before.items():
                destination = staged_path / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                source_stat = (source_path / relative).stat()
                os.chmod(destination, source_stat.st_mode & 0o777)
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
        return cls(source_path, temp_root, staged_path, before)

    def changes(self) -> list[StagedChange]:
        after = self._read_files(self.path)
        changes: list[StagedChange] = []
        for relative in sorted(set(self._before) | set(after)):
            before = self._before.get(relative)
            current = after.get(relative)
            if before != current:
                changes.append(StagedChange(relative, before, current))
        return changes

    def diff(self, *, max_chars: int = 20_000) -> str:
        chunks: list[str] = []
        for change in self.changes():
            if _is_text(change.before) and _is_text(change.after):
                before = (
                    (change.before or b"").decode("utf-8").splitlines(keepends=True)
                )
                after = (change.after or b"").decode("utf-8").splitlines(keepends=True)
                chunks.extend(
                    difflib.unified_diff(
                        before,
                        after,
                        fromfile=f"a/{change.path}",
                        tofile=f"b/{change.path}",
                    )
                )
            else:
                chunks.append(f"Binary files differ: {change.path}\n")
            if sum(map(len, chunks)) >= max_chars:
                break
        result = "".join(chunks)
        return result[:max_chars] + (
            "\n...[diff truncated]" if len(result) > max_chars else ""
        )

    def apply(self) -> list[StagedChange]:
        changes = self.changes()
        for change in changes:
            target = self.source / change.path
            _ensure_safe_target(self.source, target)
            current = target.read_bytes() if target.is_file() else None
            if current != change.before:
                raise StagingError(
                    f"workspace changed while awaiting approval: {change.path}"
                )

        applied: list[StagedChange] = []
        try:
            for change in changes:
                target = self.source / change.path
                if change.after is None:
                    if target.exists() or target.is_symlink():
                        if target.is_symlink() or not target.is_file():
                            raise StagingError(
                                f"refusing to remove non-file path: {change.path}"
                            )
                        target.unlink()
                    applied.append(change)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                _ensure_safe_target(self.source, target)
                temporary = target.with_name(f".{target.name}.fcc-tmp")
                try:
                    temporary.write_bytes(change.after)
                    mode_source = target if target.exists() else self.path / change.path
                    os.chmod(temporary, mode_source.stat().st_mode & 0o777)
                    os.replace(temporary, target)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                applied.append(change)
        except Exception as exc:
            try:
                for change in reversed(applied):
                    target = self.source / change.path
                    if change.before is None:
                        if target.exists() or target.is_symlink():
                            target.unlink()
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(change.before)
            except OSError as rollback_exc:
                raise StagingError("failed to roll back staged changes") from rollback_exc
            if isinstance(exc, StagingError):
                raise
            raise StagingError("failed to apply staged changes") from exc
        return changes

    def cleanup(self) -> None:
        shutil.rmtree(self._temp_root, ignore_errors=True)

    @staticmethod
    def _read_files(root: Path) -> dict[str, bytes]:
        if root.is_symlink():
            raise StagingError(f"symlink workspace is not allowed: {root}")
        files: dict[str, bytes] = {}
        for current_root, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name != ".git"]
            current = Path(current_root)
            directories[:] = [
                name for name in directories if not _is_sensitive_relative(Path(name))
            ]
            for directory in directories:
                if (current / directory).is_symlink():
                    raise StagingError(
                        f"symlink workspace entry is not allowed: {current / directory}"
                    )
            for filename in filenames:
                path = current / filename
                if path.is_symlink():
                    raise StagingError(
                        f"symlink workspace entry is not allowed: {path}"
                    )
                relative = path.relative_to(root)
                if _is_sensitive_relative(relative):
                    continue
                files[str(relative)] = path.read_bytes()
        return files


def _is_text(value: bytes | None) -> bool:
    if value is None:
        return True
    if b"\x00" in value:
        return False
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _ensure_safe_target(root: Path, target: Path) -> None:
    root = root.resolve()
    candidate = target.resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise StagingError(f"staged path escapes workspace: {target}")
    current = root
    for part in target.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise StagingError(f"symlink target is not allowed: {current}")


def _is_sensitive_relative(relative: Path) -> bool:
    """Keep credentials and private keys out of mobile approval diffs."""
    parts = relative.parts
    name = relative.name.lower()
    return (
        ".ssh" in {part.lower() for part in parts}
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )

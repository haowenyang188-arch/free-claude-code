"""Safely inspect and configure Codex GPT-5.6 Sol as a 1M default."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL_SLUG = "gpt-5.6-sol"
DESIRED_CONTEXT_WINDOW = 1_000_000
AUTO_COMPACT_TOKEN_LIMIT = 900_000
COMMAND_TIMEOUT_SECONDS = 15.0
CONFIG_MARKER_BEGIN = "# managed-by: fcc-codex-1m begin\n"
CONFIG_MARKER_END = "# managed-by: fcc-codex-1m end\n"
CATALOG_RELATIVE_PATH = Path("model-catalogs") / f"{MODEL_SLUG}-1m.json"
CATALOG_OWNER_RELATIVE_PATH = Path("model-catalogs") / f"{MODEL_SLUG}-1m.owner"
CATALOG_OWNER_CONTENT = "managed-by: fcc-codex-1m\n"
MANAGED_CONFIG_KEYS = (
    "model",
    "model_context_window",
    "model_auto_compact_token_limit",
    "model_catalog_json",
)


class Codex1MError(RuntimeError):
    """Base error for the Codex 1M helper."""


class ConfigurationBlocked(Codex1MError):
    """Raised when the target cannot be configured or verified."""


class ConfigurationOwnershipError(Codex1MError):
    """Raised when a target file contains user-owned conflicting content."""


class TargetResolutionError(Codex1MError):
    """Raised when the requested Codex installation cannot be identified."""


@dataclass(frozen=True)
class CodexTarget:
    """A concrete Codex executable and its configuration home."""

    kind: str
    executable: str
    home: Path


@dataclass(frozen=True)
class CommandResult:
    """Sanitized subprocess result boundary used by real and fake runners."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], dict[str, str], float], CommandResult]
ExecFn = Callable[[str, list[str], dict[str, str]], Any]


@dataclass(frozen=True)
class ManagedFileState:
    """Ownership and readiness state for one managed file."""

    exists: bool
    managed: bool
    configured: bool
    error: str | None = None


@dataclass(frozen=True)
class Codex1MStatus:
    """Requested, configured, and effective state for a Codex target."""

    target: CodexTarget
    version: str | None
    auth_mode: str
    config_path: Path
    catalog_path: Path
    catalog_owner_path: Path
    config_state: ManagedFileState
    catalog_state: ManagedFileState
    catalog_context_window: int | None
    catalog_max_context_window: int | None
    effective_context_window_percent: int | None
    effective_context_window: int | None
    eligible: bool
    ready: bool
    eligibility_blockers: tuple[str, ...]
    readiness_blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe status data without raw authentication output."""
        return {
            "target": self.target.kind,
            "codex_bin": self.target.executable,
            "codex_home": str(self.target.home),
            "version": self.version,
            "auth_mode": self.auth_mode,
            "model": MODEL_SLUG,
            "config_path": str(self.config_path),
            "catalog_path": str(self.catalog_path),
            "catalog_owner_path": str(self.catalog_owner_path),
            "config_exists": self.config_state.exists,
            "config_managed": self.config_state.managed,
            "catalog_exists": self.catalog_state.exists,
            "catalog_managed": self.catalog_state.managed,
            "configured": (
                self.config_state.configured and self.catalog_state.configured
            ),
            "config_error": self.config_state.error,
            "catalog_error": self.catalog_state.error,
            "requested_context_window": DESIRED_CONTEXT_WINDOW,
            "requested_auto_compact_token_limit": AUTO_COMPACT_TOKEN_LIMIT,
            "catalog_context_window": self.catalog_context_window,
            "catalog_max_context_window": self.catalog_max_context_window,
            "effective_context_window_percent": (self.effective_context_window_percent),
            "effective_context_window": self.effective_context_window,
            "eligible": self.eligible,
            "ready": self.ready,
            "eligibility_blockers": list(self.eligibility_blockers),
            "readiness_blockers": list(self.readiness_blockers),
        }


def _default_runner(
    args: Sequence[str], env: dict[str, str], timeout: float
) -> CommandResult:
    completed = subprocess.run(
        args,
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=timeout,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _target_env(target: CodexTarget) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(target.home)
    if target.executable.lower().endswith(".exe") and os.name != "nt":
        entries = [entry for entry in env.get("WSLENV", "").split(":") if entry]
        if not any(entry.split("/", 1)[0] == "CODEX_HOME" for entry in entries):
            entries.append("CODEX_HOME/p")
        env["WSLENV"] = ":".join(entries)
    return env


def _run_codex(
    target: CodexTarget,
    args: Sequence[str],
    runner: Runner,
) -> CommandResult:
    return runner(
        [target.executable, *args],
        _target_env(target),
        COMMAND_TIMEOUT_SECONDS,
    )


def _safe_error(value: str) -> str:
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)"
        r"(\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)",
        r"\1\2[redacted]",
        value.strip(),
    )
    redacted = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~-]+", "Bearer [redacted]", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", redacted)
    return redacted[:500]


def _runtime_path(target: CodexTarget, path: Path) -> str:
    if target.kind != "windows" or os.name == "nt":
        return str(path)
    parts = path.parts
    if len(parts) >= 4 and parts[:2] == ("/", "mnt") and len(parts[2]) == 1:
        drive = parts[2].upper()
        return f"{drive}:\\" + "\\".join(parts[3:])
    return str(path)


def _expected_config(target: CodexTarget, catalog_path: Path) -> dict[str, Any]:
    return {
        "model": MODEL_SLUG,
        "model_context_window": DESIRED_CONTEXT_WINDOW,
        "model_auto_compact_token_limit": AUTO_COMPACT_TOKEN_LIMIT,
        "model_catalog_json": _runtime_path(target, catalog_path),
    }


def _managed_config_block(target: CodexTarget, catalog_path: Path) -> str:
    expected = _expected_config(target, catalog_path)
    return (
        CONFIG_MARKER_BEGIN
        + "# Default GPT-5.6 Sol 1M context and its managed catalog override.\n"
        + f"model = {json.dumps(expected['model'], ensure_ascii=False)}\n"
        + f"model_context_window = {expected['model_context_window']}\n"
        + "model_auto_compact_token_limit = "
        + f"{expected['model_auto_compact_token_limit']}\n"
        + "model_catalog_json = "
        + f"{json.dumps(expected['model_catalog_json'], ensure_ascii=False)}\n"
        + CONFIG_MARKER_END
    )


def _managed_config_layout(
    content: str,
) -> tuple[tuple[int, int] | None, str | None]:
    begin_count = content.count(CONFIG_MARKER_BEGIN)
    end_count = content.count(CONFIG_MARKER_END)
    if begin_count == 0 and end_count == 0:
        return None, None
    if begin_count != 1 or end_count != 1:
        return None, "Managed config marker is missing or duplicated"
    start = content.index(CONFIG_MARKER_BEGIN)
    end_start = content.index(CONFIG_MARKER_END)
    if start != 0 or end_start < start:
        return None, "Managed config marker must form one block at the file start"
    return (start, end_start + len(CONFIG_MARKER_END)), None


def _managed_key_conflicts(parsed: dict[str, Any]) -> tuple[str, ...]:
    return tuple(key for key in MANAGED_CONFIG_KEYS if key in parsed)


def _read_config_state(
    target: CodexTarget,
    path: Path,
    catalog_path: Path,
) -> ManagedFileState:
    if not path.exists():
        return ManagedFileState(False, False, False)
    try:
        content = path.read_text("utf-8")
    except OSError as exc:
        return ManagedFileState(True, False, False, _safe_error(str(exc)))
    layout, layout_error = _managed_config_layout(content)
    if layout_error is not None:
        return ManagedFileState(True, False, False, layout_error)
    try:
        parsed = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        return ManagedFileState(True, layout is not None, False, _safe_error(str(exc)))
    if layout is None:
        conflicts = _managed_key_conflicts(parsed)
        error = (
            "Base config contains unmanaged default keys: " + ", ".join(conflicts)
            if conflicts
            else None
        )
        return ManagedFileState(True, False, False, error)
    _, end = layout
    try:
        remainder = tomllib.loads(content[end:])
    except tomllib.TOMLDecodeError as exc:
        return ManagedFileState(True, True, False, _safe_error(str(exc)))
    conflicts = _managed_key_conflicts(remainder)
    if conflicts:
        return ManagedFileState(
            True,
            True,
            False,
            "Base config repeats managed keys outside the marker block: "
            + ", ".join(conflicts),
        )
    expected = _expected_config(target, catalog_path)
    configured = all(parsed.get(key) == value for key, value in expected.items())
    return ManagedFileState(True, True, configured)


def _prepare_config_content(
    target: CodexTarget,
    path: Path,
    catalog_path: Path,
) -> bytes:
    try:
        content = path.read_text("utf-8") if path.exists() else ""
    except OSError as exc:
        raise ConfigurationOwnershipError(_safe_error(str(exc))) from exc
    layout, layout_error = _managed_config_layout(content)
    if layout_error is not None:
        raise ConfigurationOwnershipError(layout_error)
    if layout is None:
        try:
            parsed = tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationOwnershipError(
                "Base config could not be safely inspected: " + _safe_error(str(exc))
            ) from exc
        conflicts = _managed_key_conflicts(parsed)
        if conflicts:
            raise ConfigurationOwnershipError(
                "Refusing to overwrite unmanaged base config keys: "
                + ", ".join(conflicts)
            )
        separator = "\n" if content else ""
        updated = _managed_config_block(target, catalog_path) + separator + content
    else:
        start, end = layout
        remainder_text = content[:start] + content[end:]
        try:
            remainder = tomllib.loads(remainder_text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationOwnershipError(
                "Base config outside the managed block is invalid: "
                + _safe_error(str(exc))
            ) from exc
        conflicts = _managed_key_conflicts(remainder)
        if conflicts:
            raise ConfigurationOwnershipError(
                "Refusing repeated unmanaged base config keys: " + ", ".join(conflicts)
            )
        updated = (
            content[:start]
            + _managed_config_block(target, catalog_path)
            + content[end:]
        )
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationBlocked(
            "Generated base config is invalid: " + _safe_error(str(exc))
        ) from exc
    return updated.encode("utf-8")


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _find_model(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Model catalog root is not an object")
    models = payload.get("models")
    if not isinstance(models, list):
        raise ValueError("Model catalog has no models list")
    for item in models:
        if isinstance(item, dict) and item.get("slug") == MODEL_SLUG:
            return item
    raise ValueError(f"Model {MODEL_SLUG} was not found in the Codex catalog")


def _catalog_metrics(payload: Any) -> tuple[int | None, int | None, int]:
    model = _find_model(payload)
    context = _optional_int(model.get("context_window"))
    maximum = _optional_int(model.get("max_context_window")) or context
    percent = _optional_int(model.get("effective_context_window_percent")) or 100
    return context, maximum, percent


def _read_catalog_state(path: Path, owner_path: Path) -> ManagedFileState:
    exists = path.exists() or owner_path.exists()
    if not exists:
        return ManagedFileState(False, False, False)
    try:
        owner_content = owner_path.read_text("utf-8") if owner_path.exists() else None
    except OSError as exc:
        return ManagedFileState(True, False, False, _safe_error(str(exc)))
    managed = owner_content == CATALOG_OWNER_CONTENT
    if owner_content is not None and not managed:
        return ManagedFileState(
            True, False, False, "Catalog ownership marker is not recognized"
        )
    if path.exists() and not managed:
        return ManagedFileState(
            True,
            False,
            False,
            "Catalog file exists without this tool's ownership marker",
        )
    if not path.exists():
        return ManagedFileState(True, managed, False)
    try:
        payload = json.loads(path.read_text("utf-8"))
        context, maximum, percent = _catalog_metrics(payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return ManagedFileState(True, managed, False, _safe_error(str(exc)))
    effective = (
        min(DESIRED_CONTEXT_WINDOW, maximum) * percent // 100
        if maximum is not None
        else None
    )
    configured = (
        context is not None
        and context >= DESIRED_CONTEXT_WINDOW
        and maximum is not None
        and maximum >= DESIRED_CONTEXT_WINDOW
        and effective is not None
        and effective > AUTO_COMPACT_TOKEN_LIMIT
    )
    return ManagedFileState(True, managed, configured)


def _assert_catalog_ownership(path: Path, owner_path: Path) -> None:
    try:
        owner_content = owner_path.read_text("utf-8") if owner_path.exists() else None
    except OSError as exc:
        raise ConfigurationOwnershipError(_safe_error(str(exc))) from exc
    if owner_content is not None and owner_content != CATALOG_OWNER_CONTENT:
        raise ConfigurationOwnershipError(
            f"Refusing to overwrite catalog with an unknown owner: {path}"
        )
    if path.exists() and owner_content != CATALOG_OWNER_CONTENT:
        raise ConfigurationOwnershipError(
            f"Refusing to overwrite unmanaged catalog: {path}"
        )


def _prepare_catalog_content(result: CommandResult) -> bytes:
    if result.returncode != 0:
        raise ConfigurationBlocked(
            "Codex bundled catalog probe failed: " + _safe_error(result.stderr)
        )
    try:
        payload = json.loads(result.stdout)
        model = _find_model(payload)
        current_context = _optional_int(model.get("context_window")) or 0
        current_maximum = _optional_int(model.get("max_context_window")) or 0
        percent = _optional_int(model.get("effective_context_window_percent")) or 100
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigurationBlocked(_safe_error(str(exc))) from exc
    if DESIRED_CONTEXT_WINDOW * percent // 100 <= AUTO_COMPACT_TOKEN_LIMIT:
        raise ConfigurationBlocked(
            "The catalog effective context percentage is too small for a 900,000-token "
            "auto-compaction threshold"
        )
    model["context_window"] = max(current_context, DESIRED_CONTEXT_WINDOW)
    model["max_context_window"] = max(
        current_maximum,
        model["context_window"],
        DESIRED_CONTEXT_WINDOW,
    )
    return (json.dumps(payload, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _restore_path(path: Path, previous_content: bytes | None) -> None:
    if previous_content is None:
        if path.exists():
            path.unlink()
            _fsync_directory(path.parent)
        return
    _atomic_write(path, previous_content)


def _auth_mode(output: str) -> str:
    lowered = output.lower()
    if "api key" in lowered:
        return "api_key"
    if "chatgpt" in lowered:
        return "chatgpt"
    if "not logged in" in lowered:
        return "not_logged_in"
    return "unknown"


def inspect_target(
    target: CodexTarget,
    *,
    runner: Runner = _default_runner,
) -> Codex1MStatus:
    """Inspect the target without writing configuration or reading auth.json."""
    config_path = target.home / "config.toml"
    catalog_path = target.home / CATALOG_RELATIVE_PATH
    owner_path = target.home / CATALOG_OWNER_RELATIVE_PATH
    config_state = _read_config_state(target, config_path, catalog_path)
    catalog_state = _read_catalog_state(catalog_path, owner_path)
    blockers: list[str] = []
    if config_state.error is not None:
        blockers.append(config_state.error)
    if catalog_state.error is not None:
        blockers.append(catalog_state.error)

    version_result = _run_codex(target, ["--version"], runner)
    version = version_result.stdout.strip() if version_result.returncode == 0 else None
    if version is None:
        blockers.append(
            f"Codex version probe failed: {_safe_error(version_result.stderr)}"
        )

    auth_result = _run_codex(target, ["login", "status"], runner)
    auth_mode = (
        _auth_mode(f"{auth_result.stdout}\n{auth_result.stderr}")
        if auth_result.returncode == 0
        else "unknown"
    )

    catalog_result = _run_codex(target, ["debug", "models"], runner)
    catalog_context: int | None = None
    catalog_maximum: int | None = None
    effective_percent: int | None = None
    if catalog_result.returncode != 0:
        blockers.append(
            f"Codex model catalog probe failed: {_safe_error(catalog_result.stderr)}"
        )
    else:
        try:
            payload = json.loads(catalog_result.stdout)
            catalog_context, catalog_maximum, effective_percent = _catalog_metrics(
                payload
            )
        except (ValueError, json.JSONDecodeError) as exc:
            blockers.append(_safe_error(str(exc)))

    effective_context: int | None = None
    if catalog_maximum is not None and effective_percent is not None:
        requested = min(DESIRED_CONTEXT_WINDOW, catalog_maximum)
        effective_context = requested * effective_percent // 100

    eligibility_blockers = tuple(blockers)
    eligible = not eligibility_blockers
    readiness = list(eligibility_blockers)
    if not (config_state.configured and catalog_state.configured):
        readiness.append("default 1M configuration is not installed")
    if catalog_maximum is not None and catalog_maximum < DESIRED_CONTEXT_WINDOW:
        readiness.append(
            "Active catalog max context is "
            f"{catalog_maximum:,}; requested context is {DESIRED_CONTEXT_WINDOW:,}"
        )
    if effective_context is not None and effective_context <= AUTO_COMPACT_TOKEN_LIMIT:
        readiness.append(
            "Auto-compact threshold "
            f"{AUTO_COMPACT_TOKEN_LIMIT:,} is not below effective context "
            f"{effective_context:,}"
        )

    return Codex1MStatus(
        target=target,
        version=version,
        auth_mode=auth_mode,
        config_path=config_path,
        catalog_path=catalog_path,
        catalog_owner_path=owner_path,
        config_state=config_state,
        catalog_state=catalog_state,
        catalog_context_window=catalog_context,
        catalog_max_context_window=catalog_maximum,
        effective_context_window_percent=effective_percent,
        effective_context_window=effective_context,
        eligible=eligible,
        ready=eligible and not readiness,
        eligibility_blockers=eligibility_blockers,
        readiness_blockers=tuple(readiness),
    )


def configure_target(
    target: CodexTarget,
    *,
    runner: Runner = _default_runner,
) -> Codex1MStatus:
    """Install the managed catalog and make the 1M settings the user default."""
    config_path = target.home / "config.toml"
    catalog_path = target.home / CATALOG_RELATIVE_PATH
    owner_path = target.home / CATALOG_OWNER_RELATIVE_PATH
    _assert_catalog_ownership(catalog_path, owner_path)
    config_content = _prepare_config_content(target, config_path, catalog_path)
    bundled_result = _run_codex(target, ["debug", "models", "--bundled"], runner)
    catalog_content = _prepare_catalog_content(bundled_result)

    paths = (config_path, owner_path, catalog_path)
    previous = {path: path.read_bytes() if path.exists() else None for path in paths}
    try:
        _atomic_write(catalog_path, catalog_content)
        _atomic_write(owner_path, CATALOG_OWNER_CONTENT.encode("utf-8"))
        _atomic_write(config_path, config_content)
        after = inspect_target(target, runner=runner)
        if after.ready:
            return after
        raise ConfigurationBlocked(
            "Default 1M verification failed: " + "; ".join(after.readiness_blockers)
        )
    except BaseException:
        for path in paths:
            _restore_path(path, previous[path])
        raise


def _discover_windows_home() -> Path:
    users_root = Path("/mnt/c/Users")
    if not users_root.is_dir():
        raise TargetResolutionError(
            "Windows users directory is unavailable; pass --codex-home explicitly"
        )
    candidates = sorted(
        user / ".codex"
        for user in users_root.iterdir()
        if user.is_dir() and (user / ".codex").is_dir()
    )
    if len(candidates) == 1:
        return candidates[0]
    with_executable = [
        home
        for home in candidates
        if (
            home.parent / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        ).is_file()
    ]
    if len(with_executable) == 1:
        return with_executable[0]
    raise TargetResolutionError(
        "Windows Codex home is ambiguous; pass --codex-home explicitly"
    )


def resolve_target(
    kind: str,
    *,
    codex_bin: str | None = None,
    codex_home: str | Path | None = None,
) -> CodexTarget:
    """Resolve a WSL/Linux or Windows Codex installation without changing it."""
    if kind not in {"wsl", "windows"}:
        raise TargetResolutionError(f"Unsupported target: {kind}")

    home = Path(codex_home).expanduser() if codex_home is not None else None
    executable = codex_bin
    if kind == "wsl":
        home = home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        executable = executable or shutil.which("codex")
    elif os.name == "nt":
        home = home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        executable = executable or shutil.which("codex.exe") or shutil.which("codex")
    else:
        home = home or _discover_windows_home()
        expected_executable = (
            home.parent / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        )
        executable = executable or (
            str(expected_executable)
            if expected_executable.is_file()
            else shutil.which("codex.exe")
        )

    if executable is None:
        raise TargetResolutionError(
            f"Codex executable for target {kind} was not found; pass --codex-bin"
        )
    assert home is not None
    return CodexTarget(kind=kind, executable=executable, home=home)


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", choices=("wsl", "windows"), default="wsl")
    parser.add_argument("--codex-bin", help="Explicit Codex executable")
    parser.add_argument("--codex-home", type=Path, help="Explicit CODEX_HOME")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fcc-codex-1m",
        description="Safely configure Codex GPT-5.6 Sol as the default 1M model.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser(
        "status", help="Inspect requested, configured, and effective context"
    )
    _add_target_arguments(status)
    status.add_argument("--json", action="store_true", dest="as_json")

    configure = commands.add_parser(
        "configure", help="Install the managed catalog and default 1M settings"
    )
    _add_target_arguments(configure)
    configure.add_argument("--json", action="store_true", dest="as_json")

    run = commands.add_parser("run", help="Launch Codex after the default is ready")
    _add_target_arguments(run)
    run.add_argument("codex_args", nargs=argparse.REMAINDER)
    return parser


def _print_status(status: Codex1MStatus, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(status.to_dict(), indent=2, sort_keys=True))
        return
    print(f"Target: {status.target.kind}")
    print(f"Codex: {status.version or 'unknown'} ({status.target.executable})")
    print(f"Codex home: {status.target.home}")
    print(f"Auth mode: {status.auth_mode}")
    print(f"Base config: {status.config_path}")
    print(f"Managed catalog: {status.catalog_path}")
    print(f"Requested context: {DESIRED_CONTEXT_WINDOW:,}")
    print(f"Catalog max: {status.catalog_max_context_window or 0:,}")
    print(f"Effective context: {status.effective_context_window or 0:,}")
    print(f"Auto-compact threshold: {AUTO_COMPACT_TOKEN_LIMIT:,}")
    print(f"Eligible: {'yes' if status.eligible else 'no'}")
    print(f"Ready: {'yes' if status.ready else 'no'}")
    if status.readiness_blockers:
        print("Blockers:")
        for blocker in status.readiness_blockers:
            print(f"  - {blocker}")


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = _default_runner,
    exec_fn: ExecFn = os.execvpe,
) -> int:
    """Run the command and return a process exit code."""
    args = _build_parser().parse_args(argv)
    try:
        target = resolve_target(
            args.target,
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
        )
        if args.command == "configure":
            status = configure_target(target, runner=runner)
            _print_status(status, as_json=args.as_json)
            return 0

        status = inspect_target(target, runner=runner)
        if args.command == "status":
            _print_status(status, as_json=args.as_json)
            return 0 if status.ready else 2

        if not status.ready:
            _print_status(status, as_json=False)
            return 2
        codex_args = list(args.codex_args)
        if codex_args[:1] == ["--"]:
            codex_args.pop(0)
        command = [target.executable, *codex_args]
        exec_fn(target.executable, command, _target_env(target))
        return 0
    except (Codex1MError, OSError, subprocess.SubprocessError) as exc:
        print(f"fcc-codex-1m: {_safe_error(str(exc))}", file=sys.stderr)
        return 2


def cli() -> None:
    """Console-script entry point."""
    raise SystemExit(main())


if __name__ == "__main__":
    cli()

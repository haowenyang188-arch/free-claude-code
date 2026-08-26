"""Discover and describe the local CLI runtimes used by the gateway.

The registry is intentionally independent from session execution.  It borrows
the useful part of WorkBuddy's provider registry (bounded, cached, isolated
probes) while keeping the DSH rule that subprocesses are started with an argv
array and never through a shell.  A probe is diagnostic; callers decide whether
to make it a startup gate.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Any

CommandRunner = Callable[[Sequence[str], float], Awaitable[tuple[int, bytes, bytes]]]

_VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?\b")


class RuntimeBackend(StrEnum):
    """CLI backends supported by the messaging gateway."""

    CLAUDE = "claude"
    CODEX = "codex"


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    """User-safe result of probing one local CLI executable."""

    backend: RuntimeBackend
    executable: str
    available: bool
    version: str | None
    capabilities: tuple[str, ...]
    reason: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        """Return a stable, path-safe mapping for status endpoints/logs."""
        return {
            "backend": self.backend.value,
            "executable": Path(self.executable).name,
            "available": self.available,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RuntimeProfileProbe:
    """Whether the installed runtime supports this project's safe profile."""

    backend: RuntimeBackend
    available: bool
    required_flags: tuple[str, ...]
    missing_flags: tuple[str, ...]
    reason: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "backend": self.backend.value,
            "available": self.available,
            "required_flags": list(self.required_flags),
            "missing_flags": list(self.missing_flags),
            "reason": self.reason,
        }


_CAPABILITIES: Mapping[RuntimeBackend, tuple[str, ...]] = {
    RuntimeBackend.CLAUDE: (
        "stream_json",
        "resume",
        "fork",
        "permission_modes",
        "workspace_allowlist",
    ),
    RuntimeBackend.CODEX: (
        "jsonl_events",
        "resume",
        "fork",
        "sandbox",
        "staged_approval",
    ),
}
_SAFE_PROFILE_FLAGS: Mapping[RuntimeBackend, tuple[str, ...]] = {
    RuntimeBackend.CLAUDE: ("--safe-mode", "--strict-mcp-config"),
    RuntimeBackend.CODEX: ("--ignore-user-config", "--ignore-rules", "--strict-config"),
}


class RuntimeRegistry:
    """Probe the configured CLI backends with bounded caching.

    The default probe only executes ``<binary> --version``.  It does not read
    credentials, start a model session, or expose raw stderr.  A custom runner
    is accepted so tests and embedding applications can supply a deterministic
    subprocess boundary.
    """

    def __init__(
        self,
        *,
        executables: Mapping[str | RuntimeBackend, str] | None = None,
        timeout_seconds: float = 5.0,
        cache_ttl_seconds: float = 30.0,
        runner: CommandRunner | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must not be negative")

        configured = {
            RuntimeBackend.CLAUDE: "claude",
            RuntimeBackend.CODEX: "codex",
        }
        for key, value in (executables or {}).items():
            try:
                backend = RuntimeBackend(key)
            except ValueError as exc:
                raise ValueError(f"unsupported runtime backend: {key!r}") from exc
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"executable for {backend.value} must be non-empty")
            configured[backend] = value.strip()

        self._executables = configured
        self.timeout_seconds = float(timeout_seconds)
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self._runner = runner or _run_command
        self._uses_default_runner = runner is None
        self._cache: dict[RuntimeBackend, tuple[float, RuntimeProbe]] = {}
        self._profile_cache: dict[
            RuntimeBackend, tuple[float, RuntimeProfileProbe]
        ] = {}
        self._locks = {backend: asyncio.Lock() for backend in RuntimeBackend}
        self._profile_locks = {backend: asyncio.Lock() for backend in RuntimeBackend}

    async def probe(
        self,
        backend: str | RuntimeBackend,
        *,
        force: bool = False,
    ) -> RuntimeProbe:
        """Probe one backend and return a redacted, stable result."""
        runtime = _coerce_backend(backend)
        now = monotonic()
        cached = self._cache.get(runtime)
        if not force and cached is not None:
            cached_at, result = cached
            if now - cached_at <= self.cache_ttl_seconds:
                return result

        async with self._locks[runtime]:
            now = monotonic()
            cached = self._cache.get(runtime)
            if not force and cached is not None:
                cached_at, result = cached
                if now - cached_at <= self.cache_ttl_seconds:
                    return result

            executable = self._executables[runtime]
            result = await self._probe_uncached(runtime, executable)
            self._cache[runtime] = (monotonic(), result)
            return result

    async def probe_all(
        self,
        *,
        force: bool = False,
    ) -> dict[str, RuntimeProbe]:
        """Probe both runtimes concurrently without one failure masking the other."""
        results = await asyncio.gather(
            *(self.probe(backend, force=force) for backend in RuntimeBackend)
        )
        return {result.backend.value: result for result in results}

    async def probe_safe_profile(
        self,
        backend: str | RuntimeBackend,
        *,
        force: bool = False,
    ) -> RuntimeProfileProbe:
        """Check help output for every flag required by safe isolation."""
        runtime = _coerce_backend(backend)
        runtime_probe = await self.probe(runtime, force=force)
        required_flags = _SAFE_PROFILE_FLAGS[runtime]
        if not runtime_probe.available:
            return RuntimeProfileProbe(
                backend=runtime,
                available=False,
                required_flags=required_flags,
                missing_flags=(),
                reason=runtime_probe.reason or "runtime_unavailable",
            )

        now = monotonic()
        cached = self._profile_cache.get(runtime)
        if not force and cached is not None:
            cached_at, result = cached
            if now - cached_at <= self.cache_ttl_seconds:
                return result

        async with self._profile_locks[runtime]:
            now = monotonic()
            cached = self._profile_cache.get(runtime)
            if not force and cached is not None:
                cached_at, result = cached
                if now - cached_at <= self.cache_ttl_seconds:
                    return result

            result = await self._probe_safe_profile_uncached(runtime)
            self._profile_cache[runtime] = (monotonic(), result)
            return result

    def clear_cache(self) -> None:
        """Forget cached probe results so the next request rechecks the host."""
        self._cache.clear()
        self._profile_cache.clear()

    async def _probe_safe_profile_uncached(
        self, backend: RuntimeBackend
    ) -> RuntimeProfileProbe:
        executable = self._executables[backend]
        required_flags = _SAFE_PROFILE_FLAGS[backend]
        argv = (
            (executable, "--help")
            if backend is RuntimeBackend.CLAUDE
            else (executable, "exec", "--help")
        )
        try:
            return_code, stdout, _stderr = await self._run_probe(backend, argv)
        except TimeoutError:
            return RuntimeProfileProbe(
                backend=backend,
                available=False,
                required_flags=required_flags,
                missing_flags=(),
                reason="profile_probe_timeout",
            )
        except Exception:
            return RuntimeProfileProbe(
                backend=backend,
                available=False,
                required_flags=required_flags,
                missing_flags=(),
                reason="profile_probe_failed",
            )
        if return_code != 0:
            return RuntimeProfileProbe(
                backend=backend,
                available=False,
                required_flags=required_flags,
                missing_flags=(),
                reason="help_command_failed",
            )
        text = stdout.decode("utf-8", errors="replace")
        missing = tuple(
            flag for flag in required_flags if not _has_option_token(text, flag)
        )
        return RuntimeProfileProbe(
            backend=backend,
            available=not missing,
            required_flags=required_flags,
            missing_flags=missing,
            reason=None if not missing else "required_flags_missing",
        )

    async def _probe_uncached(
        self,
        backend: RuntimeBackend,
        executable: str,
    ) -> RuntimeProbe:
        capabilities = _CAPABILITIES[backend]
        if shutil.which(executable) is None and not Path(executable).is_file():
            return RuntimeProbe(
                backend=backend,
                executable=executable,
                available=False,
                version=None,
                capabilities=capabilities,
                reason="executable_not_found",
            )

        try:
            return_code, stdout, _stderr = await self._run_probe(
                backend, (executable, "--version")
            )
        except TimeoutError:
            return RuntimeProbe(
                backend=backend,
                executable=executable,
                available=False,
                version=None,
                capabilities=capabilities,
                reason="probe_timeout",
            )
        except Exception:
            # Do not expose subprocess paths, environment values, or raw
            # provider diagnostics in a status response.
            return RuntimeProbe(
                backend=backend,
                executable=executable,
                available=False,
                version=None,
                capabilities=capabilities,
                reason="probe_failed",
            )

        version = _extract_version(stdout)
        if return_code != 0:
            return RuntimeProbe(
                backend=backend,
                executable=executable,
                available=False,
                version=version,
                capabilities=capabilities,
                reason="version_command_failed",
            )

        return RuntimeProbe(
            backend=backend,
            executable=executable,
            available=True,
            version=version,
            capabilities=capabilities,
            reason=None if version is not None else "version_unreported",
        )

    async def _run_probe(
        self,
        backend: RuntimeBackend,
        argv: Sequence[str],
    ) -> tuple[int, bytes, bytes]:
        """Run a probe while keeping the two-argument custom runner contract."""
        if self._uses_default_runner:
            return await asyncio.wait_for(
                _run_command(
                    argv,
                    self.timeout_seconds,
                    backend=backend,
                ),
                timeout=self.timeout_seconds,
            )
        return await asyncio.wait_for(
            self._runner(argv, self.timeout_seconds),
            timeout=self.timeout_seconds,
        )


def _coerce_backend(value: str | RuntimeBackend) -> RuntimeBackend:
    try:
        return RuntimeBackend(value)
    except ValueError as exc:
        raise ValueError(f"unsupported runtime backend: {value!r}") from exc


def _extract_version(output: bytes) -> str | None:
    """Extract only a conventional version token from untrusted CLI output."""
    text = output.decode("utf-8", errors="replace")
    match = _VERSION_RE.search(text)
    return match.group(0) if match else None


def _has_option_token(text: str, option: str) -> bool:
    """Return whether ``option`` appears as a complete long-option token."""
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(option)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text) is not None


async def _run_command(
    argv: Sequence[str],
    timeout_seconds: float,
    *,
    backend: RuntimeBackend = RuntimeBackend.CLAUDE,
) -> tuple[int, bytes, bytes]:
    """Run a probe without invoking a shell or inheriting provider state."""
    # Import lazily to avoid the runtime_environment -> runtime_registry cycle.
    from .runtime_environment import build_cli_environment

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=build_cli_environment(backend),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise
    return process.returncode or 0, stdout, stderr


__all__ = [
    "RuntimeBackend",
    "RuntimeProbe",
    "RuntimeProfileProbe",
    "RuntimeRegistry",
]

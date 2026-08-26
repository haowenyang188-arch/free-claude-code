"""Track and clean up spawned CLI subprocesses.

This is a safety net for cases where the server is interrupted (Ctrl+C) and the
FastAPI lifespan cleanup doesn't run to completion. We only track processes we
spawn so we don't accidentally kill unrelated system processes.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

_lock = threading.Lock()
_pids: set[int] = set()
_leases: dict[int, ProcessLease] = {}
_atexit_registered = False


class ProcessOwnershipError(RuntimeError):
    """Raised when a PID is already owned by another runtime generation."""


@dataclass(frozen=True, slots=True)
class ProcessLease:
    """Exact ownership evidence for one spawned subprocess."""

    pid: int
    generation: str
    fingerprint: str | None


def ensure_atexit_registered() -> None:
    global _atexit_registered
    with _lock:
        if _atexit_registered:
            return
        atexit.register(kill_all_best_effort)
        _atexit_registered = True


def register_pid(pid: int) -> None:
    if not pid:
        return
    ensure_atexit_registered()
    with _lock:
        _pids.add(int(pid))


def unregister_pid(pid: int) -> None:
    if not pid:
        return
    with _lock:
        _pids.discard(int(pid))


def register_process(pid: int, *, generation: str) -> ProcessLease | None:
    """Register a PID under one immutable generation owner."""
    if not pid:
        return None
    if not isinstance(generation, str) or not generation.strip():
        raise ValueError("generation must be a non-empty string")
    normalized_pid = int(pid)
    lease = ProcessLease(
        pid=normalized_pid,
        generation=generation.strip(),
        fingerprint=_process_fingerprint(normalized_pid),
    )
    ensure_atexit_registered()
    with _lock:
        existing = _leases.get(normalized_pid)
        if existing is not None and existing != lease:
            raise ProcessOwnershipError(
                f"pid {normalized_pid} is already registered to another generation"
            )
        _leases[normalized_pid] = lease
    return lease


def unregister_process(pid: int, *, generation: str) -> bool:
    """Release a PID only when the caller owns its exact generation."""
    if not pid:
        return False
    normalized_pid = int(pid)
    with _lock:
        existing = _leases.get(normalized_pid)
        if existing is None or existing.generation != generation:
            return False
        _leases.pop(normalized_pid, None)
        return True


def kill_all_best_effort() -> None:
    """Kill any still-running registered pids (best-effort)."""
    with _lock:
        pids = list(_pids)
        leases = list(_leases.values())
        _pids.clear()
        _leases.clear()

    for lease in leases:
        if lease.fingerprint is not None:
            current = _process_fingerprint(lease.pid)
            if current != lease.fingerprint:
                logger.warning(
                    "process_registry: skipped pid={} after ownership fingerprint changed",
                    lease.pid,
                )
                continue
        pids.append(lease.pid)

    pids = list(dict.fromkeys(pids))

    if not pids:
        return

    if os.name == "nt":
        for pid in pids:
            try:
                # /T kills child processes, /F forces termination.
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception as e:
                logger.debug("process_registry: taskkill failed pid=%s: %s", pid, e)
        return

    # Best-effort fallback for non-Windows.
    for pid in pids:
        try:
            os.kill(pid, 9)
        except Exception as e:
            logger.debug("process_registry: kill failed pid=%s: %s", pid, e)


def _process_fingerprint(pid: int) -> str | None:
    """Return Linux process start ticks used to detect PID reuse."""
    if os.name != "posix":
        return None
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    # The comm field may contain spaces and parentheses.  Fields after its
    # final ')' start at process-state (field 3); starttime is field 22.
    closing = stat.rfind(")")
    if closing < 0:
        return None
    remainder = stat[closing + 2 :].split()
    if len(remainder) <= 19:
        return None
    return remainder[19]


__all__ = [
    "ProcessLease",
    "ProcessOwnershipError",
    "ensure_atexit_registered",
    "kill_all_best_effort",
    "register_pid",
    "register_process",
    "unregister_pid",
    "unregister_process",
]

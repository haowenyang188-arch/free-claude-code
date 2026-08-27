"""Owned process lifecycle for Workbench command execution."""

from __future__ import annotations

import asyncio
import math
import os
import signal
import subprocess
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlparse
from urllib.request import urlopen

from cli.process_registry import register_process, unregister_process

from .approval import ApprovalManager, ApprovalRecord, ApprovalState, CommandIntent


class JobRuntimeError(RuntimeError):
    """A process could not be started or managed by the job runtime."""


class JobState(StrEnum):
    JOB_RUNNING = "job_running"
    JOB_COMPLETED = "job_completed"
    PROCESS_FAILED = "process_failed"
    PROCESS_TIMEOUT = "process_timeout"
    JOB_CANCELLED = "job_cancelled"


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    call_id: str
    command_hash: str
    pid: int
    status: JobState
    background: bool
    port: int | None
    health_url: str | None
    started_at: datetime
    ready: bool = False
    exit_code: int | None = None


class JobRuntime:
    """Track processes only after an approved executor has spawned them."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._generations: dict[str, str] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._executor_token = object()

    async def start(
        self,
        approval: ApprovalRecord,
        *,
        background: bool,
        port: int | None = None,
        health_url: str | None = None,
        job_wait_timeout_seconds: float = 5.0,
        process_timeout_seconds: float | None = None,
        _executor_token: object | None = None,
    ) -> JobRecord:
        if _executor_token is not self._executor_token:
            raise PermissionError("executor-only job start")
        if approval.status is not ApprovalState.CONSUMED:
            raise PermissionError("approval_unavailable")
        _validate_start_options(
            background=background,
            port=port,
            health_url=health_url,
            job_wait_timeout_seconds=job_wait_timeout_seconds,
            process_timeout_seconds=process_timeout_seconds,
        )

        try:
            process = await _spawn_owned_process(approval)
        except OSError as exc:
            raise JobRuntimeError("process_failed") from exc
        if not process.pid:
            if process.returncode is None:
                _terminate_process_group(process)
                await process.wait()
            raise JobRuntimeError("process_failed")
        job_id = str(uuid.uuid4())
        generation = uuid.uuid4().hex
        try:
            register_process(process.pid, generation=generation)
        except Exception as exc:
            if process.returncode is None:
                _terminate_process_group(process)
                await process.wait()
            raise JobRuntimeError("process_failed") from exc
        record = JobRecord(
            job_id=job_id,
            call_id=approval.call_id,
            command_hash=approval.command_hash,
            pid=process.pid,
            status=JobState.JOB_RUNNING,
            background=background,
            port=port,
            health_url=health_url,
            started_at=datetime.now(UTC),
        )
        async with self._lock:
            self._jobs[job_id] = record
            self._processes[job_id] = process
            self._generations[job_id] = generation
            self._watchers[job_id] = asyncio.create_task(
                self._watch(job_id, process, generation), name=f"workbench-job-{job_id}"
            )

        if background:
            return await self._await_background_readiness(
                job_id, job_wait_timeout_seconds
            )
        if process_timeout_seconds is None:
            await process.wait()
            watcher = self._watchers.get(job_id)
            if watcher is not None:
                await watcher
            return await self.get(job_id)
        try:
            await asyncio.wait_for(process.wait(), timeout=process_timeout_seconds)
        except TimeoutError:
            await self._terminate_owned(job_id)
            return await self.get(job_id)
        watcher = self._watchers.get(job_id)
        if watcher is not None:
            await watcher
        return await self.get(job_id)

    async def get(self, job_id: str) -> JobRecord:
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise KeyError(job_id)
            return record

    async def is_process_alive(self, job_id: str) -> bool:
        async with self._lock:
            process = self._processes.get(job_id)
            return process is not None and process.returncode is None

    async def stop(self, job_id: str) -> JobRecord:
        await self._terminate_owned(job_id, cancelled=True)
        return await self.get(job_id)

    async def close(self) -> None:
        for job_id in tuple(self._processes):
            await self._terminate_owned(job_id, cancelled=True)

    async def _await_background_readiness(
        self, job_id: str, timeout_seconds: float
    ) -> JobRecord:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            record = await self.get(job_id)
            if record.status is not JobState.JOB_RUNNING:
                return record
            ready = await self._is_ready(record)
            if ready:
                updated = replace(record, ready=True)
                async with self._lock:
                    self._jobs[job_id] = updated
                return updated
            if asyncio.get_running_loop().time() >= deadline:
                # A readiness wait expiring is not a process timeout. The job
                # remains owned and running until it exits or is explicitly stopped.
                return record
            await asyncio.sleep(0.05)

    async def _is_ready(self, record: JobRecord) -> bool:
        if record.port is not None and not await _port_is_listening(record.port):
            return False
        if record.health_url is not None and not await _health_is_ok(record.health_url):
            return False
        return record.port is not None or record.health_url is not None

    async def _watch(
        self,
        job_id: str,
        process: asyncio.subprocess.Process,
        generation: str,
    ) -> None:
        exit_code = await process.wait()
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is not None and record.status is JobState.JOB_RUNNING:
                self._jobs[job_id] = replace(
                    record,
                    status=(
                        JobState.JOB_COMPLETED
                        if exit_code == 0
                        else JobState.PROCESS_FAILED
                    ),
                    exit_code=exit_code,
                )
            self._processes.pop(job_id, None)
            self._generations.pop(job_id, None)
            self._watchers.pop(job_id, None)
        unregister_process(process.pid, generation=generation)

    async def _terminate_owned(self, job_id: str, *, cancelled: bool = False) -> None:
        async with self._lock:
            process = self._processes.get(job_id)
            record = self._jobs.get(job_id)
            generation = self._generations.get(job_id)
        if process is None or record is None:
            return
        if process.returncode is not None:
            watcher = self._watchers.get(job_id)
            if watcher is not None and not watcher.done():
                await watcher
            return
        status = JobState.JOB_CANCELLED if cancelled else JobState.PROCESS_TIMEOUT
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is not None and current.status is JobState.JOB_RUNNING:
                # Claim the terminal state before waiting so the watcher cannot
                # turn an explicit stop/timeout into a generic process failure.
                self._jobs[job_id] = replace(current, status=status)
        if process.returncode is None:
            _terminate_process_group(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                _kill_process_group(process)
                await process.wait()
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is not None and current.status is status:
                self._jobs[job_id] = replace(
                    current,
                    exit_code=process.returncode,
                )
            self._processes.pop(job_id, None)
            self._generations.pop(job_id, None)
        if generation is not None:
            unregister_process(process.pid, generation=generation)


class ApprovalExecutor:
    """The only component allowed to consume grants and spawn owned jobs."""

    def __init__(self, approvals: ApprovalManager, jobs: JobRuntime) -> None:
        self._approvals = approvals
        self._jobs = jobs

    async def execute(
        self,
        intent: CommandIntent,
        *,
        background: bool,
        port: int | None = None,
        health_url: str | None = None,
        job_wait_timeout_seconds: float = 5.0,
        process_timeout_seconds: float | None = None,
    ) -> JobRecord:
        intent.verify_integrity()
        _validate_start_options(
            background=background,
            port=port,
            health_url=health_url,
            job_wait_timeout_seconds=job_wait_timeout_seconds,
            process_timeout_seconds=process_timeout_seconds,
        )
        approval = await self._approvals.consume(intent)
        return await self._jobs.start(
            approval,
            background=background,
            port=port,
            health_url=health_url,
            job_wait_timeout_seconds=job_wait_timeout_seconds,
            process_timeout_seconds=process_timeout_seconds,
            _executor_token=self._jobs._executor_token,
        )


def _job_environment() -> dict[str, str]:
    keys = (
        "PATH",
        "HOME",
        "TMP",
        "TMPDIR",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "SYSTEMROOT",
        "WINDIR",
        "ComSpec",
        "PATHEXT",
        "WSL_INTEROP",
    )
    environment = {key: value for key in keys if (value := os.environ.get(key))}
    environment.setdefault("PATH", os.defpath)
    environment.setdefault("TERM", "dumb")
    return environment


async def _spawn_owned_process(approval: ApprovalRecord) -> asyncio.subprocess.Process:
    """Spawn a job in a dedicated process group with typed platform branches."""
    if os.name == "nt":
        creation_flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        if creation_flags:
            return await asyncio.create_subprocess_exec(
                *approval.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(approval.cwd),
                env=_job_environment(),
                creationflags=creation_flags,
            )
        return await asyncio.create_subprocess_exec(
            *approval.argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(approval.cwd),
            env=_job_environment(),
        )
    return await asyncio.create_subprocess_exec(
        *approval.argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(approval.cwd),
        env=_job_environment(),
        start_new_session=True,
    )


def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        if process.returncode is not None:
            return
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
    except OSError, ProcessLookupError:
        process.terminate()


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        if process.returncode is not None:
            return
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except OSError, ProcessLookupError:
        process.kill()


async def _port_is_listening(port: int) -> bool:
    try:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    await writer.wait_closed()
    return True


def _validate_loopback_health_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("health_url must be an http loopback URL")


async def _health_is_ok(url: str) -> bool:
    def request() -> bool:
        try:
            with urlopen(url, timeout=1.0) as response:
                return 200 <= response.status < 400
        except OSError:
            return False

    return await asyncio.to_thread(request)


__all__ = [
    "ApprovalExecutor",
    "JobRecord",
    "JobRuntime",
    "JobRuntimeError",
    "JobState",
]


def _validate_start_options(
    *,
    background: bool,
    port: int | None,
    health_url: str | None,
    job_wait_timeout_seconds: float,
    process_timeout_seconds: float | None,
) -> None:
    if not isinstance(background, bool):
        raise ValueError("background must be a boolean")
    if not math.isfinite(job_wait_timeout_seconds) or job_wait_timeout_seconds < 0:
        raise ValueError("job_wait_timeout_seconds must be finite and non-negative")
    if process_timeout_seconds is not None and (
        not math.isfinite(process_timeout_seconds) or process_timeout_seconds <= 0
    ):
        raise ValueError("process_timeout_seconds must be finite and greater than zero")
    if background and process_timeout_seconds is not None:
        raise ValueError("process_timeout_seconds applies only to foreground jobs")
    if background and port is None and health_url is None:
        raise ValueError("background jobs require port or health_url readiness")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if health_url is not None:
        _validate_loopback_health_url(health_url)

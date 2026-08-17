"""Async JSON-RPC stdio transport for the optional DeepSeek Harness runtime.

The runtime is deliberately kept behind a subprocess boundary.  Cordis owns
plugin loading and agent execution; this module only transports validated
JSON-RPC frames and exposes runtime notifications to the bridge.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import uuid
from collections import deque
from collections.abc import AsyncIterator, Mapping
from typing import Any

from loguru import logger

from .config import HarnessConfig
from .protocol import (
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    decode_message,
    encode_message,
)

# Keep the child useful for locating its executable and writing temporary
# files without forwarding provider tokens, shell credentials, or unrelated
# application configuration from the host process.
_INHERITED_ENV_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "TMP",
    "TEMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "SYSTEMROOT",
    "WINDIR",
    "ComSpec",
    "PATHEXT",
)


class HarnessProcessError(RuntimeError):
    """Base error raised by the runtime transport."""


class HarnessRuntimeClosedError(HarnessProcessError):
    """The runtime exited before completing a request."""


class HarnessRequestTimeoutError(HarnessProcessError, TimeoutError):
    """A JSON-RPC request exceeded its configured timeout."""


class HarnessRpcError(HarnessProcessError):
    """The runtime returned a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class HarnessProtocolError(HarnessProcessError):
    """The runtime emitted a malformed or otherwise invalid frame."""


class HarnessProcess:
    """Own one Cordis runtime subprocess and multiplex JSON-RPC messages.

    A process is intentionally single-owner.  Multiple callers may issue
    requests, but request correlation and notification fan-out are handled in
    one reader task so stdout is never consumed by competing coroutines.
    """

    def __init__(
        self,
        config: HarnessConfig,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.environment = dict(environment or {})
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._subscribers: set[asyncio.Queue[Any]] = set()
        self._stderr_tail: deque[bytes] = deque()
        self._stderr_size = 0
        self._closed = False
        self._close_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Return whether the owned subprocess is currently alive."""
        return self.process is not None and self.process.returncode is None

    @property
    def stderr_tail(self) -> str:
        """Return a bounded diagnostic tail without exposing stdout payloads."""
        return b"".join(self._stderr_tail).decode("utf-8", errors="replace")

    async def start(self) -> None:
        """Start the runtime and its stdout/stderr reader tasks."""
        async with self._state_lock:
            if self.is_running:
                return
            argv = self.config.command_argv
            if not argv:
                raise HarnessProcessError(
                    "DeepSeek Harness runtime command is not configured"
                )
            env = {
                key: value
                for key in _INHERITED_ENV_KEYS
                if (value := os.environ.get(key)) is not None
            }
            env.update(self.environment)
            if self.config.cordis_config is not None:
                env["DSH_CORDIS_CONFIG"] = str(self.config.cordis_config)
            cwd = (
                str(self.config.workspace_root)
                if self.config.workspace_root is not None
                else None
            )
            spawn_kwargs: dict[str, Any] = {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": cwd,
                "env": env,
                "limit": self.config.max_frame_bytes + 2,
            }
            if os.name == "nt":
                creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if creation_flags:
                    spawn_kwargs["creationflags"] = creation_flags
            else:
                spawn_kwargs["start_new_session"] = True
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *argv, **spawn_kwargs
                )
            except OSError as exc:
                self.process = None
                raise HarnessProcessError(
                    f"Unable to start DeepSeek Harness runtime: {exc}"
                ) from exc
            self._closed = False
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name="dsh-runtime-reader"
            )
            self._stderr_task = asyncio.create_task(
                self._stderr_loop(), name="dsh-runtime-stderr"
            )
            self._wait_task = asyncio.create_task(
                self._wait_loop(), name="dsh-runtime-wait"
            )

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Send a JSON-RPC request and await its correlated result."""
        if not self.is_running or self.process is None:
            raise HarnessRuntimeClosedError("DeepSeek Harness runtime is not running")

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        async with self._state_lock:
            if self._closed or not self.is_running:
                raise HarnessRuntimeClosedError(
                    "DeepSeek Harness runtime is not running"
                )
            self._pending[request_id] = future

        try:
            await self._send(JsonRpcRequest(request_id, method, params))
            timeout = (
                self.config.request_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            )
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except TimeoutError as exc:
                raise HarnessRequestTimeoutError(
                    f"DeepSeek Harness request timed out: {method}"
                ) from exc
        finally:
            async with self._state_lock:
                self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        """Send a JSON-RPC notification without waiting for a response."""
        if not self.is_running:
            raise HarnessRuntimeClosedError("DeepSeek Harness runtime is not running")
        await self._send(JsonRpcNotification(method, params))

    def subscribe(self) -> asyncio.Queue[Any]:
        """Subscribe to runtime notifications.

        A queue receives :class:`JsonRpcNotification` instances.  On transport
        failure a ``HarnessRuntimeClosedError`` instance is placed in the queue
        so consumers can stop without polling process state.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        self._subscribers.discard(queue)

    async def notifications(self, queue: asyncio.Queue[Any]) -> AsyncIterator[Any]:
        """Yield notifications from a subscription until the runtime closes."""
        try:
            while True:
                item = await queue.get()
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            self.unsubscribe(queue)

    async def close(self) -> None:
        """Ask the runtime to shut down, then terminate/kill if necessary."""
        async with self._close_lock:
            process = self.process
            if process is None:
                return
            if process.returncode is None:
                try:
                    await self.request(
                        "shutdown",
                        None,
                        timeout_seconds=self.config.shutdown_timeout_seconds,
                    )
                except Exception as exc:
                    logger.debug("DSH shutdown request failed: {}", type(exc).__name__)
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                        await process.stdin.wait_closed()
                    except BrokenPipeError, ConnectionError, RuntimeError:
                        pass
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=self.config.shutdown_timeout_seconds
                    )
                except TimeoutError:
                    self._terminate_process(process)
                    try:
                        await asyncio.wait_for(
                            process.wait(), timeout=self.config.shutdown_timeout_seconds
                        )
                    except TimeoutError:
                        self._kill_process(process)
                        await process.wait()
            self._closed = True
            await self._finish_tasks()
            await self._fail_pending(
                HarnessRuntimeClosedError("DeepSeek Harness runtime closed")
            )
            self.process = None

    async def _send(self, message: Any) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise HarnessRuntimeClosedError("DeepSeek Harness runtime is not running")
        try:
            encoded = encode_message(message, max_bytes=self.config.max_frame_bytes)
            async with self._write_lock:
                process.stdin.write(encoded.encode("utf-8"))
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise HarnessRuntimeClosedError(
                "Failed to write to DeepSeek Harness runtime"
            ) from exc

    async def _reader_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        failure: BaseException | None = None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    message = decode_message(
                        line, max_bytes=self.config.max_frame_bytes
                    )
                except Exception as exc:  # protocol errors are transport-fatal
                    failure = HarnessProtocolError(
                        f"Invalid DeepSeek Harness stdout frame: {type(exc).__name__}"
                    )
                    break
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = HarnessRuntimeClosedError(
                f"DeepSeek Harness stdout reader failed: {type(exc).__name__}"
            )
        finally:
            if failure is None:
                failure = HarnessRuntimeClosedError(
                    "DeepSeek Harness runtime stdout closed"
                )
            await self._fail_pending(failure)

    async def _dispatch(self, message: Any) -> None:
        if isinstance(message, JsonRpcResponse):
            key = str(message.request_id)
            async with self._state_lock:
                future = self._pending.get(key)
            if future is None or future.done():
                return
            if message.error is not None:
                future.set_exception(
                    HarnessRpcError(
                        message.error.code,
                        message.error.message,
                        message.error.data,
                    )
                )
            else:
                future.set_result(message.result)
            return
        if isinstance(message, JsonRpcNotification):
            for subscriber in tuple(self._subscribers):
                subscriber.put_nowait(message)

    async def _stderr_loop(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                self._append_stderr(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _wait_loop(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            await process.wait()
        except asyncio.CancelledError:
            raise
        finally:
            if not self._closed:
                await self._fail_pending(
                    HarnessRuntimeClosedError(
                        f"DeepSeek Harness runtime exited (code={process.returncode})"
                    )
                )

    async def _fail_pending(self, error: BaseException) -> None:
        async with self._state_lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)
        for subscriber in tuple(self._subscribers):
            subscriber.put_nowait(error)

    async def _finish_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._reader_task, self._stderr_task, self._wait_task)
            if task is not None and task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        self._wait_task = None

    def _append_stderr(self, chunk: bytes) -> None:
        limit = self.config.max_stderr_bytes
        if len(chunk) > limit:
            chunk = chunk[-limit:]
        self._stderr_tail.append(chunk)
        self._stderr_size += len(chunk)
        while self._stderr_size > limit and self._stderr_tail:
            removed = self._stderr_tail.popleft()
            self._stderr_size -= len(removed)

    @staticmethod
    def _terminate_process(process: asyncio.subprocess.Process) -> None:
        try:
            if process.returncode is None:
                if os.name == "posix":
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    except ProcessLookupError, OSError:
                        process.terminate()
                elif hasattr(signal, "CTRL_BREAK_EVENT"):
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
        except ProcessLookupError, OSError:
            return

    @staticmethod
    def _kill_process(process: asyncio.subprocess.Process) -> None:
        try:
            if process.returncode is None:
                if os.name == "posix":
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except ProcessLookupError, OSError:
                        process.kill()
                else:
                    process.kill()
        except ProcessLookupError, OSError:
            return


__all__ = [
    "HarnessProcess",
    "HarnessProcessError",
    "HarnessProtocolError",
    "HarnessRequestTimeoutError",
    "HarnessRpcError",
    "HarnessRuntimeClosedError",
]

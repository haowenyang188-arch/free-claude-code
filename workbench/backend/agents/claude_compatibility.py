"""Claude CLI compatibility approval bridge for Workbench.

Claude Code does not expose Codex's app-server protocol.  Its documented
headless stream-json control channel does expose ``can_use_tool`` requests,
which lets Workbench answer one concrete permission request without driving a
terminal or pretending this is a native Claude app-server adapter.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cli.process_registry import register_process, unregister_process
from cli.runtime_environment import build_cli_environment
from cli.runtime_registry import RuntimeBackend

from ..runtime.approval import (
    ApprovalManager,
    ApprovalRecord,
    ApprovalState,
    CommandIntent,
    CommandSyntaxError,
)

ApprovalPendingCallback = Callable[[ApprovalRecord], Awaitable[None]]


class ClaudeCompatibilityError(RuntimeError):
    """Raised when Claude's headless control stream cannot be used safely."""


class ClaudeCompatibilitySession:
    """Run one Claude CLI print turn with Workbench permission mediation."""

    shell = False
    _CONTROL_DRAIN_TIMEOUT_SECONDS = 1.0

    def __init__(
        self,
        workspace_path: str | Path,
        *,
        claude_bin: str = "claude",
        model: str | None = None,
        approval_manager: ApprovalManager | None = None,
        on_approval_pending: ApprovalPendingCallback | None = None,
        approval_timeout_seconds: float = 300.0,
    ) -> None:
        workspace = Path(workspace_path).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace_path must reference a directory")
        if approval_timeout_seconds < 0:
            raise ValueError("approval_timeout_seconds must be non-negative")
        self.workspace = workspace
        self.claude_bin = claude_bin
        self.model = model
        self.approval_manager = approval_manager
        self.on_approval_pending = on_approval_pending
        self.approval_timeout_seconds = approval_timeout_seconds
        self.process: asyncio.subprocess.Process | None = None
        self._process_generation: str | None = None
        self.current_session_id: str | None = None
        self.current_turn_id: str | None = None
        self.generation: str | None = None
        self._is_busy = False
        self._lock = asyncio.Lock()
        self._terminal_seen = False
        self._control_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_control_intents: dict[str, CommandIntent] = {}
        self._cancelled_control_requests: set[str] = set()

    @property
    def is_busy(self) -> bool:
        return self._is_busy

    def build_command(
        self,
        *,
        session_id: str | None = None,
        fork_session: bool = False,
    ) -> list[str]:
        command = [self.claude_bin]
        if session_id:
            command.extend(["--resume", session_id])
            if fork_session:
                command.append("--fork-session")
        command.extend(
            [
                "-p",
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "auto",
                "--permission-prompt-tool",
                "stdio",
                "--setting-sources",
                "user",
                "--strict-mcp-config",
            ]
        )
        if self.model:
            command.extend(["--model", self.model])
        return command

    async def start_task(
        self,
        prompt: str,
        session_id: str | None = None,
        fork_session: bool = False,
        generation: str | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Claude prompt must not be empty")
        async with self._lock:
            if self._is_busy:
                raise RuntimeError("Claude compatibility session is busy")
            self._is_busy = True
            self.generation = generation or uuid.uuid4().hex
            self.current_turn_id = uuid.uuid4().hex
            self._terminal_seen = False
            process: asyncio.subprocess.Process | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.build_command(
                        session_id=session_id,
                        fork_session=fork_session,
                    ),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=str(self.workspace),
                    env=build_cli_environment(RuntimeBackend.CLAUDE),
                    start_new_session=os.name == "posix",
                )
                self.process = process
                if process.pid:
                    self._process_generation = self.generation
                    register_process(process.pid, generation=self.generation)
                await self._send_user_message(prompt)
                async for event in self._read_events():
                    yield event
                    if event.get("type") == "exit":
                        self._terminal_seen = True
                        # A result can be emitted in the same read cycle as a
                        # permission request. Give that request a chance to
                        # finish (or cancel it) before closing the transport;
                        # otherwise the response task loses its stdin pipe.
                        await self._finish_control_tasks()
                        if process.stdin is not None:
                            process.stdin.close()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=5.0)
                        except TimeoutError:
                            await self._terminate_process(process)
                        if (
                            self.approval_manager is not None
                            and self.current_session_id is not None
                            and self.current_turn_id is not None
                        ):
                            await self.approval_manager.clear_turn(
                                provider="claude_cli",
                                session_id=self.current_session_id,
                                thread_id=self.current_session_id,
                                turn_id=self.current_turn_id,
                            )
                        break
                if not self._terminal_seen:
                    return_code = await process.wait()
                    yield {"type": "exit", "code": return_code, "stderr": None}
                    self._terminal_seen = True
            finally:
                if process is not None and process.returncode is None:
                    await self._terminate_process(process)
                self._release_process(process)
                self._is_busy = False

    async def _send_user_message(self, prompt: str) -> None:
        await self._send(
            {
                "type": "user",
                "session_id": self.current_session_id or "",
                "parent_tool_use_id": None,
                "uuid": str(uuid.uuid4()),
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                },
            }
        )

    async def _read_events(self) -> AsyncGenerator[dict[str, Any]]:
        process = self.process
        if process is None or process.stdout is None:
            raise ClaudeCompatibilityError("Claude stdout is unavailable")
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ClaudeCompatibilityError(
                    "Claude emitted invalid stream-json"
                ) from exc
            if not isinstance(message, Mapping):
                raise ClaudeCompatibilityError("Claude emitted a non-object message")
            if message.get("type") == "control_request":
                request_id = _text(message.get("request_id"))
                if request_id is not None:
                    task = asyncio.create_task(
                        self._handle_control_request(message),
                        name=f"claude-control-{request_id}",
                    )
                    self._control_tasks[request_id] = task
                else:
                    await self._handle_control_request(message)
                continue
            if message.get("type") == "control_cancel_request":
                await self._handle_control_cancel(message)
                continue
            event = self._event_from_message(message)
            if event is not None:
                yield event
                if event.get("type") == "exit":
                    await self._drain_control_tasks()
                    return

    async def _handle_control_request(self, message: Mapping[str, Any]) -> None:
        request_id = _text(message.get("request_id"))
        request = message.get("request")
        if request_id is None or not isinstance(request, Mapping):
            return
        try:
            if request.get("subtype") != "can_use_tool":
                await self._send_control_response(
                    request_id,
                    {
                        "behavior": "deny",
                        "message": "unsupported_permission_request",
                    },
                )
                return
            try:
                intent = self._intent_from_request(request, request_id)
            except CommandSyntaxError, ValueError:
                intent = None
            if intent is None or self.approval_manager is None:
                await self._send_control_response(
                    request_id,
                    {"behavior": "deny", "message": "approval_unavailable"},
                )
                return
            record = await self.approval_manager.request(
                intent, approval_timeout_seconds=self.approval_timeout_seconds
            )
            self._pending_control_intents[request_id] = intent
            if request_id in self._cancelled_control_requests:
                self._cancelled_control_requests.discard(request_id)
                observed = await self._cancel_intent(intent)
            else:
                observed = record
                if record.status is ApprovalState.PENDING:
                    if self.on_approval_pending is not None:
                        await self.on_approval_pending(record)
                    observed = await self.approval_manager.wait_for_terminal(intent)
            if observed.status is ApprovalState.APPROVED:
                try:
                    await self.approval_manager.consume(intent)
                except Exception:
                    await self._send_control_response(
                        request_id,
                        {"behavior": "deny", "message": "approval_unavailable"},
                    )
                    return
                tool_input = request.get("input")
                updated_input = (
                    dict(tool_input) if isinstance(tool_input, Mapping) else {}
                )
                await self._send_control_response(
                    request_id,
                    {
                        "behavior": "allow",
                        "updatedInput": updated_input,
                        "toolUseID": intent.call_id,
                    },
                )
                return
            if observed.status is ApprovalState.CANCELLED:
                await self._send_control_error(request_id, "approval_cancelled")
                return
            reason = observed.reason or observed.status.value
            await self._send_control_response(
                request_id,
                {"behavior": "deny", "message": reason, "toolUseID": intent.call_id},
            )
        finally:
            self._pending_control_intents.pop(request_id, None)
            self._cancelled_control_requests.discard(request_id)
            task = self._control_tasks.get(request_id)
            if task is asyncio.current_task():
                self._control_tasks.pop(request_id, None)

    async def _handle_control_cancel(self, message: Mapping[str, Any]) -> None:
        request_id = _text(message.get("request_id"))
        if request_id is None:
            return
        self._cancelled_control_requests.add(request_id)
        intent = self._pending_control_intents.get(request_id)
        if intent is not None:
            await self._cancel_intent(intent)
        task = self._control_tasks.get(request_id)
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(Exception):
                await task

    async def _cancel_intent(self, intent: CommandIntent) -> ApprovalRecord:
        if self.approval_manager is None:
            raise ClaudeCompatibilityError("approval manager is unavailable")
        try:
            return await self.approval_manager.cancel(
                provider=intent.provider,
                session_id=intent.session_id,
                call_id=intent.call_id,
                command_hash=intent.command_hash,
            )
        except Exception:
            return await self.approval_manager.get(
                provider=intent.provider,
                session_id=intent.session_id,
                call_id=intent.call_id,
            )

    async def _drain_control_tasks(self) -> None:
        tasks = tuple(self._control_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _finish_control_tasks(self) -> None:
        """Complete or cancel outstanding approvals before process teardown."""
        if not self._control_tasks:
            return
        # A well-behaved provider waits for the permission response. Give a
        # response already being approved a short chance to finish before
        # treating an early provider result as cancellation.
        drain_task = asyncio.create_task(self._drain_control_tasks())
        try:
            await asyncio.wait_for(
                asyncio.shield(drain_task),
                timeout=self._CONTROL_DRAIN_TIMEOUT_SECONDS,
            )
            return
        except TimeoutError:
            pass
        # The provider exited first. Cancel only the still-pending grants, then
        # drain their response tasks while the transport remains open.
        for intent in tuple(self._pending_control_intents.values()):
            with contextlib.suppress(Exception):
                await self._cancel_intent(intent)
        with contextlib.suppress(Exception):
            await drain_task

    async def _send_control_response(
        self, request_id: str, response: Mapping[str, Any]
    ) -> None:
        await self._send(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": dict(response),
                },
            }
        )

    async def _send_control_error(self, request_id: str, error: str) -> None:
        await self._send(
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": error,
                },
            }
        )

    def _intent_from_request(
        self, request: Mapping[str, Any], request_id: str
    ) -> CommandIntent | None:
        session_id = self.current_session_id
        tool_name = _text(request.get("tool_name"))
        tool_input = request.get("input")
        call_id = _text(request.get("tool_use_id")) or request_id
        if (
            session_id is None
            or tool_name is None
            or not isinstance(tool_input, Mapping)
        ):
            return None
        cwd = self._resolve_cwd(tool_input.get("cwd") or request.get("cwd"))
        if cwd is None:
            return None
        try:
            canonical_input = _canonical_json(tool_input)
        except TypeError, ValueError:
            return None
        lowered = tool_name.lower()
        if lowered in {"bash", "powershell", "shell"}:
            command = tool_input.get("command") or tool_input.get("cmd")
            if not isinstance(command, str):
                return None
            return CommandIntent.create(
                provider="claude_cli",
                session_id=session_id,
                thread_id=session_id,
                turn_id=self.current_turn_id,
                item_id=call_id,
                call_id=call_id,
                command=command,
                cwd=cwd,
                workspace_target=self.workspace,
                requested_permission="process_spawn",
                permission_scope=_command_scope(command),
                patch_identity=canonical_input,
            )
        if lowered in {"read", "glob", "grep"}:
            path = _text(
                tool_input.get("file_path")
                or tool_input.get("path")
                or tool_input.get("pattern")
            )
            if path is None or not _valid_path_reference(path):
                return None
            argv = ("cat", path) if lowered == "read" else ("grep", path)
            return CommandIntent.create(
                provider="claude_cli",
                session_id=session_id,
                thread_id=session_id,
                turn_id=self.current_turn_id,
                item_id=call_id,
                call_id=call_id,
                argv=argv,
                cwd=cwd,
                workspace_target=self.workspace,
                requested_permission="filesystem",
                permission_scope=f"filesystem:read:{_resolve_path(path, self.workspace)}",
                patch_identity=canonical_input,
            )
        if lowered in {"edit", "write", "notebookedit"}:
            path = _text(tool_input.get("file_path") or tool_input.get("path"))
            if path is None or not _valid_path_reference(path):
                return None
            resolved = _resolve_path(path, self.workspace)
            return CommandIntent.create(
                provider="claude_cli",
                session_id=session_id,
                thread_id=session_id,
                turn_id=self.current_turn_id,
                item_id=call_id,
                call_id=call_id,
                argv=("claude-file-change", lowered, str(resolved)),
                cwd=cwd,
                workspace_target=self.workspace,
                requested_permission="filesystem",
                permission_scope=f"filesystem:write:{resolved}",
                patch_identity=canonical_input,
            )
        url = _text(tool_input.get("url"))
        if url is not None:
            host = urlparse(url).hostname
            if not host:
                return None
            return CommandIntent.create(
                provider="claude_cli",
                session_id=session_id,
                thread_id=session_id,
                turn_id=self.current_turn_id,
                item_id=call_id,
                call_id=call_id,
                argv=("claude-network", host.lower()),
                cwd=cwd,
                workspace_target=self.workspace,
                requested_permission="network",
                permission_scope=f"network:{host.lower()}",
                patch_identity=canonical_input,
            )
        return CommandIntent.create(
            provider="claude_cli",
            session_id=session_id,
            thread_id=session_id,
            turn_id=self.current_turn_id,
            item_id=call_id,
            call_id=call_id,
            argv=("claude-tool", lowered),
            cwd=cwd,
            workspace_target=self.workspace,
            requested_permission=f"tool:{lowered}",
            patch_identity=canonical_input,
        )

    def _resolve_cwd(self, value: Any) -> Path | None:
        candidate = self.workspace if not isinstance(value, str) else Path(value)
        try:
            resolved = candidate.expanduser().resolve(strict=True)
            resolved.relative_to(self.workspace)
        except OSError, ValueError:
            return None
        return resolved if resolved.is_dir() else None

    async def _send(self, message: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ClaudeCompatibilityError("Claude process is not running")
        process.stdin.write(
            (
                json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n"
            ).encode()
        )
        await process.stdin.drain()

    async def stop(self) -> bool:
        process = self.process
        if process is None:
            return False
        await self._terminate_process(process)
        self._release_process(process)
        self.process = None
        self.current_session_id = None
        return True

    async def pause(self) -> bool:
        if self.process is None or self.process.returncode is not None:
            return False
        if os.name == "posix":
            os.kill(self.process.pid, signal.SIGSTOP)
        return True

    async def resume(self) -> bool:
        if self.process is None or self.process.returncode is not None:
            return False
        if os.name == "posix":
            os.kill(self.process.pid, signal.SIGCONT)
        return True

    async def send_message(self, message: str) -> bool:
        return bool(message.strip()) and not self.is_busy

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if not process.pid or process.pid <= 0:
            with contextlib.suppress(OSError):
                process.terminate()
            return
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except OSError:
                with contextlib.suppress(OSError):
                    process.terminate()
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    def _release_process(self, process: asyncio.subprocess.Process | None) -> None:
        if process is not None and process.pid and self._process_generation:
            unregister_process(process.pid, generation=self._process_generation)
        if self.process is process:
            self.process = None
        self._process_generation = None

    def _event_from_message(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        message_type = message.get("type")
        if message_type == "system" and message.get("subtype") == "init":
            session_id = _text(message.get("session_id")) or _text(
                message.get("sessionId")
            )
            if session_id:
                self.current_session_id = session_id
                return {"type": "session_info", "session_id": session_id}
            return None
        if message_type == "assistant":
            content = message.get("message")
            return (
                {"type": "assistant", "message": dict(content)}
                if isinstance(content, Mapping)
                else None
            )
        if message_type == "result":
            subtype = _text(message.get("subtype")) or "success"
            code = 0 if subtype == "success" and not message.get("is_error") else 1
            return {"type": "exit", "code": code, "stderr": None}
        return None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _resolve_path(value: str, workspace: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    return candidate.resolve(strict=False)


def _valid_path_reference(value: str) -> bool:
    return (
        "\x00" not in value
        and "\n" not in value
        and "\r" not in value
        and not (
            value.startswith(("\\\\", "//"))
            or (len(value) >= 3 and value[1] == ":" and value[2] in "/\\")
        )
    )


def _command_scope(command: str) -> str | None:
    for token in command.split():
        if token.startswith(("http://", "https://")):
            host = urlparse(token).hostname
            if host:
                return f"network:{host.lower()}"
    return None


__all__ = ["ClaudeCompatibilityError", "ClaudeCompatibilitySession"]

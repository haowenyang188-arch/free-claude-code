"""Structured Codex app-server session for Workbench-managed approvals.

The legacy ``codex exec`` wrapper remains available to the messaging runtime.
Workbench uses this session when it needs native approval callbacks that carry
the complete command identity instead of a terminal prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

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


class CodexAppServerError(RuntimeError):
    """Raised when the structured app-server transport is unusable."""


class CodexAppServerSession:
    """Run Codex turns over JSON-RPC with one-shot Workbench approvals."""

    shell = False

    def __init__(
        self,
        workspace_path: str | Path,
        *,
        codex_bin: str = "codex",
        sandbox_mode: str = "read-only",
        model: str | None = None,
        approval_manager: ApprovalManager | None = None,
        on_approval_pending: ApprovalPendingCallback | None = None,
        approval_timeout_seconds: float = 300.0,
    ) -> None:
        workspace = Path(workspace_path).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace_path must reference a directory")
        if sandbox_mode not in {"read-only", "workspace-write"}:
            raise ValueError("sandbox_mode must be read-only or workspace-write")
        if approval_timeout_seconds < 0:
            raise ValueError("approval_timeout_seconds must be non-negative")
        self.workspace = workspace
        self.codex_bin = codex_bin
        self.sandbox_mode = sandbox_mode
        self.model = model
        self.approval_manager = approval_manager
        self.on_approval_pending = on_approval_pending
        self.approval_timeout_seconds = approval_timeout_seconds
        self.process: asyncio.subprocess.Process | None = None
        self._process_generation: str | None = None
        self.current_session_id: str | None = None
        self.generation: str | None = None
        self._request_id = 0
        self._initialized = False
        self._is_busy = False
        self._lock = asyncio.Lock()
        self._early_notifications: list[dict[str, Any]] = []
        self._file_change_patches: dict[tuple[str, str], str] = {}

    @property
    def is_busy(self) -> bool:
        return self._is_busy

    async def start_task(
        self,
        prompt: str,
        session_id: str | None = None,
        fork_session: bool = False,
        generation: str | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Codex prompt must not be empty")
        async with self._lock:
            if self._is_busy:
                raise RuntimeError("Codex app-server session is busy")
            self._is_busy = True
            self.generation = generation or uuid.uuid4().hex
            try:
                await self._ensure_started()
                if session_id and session_id != self.current_session_id:
                    await self._open_thread(session_id, fork_session=fork_session)
                elif self.current_session_id is None:
                    await self._open_thread(None, fork_session=False)
                if self.current_session_id:
                    yield {"type": "session_info", "session_id": self.current_session_id}
                async for event in self._run_turn(prompt):
                    yield event
            finally:
                self._is_busy = False

    async def _ensure_started(self) -> None:
        if self.process is not None and self.process.returncode is None:
            if not self._initialized:
                try:
                    await self._initialize()
                except BaseException:
                    await self.stop()
                    raise
            return
        process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.workspace),
            env=build_cli_environment(RuntimeBackend.CODEX),
            start_new_session=os.name == "posix",
        )
        if process.pid:
            process_generation = self.generation or uuid.uuid4().hex
            try:
                register_process(process.pid, generation=process_generation)
            except Exception as exc:
                await self._terminate_process(process)
                raise CodexAppServerError(
                    "app-server process ownership failed"
                ) from exc
            self._process_generation = process_generation
        self.process = process
        try:
            await self._initialize()
        except BaseException:
            await self.stop()
            raise

    async def _initialize(self) -> None:
        result = await self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "free-claude-code-workbench",
                    "title": "Free Claude Code Workbench",
                    "version": "2.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        if not isinstance(result, Mapping):
            raise CodexAppServerError("invalid app-server initialize response")
        await self._notify("initialized")
        self._initialized = True

    async def _open_thread(self, session_id: str | None, *, fork_session: bool) -> None:
        if session_id:
            method = "thread/fork" if fork_session else "thread/resume"
            params: dict[str, Any] = {
                "threadId": session_id,
                "cwd": str(self.workspace),
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": self.sandbox_mode,
            }
        else:
            method = "thread/start"
            params = {
                "cwd": str(self.workspace),
                "runtimeWorkspaceRoots": [str(self.workspace)],
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": self.sandbox_mode,
                "model": self.model,
                "threadSource": "codex-exec",
            }
        result = await self._request(method, params)
        thread = result.get("thread") if isinstance(result, Mapping) else None
        thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexAppServerError("app-server thread response has no thread id")
        self.current_session_id = thread_id
        self._early_notifications.clear()

    async def _run_turn(self, prompt: str) -> AsyncGenerator[dict[str, Any]]:
        thread_id = self.current_session_id
        if not thread_id:
            raise CodexAppServerError("app-server thread is not initialized")
        request_id = await self._send_request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
            },
        )
        turn_response_seen = False
        while True:
            message = await self._read_message()
            if message.get("id") == request_id:
                if "error" in message:
                    raise CodexAppServerError("app-server turn/start failed")
                turn_response_seen = True
                continue
            if "id" in message and "method" in message:
                await self._handle_server_request(message)
                continue
            if not turn_response_seen and "result" in message:
                continue
            event = await self._notification_event(message)
            if event is not None:
                yield event
                if event.get("type") == "exit":
                    return

    async def _request(self, method: str, params: Mapping[str, Any] | None) -> Any:
        request_id = await self._send_request(method, params)
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                message = await self._read_message()
                if message.get("id") == request_id:
                    if "error" in message:
                        raise CodexAppServerError(
                            f"app-server request failed: {method}"
                        )
                    return message.get("result")
                if "id" in message and "method" in message:
                    await self._handle_server_request(message)
                else:
                    # Keep unrelated notifications/responses for the next
                    # protocol phase without re-reading the same item forever.
                    deferred.append(message)
        finally:
            if deferred:
                self._early_notifications = deferred + self._early_notifications

    async def _send_request(self, method: str, params: Mapping[str, Any] | None) -> int:
        self._request_id += 1
        await self._send(
            {"id": self._request_id, "method": method, "params": params or {}}
        )
        return self._request_id

    async def _notify(self, method: str) -> None:
        await self._send({"method": method})

    async def _send(self, message: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise CodexAppServerError("app-server process is not running")
        process.stdin.write(
            (
                json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n"
            ).encode()
        )
        await process.stdin.drain()

    async def _read_message(self) -> dict[str, Any]:
        if self._early_notifications:
            return self._early_notifications.pop(0)
        process = self.process
        if process is None or process.stdout is None:
            raise CodexAppServerError("app-server stdout is unavailable")
        line = await process.stdout.readline()
        if not line:
            raise CodexAppServerError("app-server stdout closed")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexAppServerError("app-server emitted invalid JSON") from exc
        if not isinstance(message, dict):
            raise CodexAppServerError("app-server emitted a non-object message")
        return message

    async def _handle_server_request(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(request_id, (str, int)) or not isinstance(method, str):
            raise CodexAppServerError("invalid app-server request envelope")
        if not isinstance(params, Mapping):
            params = {}
        if method in {
            "item/commandExecution/requestApproval",
            "execCommandApproval",
            "item/fileChange/requestApproval",
            "applyPatchApproval",
        }:
            decision = await self._approval_decision(method, params)
            await self._send_response(request_id, {"decision": decision})
            return
        if method == "item/permissions/requestApproval":
            permissions, _decision = await self._permissions_decision(params)
            await self._send_response(
                request_id,
                {"permissions": permissions, "scope": "turn"},
            )
            return
        if method == "item/tool/requestUserInput":
            await self._send_response(request_id, {"answers": {}})
            return
        await self._send_response(
            request_id,
            error={"code": -32601, "message": "unsupported app-server request"},
        )

    async def _send_response(
        self,
        request_id: str | int,
        result: Mapping[str, Any] | None = None,
        *,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        message: dict[str, Any] = {"id": request_id}
        if error is not None:
            message["error"] = dict(error)
        else:
            message["result"] = dict(result or {})
        await self._send(message)

    async def _approval_decision(self, method: str, params: Mapping[str, Any]) -> str:
        intent = self._intent_from_approval(method, params)
        if intent is None or self.approval_manager is None:
            return "decline"
        record = await self.approval_manager.request(
            intent, approval_timeout_seconds=self.approval_timeout_seconds
        )
        observed = record
        if record.status is ApprovalState.PENDING:
            if self.on_approval_pending is not None:
                await self.on_approval_pending(record)
            observed = await self.approval_manager.wait_for_terminal(intent)
        if observed.status is ApprovalState.APPROVED:
            try:
                await self.approval_manager.consume(intent)
            except Exception:
                return "decline"
            return "accept"
        if observed.status is ApprovalState.CANCELLED:
            return "cancel"
        return "decline"

    async def _permissions_decision(
        self, params: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str]:
        permissions = params.get("permissions")
        if not isinstance(permissions, Mapping):
            return {}, "decline"
        thread_id = _text(params.get("threadId")) or self.current_session_id
        item_id = _text(params.get("itemId"))
        cwd = self._resolve_cwd(params.get("cwd"))
        if not thread_id or not item_id or cwd is None or self.approval_manager is None:
            return {}, "decline"
        try:
            canonical = _canonical_json(permissions)
        except (TypeError, ValueError):
            return {}, "decline"
        requested = {
            key: permissions[key]
            for key in ("network", "fileSystem")
            if key in permissions
        }
        if not requested:
            return {}, "decline"
        intent = CommandIntent.create(
            session_id=thread_id,
            call_id=item_id,
            argv=("codex-permission-request", canonical),
            cwd=cwd,
            requested_permission=_permission_label("sandbox_escalation", canonical),
        )
        record = await self.approval_manager.request(
            intent, approval_timeout_seconds=self.approval_timeout_seconds
        )
        observed = record
        if record.status is ApprovalState.PENDING:
            if self.on_approval_pending is not None:
                await self.on_approval_pending(record)
            observed = await self.approval_manager.wait_for_terminal(intent)
        if observed.status is ApprovalState.APPROVED:
            try:
                await self.approval_manager.consume(intent)
            except Exception:
                return {}, "decline"
            return requested, "accept"
        return {}, "decline"

    def _intent_from_approval(
        self, method: str, params: Mapping[str, Any]
    ) -> CommandIntent | None:
        thread_id = _text(params.get("threadId")) or _text(params.get("conversationId"))
        item_id = _text(params.get("itemId"))
        call_id = _text(params.get("callId")) or item_id
        approval_id = _text(params.get("approvalId"))
        if approval_id:
            call_id = approval_id
        cwd = self._resolve_cwd(params.get("cwd"))
        if not thread_id or not call_id or cwd is None:
            return None
        command = params.get("command")
        if isinstance(command, list):
            argv = tuple(value for value in command if isinstance(value, str))
            if not argv or len(argv) != len(command):
                return None
            requested_permission = (
                "file_write"
                if "fileChange" in method
                else self._command_permission(params)
            )
            if requested_permission is None:
                return None
            return CommandIntent.create(
                session_id=thread_id,
                call_id=call_id,
                argv=argv,
                cwd=cwd,
                requested_permission=requested_permission,
            )
        if method in {"item/fileChange/requestApproval", "applyPatchApproval"}:
            patch_identity = self._file_change_identity(method, params, thread_id, item_id)
            if patch_identity is None:
                return None
            return CommandIntent.create(
                session_id=thread_id,
                call_id=call_id,
                argv=("codex-file-change", item_id or call_id, patch_identity),
                cwd=cwd,
                requested_permission="file_write",
            )
        if not isinstance(command, str):
            return None
        requested_permission = self._command_permission(params)
        if requested_permission is None:
            return None
        try:
            return CommandIntent.create(
                session_id=thread_id,
                call_id=call_id,
                command=command,
                cwd=cwd,
                requested_permission=requested_permission,
            )
        except CommandSyntaxError, ValueError:
            return None

    def _command_permission(self, params: Mapping[str, Any]) -> str | None:
        """Bind native extra permissions without exposing their contents."""
        additional = params.get("additionalPermissions")
        network = params.get("networkApprovalContext")
        if additional is None and network is None:
            return "process_spawn"
        try:
            canonical = _canonical_json(
                {"additionalPermissions": additional, "networkApprovalContext": network}
            )
        except (TypeError, ValueError):
            return None
        return _permission_label("process_spawn", canonical)

    def _file_change_identity(
        self,
        method: str,
        params: Mapping[str, Any],
        thread_id: str,
        item_id: str | None,
    ) -> str | None:
        if method == "applyPatchApproval":
            changes = params.get("fileChanges")
            if not isinstance(changes, Mapping) or not changes:
                return None
            if not _file_changes_stay_in_workspace(changes, self.workspace):
                return None
            try:
                return _canonical_json(changes)
            except (TypeError, ValueError):
                return None
        if item_id is None:
            return None
        return self._file_change_patches.get((thread_id, item_id))

    def _resolve_cwd(self, value: Any) -> Path | None:
        candidate = (
            self.workspace
            if not isinstance(value, str) or not value.strip()
            else Path(value).expanduser()
        )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        try:
            resolved.relative_to(self.workspace)
        except ValueError:
            return None
        return resolved if resolved.is_dir() else None

    async def _notification_event(
        self, message: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, Mapping):
            return None
        if method == "item/agentMessage/delta":
            delta = _text(params.get("delta"))
            return _assistant_event(delta) if delta else None
        if method == "item/commandExecution/outputDelta":
            delta = _text(params.get("delta"))
            item_id = _text(params.get("itemId")) or "codex-command"
            if not delta:
                return None
            return _tool_result_event(item_id, delta)
        if method == "item/fileChange/patchUpdated":
            thread_id = _text(params.get("threadId")) or self.current_session_id
            item_id = _text(params.get("itemId"))
            changes = params.get("changes")
            if (
                thread_id
                and item_id
                and isinstance(changes, list)
                and changes
                and _patch_paths_stay_in_workspace(changes, self.workspace)
            ):
                with contextlib.suppress(TypeError, ValueError):
                    self._file_change_patches[(thread_id, item_id)] = _canonical_json(
                        changes
                    )
            return {"type": "file_change", "item": dict(params)}
        if method == "item/started":
            item = params.get("item")
            return _item_event(item, completed=False)
        if method == "item/completed":
            item = params.get("item")
            return _item_event(item, completed=True)
        if method == "turn/completed":
            turn = params.get("turn")
            status = turn.get("status") if isinstance(turn, Mapping) else "completed"
            code = 0 if status in {None, "completed"} else 1
            return {"type": "exit", "code": code, "stderr": None}
        if method == "error":
            return {"type": "error", "error": {"message": "Codex app-server error"}}
        return None

    async def stop(self) -> bool:
        process = self.process
        if process is None:
            return False
        try:
            await self._terminate_process(process)
        finally:
            if process.pid and self._process_generation:
                unregister_process(process.pid, generation=self._process_generation)
            self.process = None
            self._process_generation = None
            self.current_session_id = None
            self._initialized = False
            self._early_notifications.clear()
            self._file_change_patches.clear()
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


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _permission_label(prefix: str, canonical: str) -> str:
    return f"{prefix}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _patch_paths_stay_in_workspace(value: Any, workspace: Path) -> bool:
    """Reject patch identities that mention paths outside the native sandbox."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in {"path", "move_path"} and nested is not None and (
                not isinstance(nested, str)
                or not _path_stays_in_workspace(nested, workspace)
            ):
                return False
            if not _patch_paths_stay_in_workspace(nested, workspace):
                return False
        return True
    if isinstance(value, list):
        return all(_patch_paths_stay_in_workspace(item, workspace) for item in value)
    return True


def _file_changes_stay_in_workspace(
    changes: Mapping[Any, Any], workspace: Path
) -> bool:
    for path, change in changes.items():
        if not isinstance(path, str) or not _path_stays_in_workspace(path, workspace):
            return False
        if not _patch_paths_stay_in_workspace(change, workspace):
            return False
    return True


def _path_stays_in_workspace(value: str, workspace: Path) -> bool:
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = candidate.resolve(strict=False)
        candidate.relative_to(workspace)
    except (OSError, ValueError):
        return False
    return True


def _assistant_event(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _tool_result_event(tool_id: str, content: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": content,
                    "is_error": False,
                }
            ]
        },
    }


def _item_event(item: Any, *, completed: bool) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    item_type = item.get("type")
    item_id = _text(item.get("id")) or "codex-item"
    if item_type == "agentMessage":
        text = _text(item.get("text"))
        return _assistant_event(text) if text else None
    if item_type == "commandExecution":
        command = _text(item.get("command")) or "command"
        if completed:
            return _tool_result_event(
                item_id, _text(item.get("aggregatedOutput")) or ""
            )
        return {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": item_id,
                        "name": "command",
                        "input": command,
                    }
                ]
            },
        }
    if item_type == "fileChange":
        return {"type": "file_change", "item": dict(item)}
    return None


__all__ = ["CodexAppServerError", "CodexAppServerSession"]

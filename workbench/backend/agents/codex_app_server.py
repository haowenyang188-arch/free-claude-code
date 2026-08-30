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
import re
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
    ApprovalScope,
    ApprovalState,
    CommandIntent,
    CommandSyntaxError,
)
from ..runtime.codex_home import CodexHomeError, prepare_codex_home

ApprovalPendingCallback = Callable[[ApprovalRecord], Awaitable[None]]
_STREAM_LIMIT_BYTES = 4 * 1024 * 1024

# Error-notification observability (hotfix).
#
# The app-server reports provider / HTTP / retry failures through a JSON-RPC
# ``error`` notification. Emitting only a generic message made it impossible to
# tell an EXTERNAL provider outage from a CODE or PROTOCOL defect, which is
# exactly what a Health Gate has to distinguish. We now preserve a small
# whitelist of diagnostic fields — never the whole ``params`` envelope — and
# scrub anything that looks like a credential along the way.
#
# Fault CLASSIFICATION is deliberately NOT done here: the caller decides
# BLOCKED_EXTERNAL vs CODE_FAILURE from http status, codexErrorInfo, the
# provider error category and the terminal event. No brittle string matching
# against localised provider messages belongs in this module.
_ERROR_GENERIC_MESSAGE = "Codex app-server error"
_ERROR_DIAGNOSTIC_KEYS: dict[str, str] = {
    "message": "upstream_message",
    "codexErrorInfo": "upstream_error_info",
    "additionalDetails": "additional_details",
}
# Substring match, case-insensitive, applied to every key at every depth.
_ERROR_SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "authorization",
    "api_key",
    "apikey",
    "token",
    "cookie",
    "secret",
    "password",
    "credential",
)
# Free-form diagnostic text carries credentials as *values* (e.g. the app-server
# echoing a request URL or headers), which key-name filtering cannot catch.
# Matched on a key<sep>value shape so ordinary wording is not destroyed.
_ERROR_SECRET_PAIR_PATTERN = re.compile(
    r"(?P<prefix>(?:" + "|".join(_ERROR_SENSITIVE_KEY_TOKENS) + r")"
    r"""[\"']?\s*(?:=|:|":)\s*[\"']?)"""
    # An optional scheme prefix (e.g. ``Authorization: Bearer sk-...``) is part
    # of the secret, not the value boundary.
    r"(?:Bearer\s+|Basic\s+|Token\s+)?"
    r"(?P<secret>[^\s,;&}\)\"']+)",
    re.IGNORECASE,
)
_ERROR_BEARER_PATTERN = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


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
        codex_home: str | Path | None = None,
        approval_scope: str = ApprovalScope.NORMAL,
    ) -> None:
        workspace = Path(workspace_path).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace_path must reference a directory")
        if sandbox_mode not in {"read-only", "workspace-write"}:
            raise ValueError("sandbox_mode must be read-only or workspace-write")
        if approval_timeout_seconds < 0:
            raise ValueError("approval_timeout_seconds must be non-negative")
        if approval_scope not in (ApprovalScope.NORMAL, ApprovalScope.DENY_ONLY):
            raise ValueError("approval_scope must be 'normal' or 'deny_only'")
        if approval_scope is ApprovalScope.DENY_ONLY and sandbox_mode != "read-only":
            # F-2: a DENY_ONLY principal (Codex Reviewer) is FORCED read-only.
            # Thread-level sandbox is the protocol authority (turn/start has no
            # sandbox override), so this guard closes the only escalation path.
            raise ValueError(
                "reviewer session (approval_scope=deny_only) must use "
                "sandbox_mode='read-only'"
            )
        self.workspace = workspace
        self.codex_bin = codex_bin
        self.sandbox_mode = sandbox_mode
        self.approval_scope = approval_scope
        self.model = model
        self.approval_manager = approval_manager
        self.on_approval_pending = on_approval_pending
        self.approval_timeout_seconds = approval_timeout_seconds
        configured_codex_home = codex_home or os.environ.get("WORKBENCH_CODEX_HOME")
        self.codex_home = (
            Path(configured_codex_home).expanduser() if configured_codex_home else None
        )
        self.process: asyncio.subprocess.Process | None = None
        self._process_generation: str | None = None
        self.current_session_id: str | None = None
        self.generation: str | None = None
        self._request_id = 0
        self._initialized = False
        self._is_busy = False
        self._lock = asyncio.Lock()
        self._early_notifications: list[dict[str, Any]] = []
        self.current_turn_id: str | None = None
        self._file_change_patches: dict[tuple[str, str, str], str] = {}

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
                session_info = (
                    {"type": "session_info", "session_id": self.current_session_id}
                    if self.current_session_id
                    else None
                )
                session_info_emitted = False
                async for event in self._run_turn(prompt):
                    # Keep terminal events last while associating the session with
                    # the first real turn event when one is available.
                    if (
                        session_info is not None
                        and not session_info_emitted
                        and event.get("type") in {"exit", "error"}
                    ):
                        yield session_info
                        session_info_emitted = True
                    yield event
                    if (
                        session_info is not None
                        and not session_info_emitted
                        and event.get("type") not in {"exit", "error"}
                    ):
                        yield session_info
                        session_info_emitted = True
                if session_info is not None and not session_info_emitted:
                    yield session_info
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
        environment = build_cli_environment(RuntimeBackend.CODEX)
        if self.codex_home is not None:
            try:
                prepared_home = prepare_codex_home(self.codex_home)
            except CodexHomeError as exc:
                raise CodexAppServerError(
                    "Workbench Codex state home is unavailable"
                ) from exc
            environment["CODEX_HOME"] = str(prepared_home)
        process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.workspace),
            env=environment,
            start_new_session=os.name == "posix",
            limit=_STREAM_LIMIT_BYTES,
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
        if session_id and not fork_session and thread_id != session_id:
            raise CodexAppServerError("app-server resumed a different thread")
        self.current_session_id = thread_id
        self.current_turn_id = None
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
                result = message.get("result")
                turn = result.get("turn") if isinstance(result, Mapping) else None
                turn_id = (
                    _text(result.get("turnId")) if isinstance(result, Mapping) else None
                )
                if turn_id is None and isinstance(turn, Mapping):
                    turn_id = _text(turn.get("id"))
                if not isinstance(turn_id, str) or not turn_id:
                    raise CodexAppServerError(
                        "app-server turn/start response has no turn id"
                    )
                self.current_turn_id = turn_id
                turn_response_seen = True
                continue
            if "id" in message and "method" in message:
                await self._handle_server_request(message)
                continue
            if not turn_response_seen and "result" in message:
                continue
            event = await self._notification_event(message)
            if event is not None:
                if event.get("type") == "exit":
                    event_turn_id = event.get("turn_id")
                    if not isinstance(event_turn_id, str):
                        raise CodexAppServerError(
                            "app-server completion has no turn id"
                        )
                    event_thread_id = event.get("thread_id")
                    if event_thread_id is not None and event_thread_id != thread_id:
                        raise CodexAppServerError(
                            "app-server completion belongs to a different thread"
                        )
                    if self.current_turn_id != event_turn_id:
                        raise CodexAppServerError(
                            "app-server completion belongs to a different turn"
                        )
                    if self.approval_manager is not None:
                        await self.approval_manager.clear_turn(
                            provider="codex_cli",
                            session_id=thread_id,
                            thread_id=thread_id,
                            turn_id=event_turn_id,
                        )
                    self._clear_turn_patch_cache(thread_id, event_turn_id)
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
        if not self._active_identity_matches(params, require_known=True):
            return "decline"
        intent = self._intent_from_approval(method, params)
        if intent is None or self.approval_manager is None:
            return "decline"
        record = await self.approval_manager.request(
            intent, approval_timeout_seconds=self.approval_timeout_seconds
        )
        bound_intent = record.intent
        observed = record
        if record.status is ApprovalState.PENDING:
            if self.on_approval_pending is not None:
                await self.on_approval_pending(record)
            observed = await self.approval_manager.wait_for_terminal(bound_intent)
        if observed.status is ApprovalState.APPROVED:
            try:
                await self.approval_manager.consume(bound_intent)
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
        turn_id = _text(params.get("turnId"))
        item_id = _text(params.get("itemId"))
        cwd = self._resolve_cwd(params.get("cwd"))
        if (
            not thread_id
            or not item_id
            or cwd is None
            or self.approval_manager is None
            or not self._active_identity_matches(
                {"threadId": thread_id, "turnId": turn_id}, require_known=True
            )
        ):
            return {}, "decline"
        try:
            canonical = _canonical_json(permissions)
        except TypeError, ValueError:
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
            thread_id=thread_id,
            item_id=item_id,
            call_id=item_id,
            argv=("codex-permission-request", canonical),
            cwd=cwd,
            requested_permission=_permission_label("sandbox_escalation", canonical),
            provider="codex_cli",
            turn_id=turn_id,
            workspace_target=self.workspace,
            permission_scope=_permission_scope_for_permissions(permissions, canonical),
            approval_scope=self.approval_scope,
        )
        record = await self.approval_manager.request(
            intent, approval_timeout_seconds=self.approval_timeout_seconds
        )
        bound_intent = record.intent
        observed = record
        if record.status is ApprovalState.PENDING:
            if self.on_approval_pending is not None:
                await self.on_approval_pending(record)
            observed = await self.approval_manager.wait_for_terminal(bound_intent)
        if observed.status is ApprovalState.APPROVED:
            try:
                await self.approval_manager.consume(bound_intent)
            except Exception:
                return {}, "decline"
            return requested, "accept"
        return {}, "decline"

    def _intent_from_approval(
        self, method: str, params: Mapping[str, Any]
    ) -> CommandIntent | None:
        thread_id = _text(params.get("threadId")) or _text(params.get("conversationId"))
        turn_id = _text(params.get("turnId"))
        item_id = _text(params.get("itemId"))
        call_id = _text(params.get("callId")) or item_id
        approval_id = _text(params.get("approvalId"))
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
                thread_id=thread_id,
                call_id=call_id,
                item_id=item_id,
                approval_id=approval_id,
                argv=argv,
                cwd=cwd,
                requested_permission=requested_permission,
                provider="codex_cli",
                turn_id=turn_id,
                workspace_target=self.workspace,
                permission_scope=self._permission_scope(params),
                approval_scope=self.approval_scope,
            )
        if method in {"item/fileChange/requestApproval", "applyPatchApproval"}:
            patch_identity = self._file_change_identity(
                method, params, thread_id, item_id
            )
            if patch_identity is None:
                return None
            return CommandIntent.create(
                session_id=thread_id,
                thread_id=thread_id,
                call_id=call_id,
                item_id=item_id,
                approval_id=approval_id,
                argv=("codex-file-change", item_id or call_id, patch_identity),
                cwd=cwd,
                requested_permission="file_write",
                provider="codex_cli",
                turn_id=turn_id,
                workspace_target=self.workspace,
                permission_scope=f"filesystem:write:{self.workspace}",
                approval_scope=self.approval_scope,
                patch_identity=patch_identity,
            )
        if not isinstance(command, str):
            return None
        requested_permission = self._command_permission(params)
        if requested_permission is None:
            return None
        try:
            return CommandIntent.create(
                session_id=thread_id,
                thread_id=thread_id,
                call_id=call_id,
                item_id=item_id,
                approval_id=approval_id,
                command=command,
                cwd=cwd,
                requested_permission=requested_permission,
                provider="codex_cli",
                turn_id=turn_id,
                workspace_target=self.workspace,
                permission_scope=self._permission_scope(params),
                approval_scope=self.approval_scope,
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
        except TypeError, ValueError:
            return None
        return _permission_label("process_spawn", canonical)

    def _permission_scope(self, params: Mapping[str, Any]) -> str | None:
        context = params.get("networkApprovalContext")
        if isinstance(context, Mapping):
            host = _text(context.get("host")) or _text(context.get("target"))
            if host:
                return f"network:{host}"
        additional = params.get("additionalPermissions")
        if additional is None:
            return None
        try:
            return f"codex:{_canonical_json(additional)}"
        except TypeError, ValueError:
            return None

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
            except TypeError, ValueError:
                return None
        if item_id is None:
            return None
        turn_id = _text(params.get("turnId"))
        if turn_id is None:
            return None
        return self._file_change_patches.get((thread_id, turn_id, item_id))

    def _active_identity_matches(
        self, params: Mapping[str, Any], *, require_known: bool = False
    ) -> bool:
        """Require provider approval callbacks to belong to the active turn."""
        thread_id = _text(params.get("threadId")) or _text(params.get("conversationId"))
        turn_id = _text(params.get("turnId"))
        if require_known and (
            self.current_session_id is None or self.current_turn_id is None
        ):
            return False
        if self.current_session_id is not None and thread_id != self.current_session_id:
            return False
        if self.current_turn_id is not None and turn_id != self.current_turn_id:
            return False
        return thread_id is not None and turn_id is not None

    def _clear_turn_patch_cache(self, thread_id: str, turn_id: str) -> None:
        for key in tuple(self._file_change_patches):
            if key[:2] == (thread_id, turn_id):
                self._file_change_patches.pop(key, None)

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
            identity = _codex_identity(
                params,
                fallback_thread_id=self.current_session_id,
            )
            return _assistant_event(delta, identity=identity) if delta else None
        if method == "item/commandExecution/outputDelta":
            delta = _text(params.get("delta"))
            identity = _codex_identity(
                params,
                fallback_thread_id=self.current_session_id,
            )
            item_id = identity.get("item_id") or "codex-command"
            if not delta:
                return None
            return _tool_result_event(item_id, delta, identity=identity)
        if method == "item/fileChange/patchUpdated":
            thread_id = _text(params.get("threadId")) or self.current_session_id
            turn_id = _text(params.get("turnId"))
            item_id = _text(params.get("itemId"))
            identity = _codex_identity(
                params,
                fallback_thread_id=self.current_session_id,
            )
            changes = params.get("changes")
            if (
                thread_id
                and turn_id
                and item_id
                and isinstance(changes, list)
                and changes
                and _patch_paths_stay_in_workspace(changes, self.workspace)
                and self._active_identity_matches(params)
            ):
                with contextlib.suppress(TypeError, ValueError):
                    self._file_change_patches[(thread_id, turn_id, item_id)] = (
                        _canonical_json(changes)
                    )
            event = {"type": "file_change", "item": dict(params)}
            event.update(identity)
            return event
        if method == "item/started":
            item = params.get("item")
            item_mapping = item if isinstance(item, Mapping) else None
            identity = _codex_identity(
                params,
                item=item_mapping,
                fallback_thread_id=self.current_session_id,
            )
            return _item_event(item, completed=False, identity=identity)
        if method == "item/completed":
            item = params.get("item")
            item_mapping = item if isinstance(item, Mapping) else None
            identity = _codex_identity(
                params,
                item=item_mapping,
                fallback_thread_id=self.current_session_id,
            )
            return _item_event(item, completed=True, identity=identity)
        if method == "turn/completed":
            turn = params.get("turn")
            status = turn.get("status") if isinstance(turn, Mapping) else "completed"
            code = 0 if status in {None, "completed"} else 1
            identity = _codex_identity(
                params,
                turn=turn if isinstance(turn, Mapping) else None,
                fallback_thread_id=self.current_session_id,
            )
            event = {"type": "exit", "code": code, "stderr": None}
            event.update(identity)
            return event
        if method == "error":
            identity = _codex_identity(
                params,
                fallback_thread_id=self.current_session_id,
            )
            event = {"type": "error", "error": _error_detail(params)}
            event.update(identity)
            return event
        return None

    async def interrupt_turn(self) -> bool:
        """Native turn-level interrupt (codex 'turn/interrupt').

        Cancels the CURRENT turn only; the app-server process and the thread
        survive, so a new turn can run afterwards.  Never SIGTERMs the process.
        Returns False when no turn is running or the request cannot be sent.
        Must NOT acquire ``self._lock``: ``start_task`` holds it while the
        turn loop is consuming messages; this method only writes to stdin.
        """
        thread_id = self.current_session_id
        turn_id = self.current_turn_id
        if not thread_id or not turn_id:
            return False
        if self.process is None or self.process.returncode is not None:
            return False
        try:
            await self._send_request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
            )
            return True
        except CodexAppServerError:
            return False

    async def stop(self) -> bool:
        process = self.process
        if process is None:
            self.current_session_id = None
            self.current_turn_id = None
            self._initialized = False
            self._early_notifications.clear()
            self._file_change_patches.clear()
            return False
        try:
            await self._terminate_process(process)
        finally:
            if process.pid and self._process_generation:
                unregister_process(process.pid, generation=self._process_generation)
            self.process = None
            self._process_generation = None
            self.current_session_id = None
            self.current_turn_id = None
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


def _permission_scope_for_permissions(
    permissions: Mapping[str, Any], canonical: str
) -> str:
    """Produce a conservative same-turn scope for a native permission profile."""
    filesystem = permissions.get("fileSystem")
    network = permissions.get("network")
    if filesystem is not None and network is None:
        entries = filesystem.get("entries") if isinstance(filesystem, Mapping) else None
        if isinstance(entries, list) and len(entries) == 1:
            entry = entries[0]
            if isinstance(entry, Mapping):
                access = _text(entry.get("access"))
                path_info = entry.get("path")
                if isinstance(path_info, Mapping):
                    path = _text(path_info.get("path"))
                    if access and path:
                        return f"filesystem:{access}:{path}"
    if network is not None and filesystem is None:
        return "network:*"
    return f"codex:{canonical}"


def _patch_paths_stay_in_workspace(value: Any, workspace: Path) -> bool:
    """Reject patch identities that mention paths outside the native sandbox."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if (
                key in {"path", "move_path"}
                and nested is not None
                and (
                    not isinstance(nested, str)
                    or not _path_stays_in_workspace(nested, workspace)
                )
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
    if value.startswith(("\\\\", "//")) or (
        len(value) >= 3 and value[1] == ":" and value[2] in "/\\"
    ):
        return False
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = candidate.resolve(strict=False)
        candidate.relative_to(workspace)
    except OSError, ValueError:
        return False
    return True


def _scrub_error_text(value: str) -> str:
    """Redact ``token=abc`` / ``Authorization: Bearer abc`` / ``"api_key": "abc"``
    style pairs embedded in free-form diagnostic text.

    Deliberately keyed on a *key=value* shape rather than the bare word, so
    ordinary wording such as "token bucket exhausted" survives intact.
    """
    redacted = _ERROR_SECRET_PAIR_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[redacted]", value
    )
    return _ERROR_BEARER_PATTERN.sub("Bearer [redacted]", redacted)


def _error_key_is_sensitive(key: Any) -> bool:
    """True when a key name looks like it carries a credential."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(token in lowered for token in _ERROR_SENSITIVE_KEY_TOKENS)


def _error_json_safe(value: Any) -> Any:
    """Reduce an arbitrary diagnostic value to JSON-safe data, scrubbing secrets.

    Always returns a value that ``json.dumps`` accepts. Unknown object types
    degrade to their ``repr`` rather than raising: a missing diagnostic field
    must never turn a provider outage into a secondary crash.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if _error_key_is_sensitive(key):
                continue
            safe[str(key)] = _error_json_safe(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_error_json_safe(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


def _error_detail(params: Any) -> dict[str, Any]:
    """Build the ``error`` payload for an app-server error notification.

    The generic message is preserved verbatim for backwards compatibility;
    diagnostic fields are added alongside it from a strict whitelist. Only
    ``params.error`` is inspected — never the whole envelope — and every value
    passes through :func:`_error_json_safe`.
    """
    detail: dict[str, Any] = {"message": _ERROR_GENERIC_MESSAGE}
    error = params.get("error") if isinstance(params, Mapping) else None
    if not isinstance(error, Mapping):
        return detail
    for source_key, target_key in _ERROR_DIAGNOSTIC_KEYS.items():
        if source_key not in error:
            continue
        safe = _error_json_safe(error[source_key])
        if safe is None:
            continue
        if isinstance(safe, str):
            # Free-form text: credentials travel as values here, not keys.
            safe = _scrub_error_text(safe)
        detail[target_key] = safe
    # Retry hints live on the notification itself, not on the error object.
    if isinstance(params, Mapping) and "willRetry" in params:
        detail["retryable"] = bool(params["willRetry"])
    return detail


_CODEX_IDENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "thread_id": ("threadId", "thread_id"),
    "turn_id": ("turnId", "turn_id"),
    "item_id": ("itemId", "item_id"),
    "approval_id": ("approvalId", "approval_id"),
    "call_id": ("callId", "call_id"),
}


def _codex_identity(
    params: Mapping[str, Any],
    *,
    item: Mapping[str, Any] | None = None,
    turn: Mapping[str, Any] | None = None,
    fallback_thread_id: str | None = None,
) -> dict[str, str]:
    """Project only bounded lifecycle IDs from known app-server mappings."""
    sources: tuple[Mapping[str, Any], ...] = tuple(
        source for source in (params, item, turn) if isinstance(source, Mapping)
    )
    identity: dict[str, str] = {}
    for field, aliases in _CODEX_IDENTITY_ALIASES.items():
        for source in sources:
            value = next(
                (
                    normalized
                    for alias in aliases
                    if (normalized := _identity_text(source.get(alias))) is not None
                ),
                None,
            )
            if value is not None:
                identity[field] = value
                break
    if "item_id" not in identity and isinstance(item, Mapping):
        item_id = _identity_text(item.get("id"))
        if item_id is not None:
            identity["item_id"] = item_id
    if "turn_id" not in identity and isinstance(turn, Mapping):
        turn_id = _identity_text(turn.get("id"))
        if turn_id is not None:
            identity["turn_id"] = turn_id
    if "thread_id" not in identity:
        fallback = _identity_text(fallback_thread_id)
        if fallback is not None:
            identity["thread_id"] = fallback
    return identity


def _identity_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 512:
        return None
    if any(ord(char) < 0x20 for char in normalized):
        return None
    return normalized


def _assistant_event(
    text: str, *, identity: Mapping[str, str] | None = None
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if identity:
        event.update(identity)
    return event


def _tool_result_event(
    tool_id: str,
    content: str,
    *,
    identity: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
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
    if identity:
        event.update(identity)
    return event


def _item_event(
    item: Any,
    *,
    completed: bool,
    identity: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    item_type = item.get("type")
    item_id = _text(item.get("id")) or "codex-item"
    if item_type == "agentMessage":
        text = _text(item.get("text"))
        return _assistant_event(text, identity=identity) if text else None
    if item_type == "commandExecution":
        command = _text(item.get("command")) or "command"
        if completed:
            return _tool_result_event(
                item_id,
                _text(item.get("aggregatedOutput")) or "",
                identity=identity,
            )
        event: dict[str, Any] = {
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
        if identity:
            event.update(identity)
        return event
    if item_type == "fileChange":
        event = {"type": "file_change", "item": dict(item)}
        if identity:
            event.update(identity)
        return event
    return None


__all__ = ["CodexAppServerError", "CodexAppServerSession"]

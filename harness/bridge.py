"""Small host-side bridge for the official DeepSeek Harness JSON-RPC runtime.

This module intentionally does not implement Cordis or emulate its plugin
loader.  It starts the configured official runtime, sends the documented
``initialize``/``session/prompt`` requests, and projects runtime events into
the existing Anthropic SSE vocabulary for callers that already speak the
gateway API.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from loguru import logger

from providers.common import RuntimeIdentity, SSEBuilder

from .config import DEFAULT_RUNTIME_COMMAND, HarnessConfig, HarnessConfigError
from .events import HarnessEventEnvelope, project_notification, project_sse
from .process import (
    HarnessProcess,
    HarnessProcessError,
    HarnessProtocolError,
    HarnessRequestTimeoutError,
    HarnessRpcError,
    HarnessRuntimeClosedError,
)
from .protocol import JsonRpcNotification


class HarnessBridgeError(HarnessProcessError):
    """A runtime response violated the bridge-level contract."""


class HarnessUnsupportedContentError(HarnessBridgeError):
    """The Anthropic request contains a block DSH cannot accept losslessly."""


HARNESS_FAILURE_TIMEOUT = "timeout"
HARNESS_FAILURE_RUNTIME_START = "runtime_start_failed"
HARNESS_FAILURE_RUNTIME = "runtime_failed"
HARNESS_FAILURE_INITIALIZE = "initialize_failed"

_FAILURE_MESSAGES = {
    HARNESS_FAILURE_TIMEOUT: "DeepSeek Harness runtime request timed out.",
    HARNESS_FAILURE_RUNTIME_START: "DeepSeek Harness runtime failed to start.",
    HARNESS_FAILURE_RUNTIME: "DeepSeek Harness runtime failed during execution.",
    HARNESS_FAILURE_INITIALIZE: "DeepSeek Harness runtime initialization failed.",
}


# The published DeepSeek adapter deliberately owns the more specific route
# name.  Keep the shorter name as an API-facing alias because existing clients
# already use ``dsh/deepseek/<model>``.
DSH_OFFICIAL_PROVIDER = "deepseek-official"
DSH_PROVIDER_ALIASES = {
    "deepseek": DSH_OFFICIAL_PROVIDER,
    DSH_OFFICIAL_PROVIDER: DSH_OFFICIAL_PROVIDER,
}


def resolve_runtime_provider(provider: str) -> str:
    """Resolve an API provider name to the Cordis runtime route name."""
    if not isinstance(provider, str) or not provider.strip():
        raise HarnessBridgeError("DeepSeek Harness provider must be a non-empty string")
    normalized = provider.strip()
    return DSH_PROVIDER_ALIASES.get(normalized, normalized)


@dataclass(slots=True)
class HarnessTurn:
    """Collected root-session activity for one prompt interval."""

    session_id: str
    message_id: str
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex}")
    turn_id: str | None = None
    item_id: str | None = None
    one_shot_id: str | None = None
    agent_id: str | None = None
    tool_id: str | None = None
    call_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[HarnessEventEnvelope] = field(default_factory=list)
    final_text: str = ""
    finish_reason: str | None = None


class DeepSeekHarnessBridge:
    """Lazy, bounded bridge to one official DSH runtime process."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        provider: str = DSH_OFFICIAL_PROVIDER,
        model: str = "deepseek-v4-flash",
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.runtime_provider = resolve_runtime_provider(provider)
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self._process: HarnessProcess | None = None
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._turn_semaphore = asyncio.Semaphore(config.max_concurrency)
        self._session_parents: dict[str, str] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_guard = asyncio.Lock()
        # Client sessions are mapped to runtime ids per workspace.  Runtime
        # ids are intentionally opaque and are discarded whenever the
        # sidecar restarts, so a stale id can never be reused against a new
        # process.
        self._session_aliases: dict[tuple[str, str], str] = {}
        self._runtime_version: str | None = None
        self._last_failure_category: str | None = None
        self._last_failure_detail: str | None = None

    @property
    def process(self) -> HarnessProcess | None:
        """Expose the owned process for lifecycle tests and diagnostics."""
        return self._process

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running

    @property
    def is_ready(self) -> bool:
        """Return whether the runtime completed the official initialize call."""
        return self._initialized and self.is_running

    @property
    def runtime_version(self) -> str | None:
        """Return the version reported by the initialized runtime, if any."""
        return self._runtime_version

    @property
    def last_failure_category(self) -> str | None:
        """Return the most recent bounded runtime failure classification."""
        return self._last_failure_category

    @property
    def last_failure_detail(self) -> str | None:
        """Return a safe, non-payload description of the most recent failure."""
        return self._last_failure_detail

    async def start(self, *, cwd: str | Path | None = None) -> None:
        """Start and initialize the runtime on first use."""
        async with self._initialize_lock:
            if self._initialized and self.is_running:
                return
            if self._process is not None:
                await self._process.close()
            self.config.validate_cordis_config()
            process_config = self._runtime_config(cwd)
            workspace = self._workspace(cwd)
            process = HarnessProcess(
                process_config,
                environment=self._runtime_environment(workspace),
            )
            self._clear_failure()
            try:
                await process.start()
            except Exception as exc:
                self._mark_failure(_failure_category(exc, phase="start"))
                raise
            self._process = process
            try:
                result = await process.request(
                    "initialize",
                    {
                        "cwd": str(workspace),
                        "provider": self.runtime_provider,
                        "model": self.model,
                        "maxTokens": self.config.max_tokens,
                    },
                )
                if result is not None and not isinstance(result, Mapping):
                    raise HarnessBridgeError(
                        "DeepSeek Harness initialize result must be an object"
                    )
                server_info = (
                    result.get("serverInfo") if isinstance(result, Mapping) else None
                )
                runtime_version = (
                    server_info.get("version")
                    if isinstance(server_info, Mapping)
                    else None
                )
                expected_version = self.config.runtime_version
                if expected_version is not None:
                    if not isinstance(runtime_version, str) or not runtime_version:
                        raise HarnessBridgeError(
                            "DeepSeek Harness runtime did not report serverInfo.version"
                        )
                    if runtime_version != expected_version:
                        raise HarnessBridgeError(
                            "DeepSeek Harness runtime version mismatch"
                        )
                self._runtime_version = (
                    runtime_version if isinstance(runtime_version, str) else None
                )
                self._initialized = True
                self._clear_failure()
            except Exception as exc:
                self._mark_failure(_failure_category(exc, phase="initialize"))
                self._forget_process(process)
                await process.close()
                raise

    async def close(self) -> None:
        """Close the runtime and release all pending subscribers."""
        async with self._initialize_lock:
            process = self._process
            self._process = None
            self._initialized = False
            self._runtime_version = None
            self._session_aliases.clear()
            self._session_parents.clear()
            self._session_locks.clear()
            if process is not None:
                await process.close()

    async def run(
        self,
        content_blocks: Sequence[Mapping[str, Any]],
        *,
        session_id: str | None = None,
        cwd: str | Path | None = None,
    ) -> HarnessTurn:
        """Queue a prompt and collect notifications through root-session idle."""
        _validate_content_blocks(content_blocks)
        root_session_id = self._runtime_session_id(session_id, cwd)
        session_lock = await self._session_lock(root_session_id)
        async with self._turn_semaphore, session_lock:
            await self.start(cwd=cwd)
            process = self._process
            if process is None:
                raise HarnessRuntimeClosedError(
                    "DeepSeek Harness runtime is not available"
                )
            queue = process.subscribe()
            try:
                response = await process.request(
                    "session/prompt",
                    {
                        "sessionId": root_session_id,
                        "contentBlocks": [dict(block) for block in content_blocks],
                    },
                )
                if not isinstance(response, Mapping):
                    raise HarnessBridgeError(
                        "DeepSeek Harness session/prompt result must be an object"
                    )
                message_id = response.get("messageId")
                if not isinstance(message_id, str) or not message_id:
                    raise HarnessBridgeError(
                        "DeepSeek Harness session/prompt result has no messageId"
                    )
                turn = HarnessTurn(root_session_id, message_id)
                await self._collect_turn(process, queue, turn)
                return turn
            except TimeoutError as exc:
                self._mark_failure(HARNESS_FAILURE_TIMEOUT)
                # A timed-out turn cannot be safely resumed because the runtime
                # may still be executing plugins.  The process is exclusive,
                # so close it before the next request.
                await process.close()
                self._forget_process(process)
                raise exc
            except HarnessProcessError:
                self._mark_failure(HARNESS_FAILURE_RUNTIME)
                # A timed-out turn cannot be safely resumed because the runtime
                # may still be executing plugins.  The process is exclusive,
                # so close it before the next request.
                await process.close()
                self._forget_process(process)
                raise
            except asyncio.CancelledError:
                await asyncio.shield(process.close())
                self._forget_process(process)
                raise
            finally:
                process.unsubscribe(queue)

    async def stream_messages(
        self,
        content_blocks: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        session_id: str | None = None,
        cwd: str | Path | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Run a prompt and yield Anthropic-compatible SSE frames.

        DSH plugin events are also exposed under namespaced ``harness_*`` SSE
        names.  Known root ``assistant/message`` events are projected to the
        existing Anthropic message lifecycle; unknown plugin events remain
        lossless custom frames for advanced clients.
        """
        message_id = request_id or f"msg_{uuid.uuid4().hex[:16]}"
        builder = SSEBuilder(message_id, model or self.model)
        yield builder.message_start()
        started_content = False
        emitted_event_keys: set[str] = set()
        root_session_id = self._runtime_session_id(session_id, cwd)
        turn = HarnessTurn(root_session_id, "")
        try:
            async for notification in self._stream_turn(
                content_blocks,
                turn=turn,
                cwd=cwd,
            ):
                event = notification.get("event")
                event_type = notification.get("event_type")
                if event_type == "assistant/message" and isinstance(event, dict):
                    key = _event_key(event)
                    if key in emitted_event_keys:
                        continue
                    emitted_event_keys.add(key)
                    for frame in _assistant_event_frames(builder, event):
                        started_content = True
                        yield frame
                elif notification.get("type") != "session_status":
                    # Do not log or interpolate raw payloads; the frame itself
                    # is sent only to the already-authorized API caller.
                    for frame in project_sse(notification):
                        yield frame

            if started_content:
                for frame in builder.close_all_blocks():
                    yield frame
            yield builder.message_delta(
                _map_finish_reason(turn.finish_reason),
                builder.estimate_output_tokens(),
            )
            yield builder.message_stop()
        except Exception as exc:  # streaming errors are encoded as SSE
            logger.warning("DSH turn failed: {}", type(exc).__name__)
            category = self._last_failure_category or _failure_category(
                exc, phase="runtime"
            )
            self._mark_failure(category)
            if started_content:
                for frame in builder.close_all_blocks():
                    yield frame
            error_payload = {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "code": category,
                    "message": _failure_message(category),
                },
            }
            yield _serialize_sse("error", error_payload)

    async def _stream_turn(
        self,
        content_blocks: Sequence[Mapping[str, Any]],
        *,
        turn: HarnessTurn,
        cwd: str | Path | None = None,
    ) -> AsyncIterator[HarnessEventEnvelope]:
        """Yield root-tree notifications as they arrive until ``idle``."""
        _validate_content_blocks(content_blocks)
        session_lock = await self._session_lock(turn.session_id)
        async with self._turn_semaphore, session_lock:
            await self.start(cwd=cwd)
            process = self._process
            if process is None:
                raise HarnessRuntimeClosedError(
                    "DeepSeek Harness runtime is not available"
                )
            queue = process.subscribe()
            ack_task = asyncio.create_task(
                process.request(
                    "session/prompt",
                    {
                        "sessionId": turn.session_id,
                        "contentBlocks": [dict(block) for block in content_blocks],
                    },
                ),
                name="dsh-prompt-ack",
            )
            buffered: list[JsonRpcNotification] = []
            message_id: str | None = None
            received = False
            deadline = asyncio.get_running_loop().time() + self.config.request_timeout

            async def process_notification(
                notification: JsonRpcNotification,
            ) -> tuple[HarnessEventEnvelope | None, bool]:
                nonlocal received
                if not self._belongs_to_tree(notification, turn.session_id):
                    return None, False
                if not received:
                    if (
                        message_id is not None
                        and _is_inbox_receipt(notification, turn.session_id, message_id)
                    ) or _is_idle(notification, turn.session_id):
                        received = True
                    else:
                        self._record_lineage(notification)
                        return None, False
                envelope = project_notification(
                    notification.method,
                    notification.params,
                    session_id=turn.session_id,
                    provider=self.runtime_provider,
                    identity=self._turn_identity(turn),
                )
                turn.notifications.append(envelope)
                _update_turn_identity(turn, envelope)
                if notification.method == "session.event":
                    event = _event_from_notification(notification)
                    if event is not None:
                        turn.events.append(event)
                        _update_turn_result(turn, event)
                self._record_lineage(notification)
                return envelope, _is_idle(notification, turn.session_id)

            try:
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError(
                            "DeepSeek Harness turn timed out waiting for idle"
                        )

                    if message_id is None:
                        queue_task = asyncio.create_task(queue.get())
                        done, pending = await asyncio.wait(
                            {ack_task, queue_task},
                            timeout=remaining,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            queue_task.cancel()
                            await asyncio.gather(queue_task, return_exceptions=True)
                            raise TimeoutError(
                                "DeepSeek Harness turn timed out waiting for idle"
                            )
                        if ack_task in done:
                            response = ack_task.result()
                            if not isinstance(response, Mapping):
                                raise HarnessBridgeError(
                                    "DeepSeek Harness session/prompt result must be an object"
                                )
                            ack_message_id = response.get("messageId")
                            if (
                                not isinstance(ack_message_id, str)
                                or not ack_message_id
                            ):
                                raise HarnessBridgeError(
                                    "DeepSeek Harness session/prompt result has no messageId"
                                )
                            message_id = ack_message_id
                            turn.message_id = message_id
                        if queue_task in done:
                            item = queue_task.result()
                            if isinstance(item, BaseException):
                                raise item
                            if isinstance(item, JsonRpcNotification):
                                buffered.append(item)
                        else:
                            for pending_task in pending:
                                pending_task.cancel()
                                await asyncio.gather(
                                    pending_task, return_exceptions=True
                                )
                        if message_id is None:
                            continue
                        for early in buffered:
                            envelope, idle = await process_notification(early)
                            if envelope is not None:
                                yield envelope
                            if idle:
                                if turn.finish_reason is None:
                                    turn.finish_reason = "completed"
                                return
                        buffered.clear()
                        continue

                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                    if isinstance(item, BaseException):
                        raise item
                    if not isinstance(item, JsonRpcNotification):
                        continue
                    envelope, idle = await process_notification(item)
                    if envelope is not None:
                        yield envelope
                    if idle:
                        if turn.finish_reason is None:
                            turn.finish_reason = "completed"
                        return
            except TimeoutError:
                self._mark_failure(HARNESS_FAILURE_TIMEOUT)
                await process.close()
                self._forget_process(process)
                raise
            except HarnessProcessError as exc:
                self._mark_failure(HARNESS_FAILURE_RUNTIME)
                await process.close()
                self._forget_process(process)
                raise exc
            except asyncio.CancelledError:
                await asyncio.shield(process.close())
                self._forget_process(process)
                raise
            finally:
                if not ack_task.done():
                    ack_task.cancel()
                    await asyncio.gather(ack_task, return_exceptions=True)
                process.unsubscribe(queue)

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _runtime_session_id(
        self, client_session_id: str | None, cwd: str | Path | None
    ) -> str:
        """Return the runtime id for a client session/workspace pair."""
        if not isinstance(client_session_id, str) or not client_session_id.strip():
            return _safe_session_id(None)
        client_id = _safe_session_id(client_session_id)
        workspace = str(self._workspace(cwd))
        key = (client_id, workspace)
        runtime_id = self._session_aliases.get(key)
        if runtime_id is None:
            runtime_id = _safe_session_id(None)
            self._session_aliases[key] = runtime_id
        return runtime_id

    def _forget_process(self, process: HarnessProcess) -> None:
        """Forget process-scoped state after a runtime failure or restart."""
        if self._process is process:
            self._process = None
        self._initialized = False
        self._runtime_version = None
        self._session_aliases.clear()
        self._session_parents.clear()

    async def _collect_turn(
        self,
        process: HarnessProcess,
        queue: asyncio.Queue[Any],
        turn: HarnessTurn,
    ) -> None:
        received = False
        timeout = self.config.request_timeout_seconds

        async def consume() -> None:
            nonlocal received
            while True:
                item = await queue.get()
                if isinstance(item, BaseException):
                    raise item
                if not isinstance(item, JsonRpcNotification):
                    continue
                if not self._belongs_to_tree(item, turn.session_id):
                    continue
                envelope = project_notification(
                    item.method,
                    item.params,
                    session_id=turn.session_id,
                    provider=self.runtime_provider,
                    identity=self._turn_identity(turn),
                )
                if not received:
                    if _is_inbox_receipt(item, turn.session_id, turn.message_id):
                        received = True
                    elif _is_idle(item, turn.session_id):
                        # Some plugin compositions do not expose inbox events;
                        # an idle notification after the prompt ack is still a
                        # complete, harmless turn.
                        received = True
                    else:
                        self._record_lineage(item)
                        continue
                turn.notifications.append(envelope)
                _update_turn_identity(turn, envelope)
                if item.method == "session.event":
                    event = _event_from_notification(item)
                    if (
                        event is not None
                        and _param_value(item.params, "sessionId", "session_id")
                        == turn.session_id
                    ):
                        turn.events.append(event)
                        _update_turn_result(turn, event)
                if _is_idle(item, turn.session_id):
                    if turn.finish_reason is None:
                        turn.finish_reason = "completed"
                    return
                self._record_lineage(item)

        try:
            await asyncio.wait_for(consume(), timeout=timeout)
        except TimeoutError as exc:
            raise HarnessRequestTimeoutError(
                "DeepSeek Harness turn timed out waiting for idle"
            ) from exc

    def _turn_identity(self, turn: HarnessTurn) -> RuntimeIdentity:
        """Return only stable host-owned identity for event projection."""
        return RuntimeIdentity(
            provider=self.runtime_provider,
            session_id=turn.session_id,
            run_id=turn.run_id,
            turn_id=turn.turn_id,
            item_id=turn.item_id,
            one_shot_id=turn.one_shot_id,
            agent_id=turn.agent_id,
            tool_id=turn.tool_id,
            call_id=turn.call_id,
            message_id=turn.message_id or None,
        )

    def _clear_failure(self) -> None:
        self._last_failure_category = None
        self._last_failure_detail = None

    def _mark_failure(self, category: str) -> None:
        normalized = (
            category if category in _FAILURE_MESSAGES else HARNESS_FAILURE_RUNTIME
        )
        self._last_failure_category = normalized
        self._last_failure_detail = _failure_message(normalized)

    def _record_lineage(self, notification: JsonRpcNotification) -> None:
        params = _params_mapping(notification)
        if notification.method != "subagent.started" or params is None:
            return
        parent = _param_value(params, "parentSessionId", "parent_session_id")
        child = _param_value(params, "childSessionId", "child_session_id")
        if (
            isinstance(parent, str)
            and isinstance(child, str)
            and parent
            and child
            and parent != child
        ):
            self._session_parents[child] = parent

    def _belongs_to_tree(
        self, notification: JsonRpcNotification, root_session_id: str
    ) -> bool:
        self._record_lineage(notification)
        params = _params_mapping(notification)
        if params is None:
            return False
        if notification.method in {"subagent.started", "subagent.finished"}:
            ids = (
                _param_value(params, "parentSessionId", "parent_session_id"),
                _param_value(params, "childSessionId", "child_session_id"),
            )
            return any(
                isinstance(value, str) and self._is_descendant(value, root_session_id)
                for value in ids
            )
        session = _param_value(params, "sessionId", "session_id")
        return isinstance(session, str) and self._is_descendant(
            session, root_session_id
        )

    def _is_descendant(self, session_id: str, root: str) -> bool:
        current = session_id
        seen: set[str] = set()
        while current not in seen:
            if current == root:
                return True
            seen.add(current)
            parent = self._session_parents.get(current)
            if parent is None:
                return False
            current = parent
        return False

    def _runtime_config(self, cwd: str | Path | None) -> HarnessConfig:
        return self.config

    def _runtime_environment(self, cwd: str | Path | None) -> dict[str, str]:
        workspace = self._workspace(cwd)
        environment: dict[str, str] = {
            "DSH_CWD": str(workspace),
            "DSH_MODEL": self.model,
            "DSH_SESSION_ROOT": str(workspace / ".dsh-sessions"),
        }
        if self.api_key is not None:
            environment["DEEPSEEK_API_KEY"] = self.api_key
        if self.base_url is not None:
            environment["DEEPSEEK_BASE_URL"] = self.base_url
        return environment

    def _workspace(self, cwd: str | Path | None) -> Path:
        candidate: str | Path
        if cwd is not None:
            candidate = cwd
        elif self.config.workspace_root is not None:
            candidate = self.config.workspace_root
        else:
            candidate = Path.cwd()
        try:
            return self.config.resolve_workspace_path(candidate, must_exist=True)
        except HarnessConfigError as exc:
            raise HarnessBridgeError(str(exc)) from exc


def messages_to_content_blocks(
    messages: Sequence[Any],
    *,
    system: Any = None,
) -> list[dict[str, Any]]:
    """Convert supported Anthropic blocks to the DSH prompt vocabulary.

    Text and thinking blocks remain typed JSON objects.  Images and tool
    blocks are rejected because serializing them as text changes the plugin
    contract and cannot be made compatible with arbitrary Cordis plugins.
    """
    blocks: list[dict[str, Any]] = []
    if system is not None:
        blocks.extend(_content_blocks_for_role("system", system))
    for message in messages:
        role = getattr(message, "role", None)
        content = getattr(message, "content", None)
        if isinstance(message, Mapping):
            role = message.get("role")
            content = message.get("content")
        role_text = role if isinstance(role, str) and role else "user"
        blocks.extend(_content_blocks_for_role(role_text, content))
    return blocks


def _content_blocks_for_role(role: str, content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": f"[{role}]\n{content}"}]
    if content is None:
        return [{"type": "text", "text": f"[{role}]\n"}]
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        blocks: list[dict[str, Any]] = []
        for raw_block in content:
            block = _as_mapping(raw_block)
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise HarnessUnsupportedContentError(
                        "text content block must contain a string text field"
                    )
                blocks.append({"type": "text", "text": f"[{role}]\n{text}"})
            elif block_type == "thinking":
                thinking = block.get("thinking")
                if not isinstance(thinking, str):
                    raise HarnessUnsupportedContentError(
                        "thinking content block must contain a string thinking field"
                    )
                blocks.append({"type": "thinking", "thinking": thinking})
            else:
                raise HarnessUnsupportedContentError(
                    "DeepSeek Harness does not support this content block type: "
                    f"{block_type!r}"
                )
        return blocks
    raise HarnessUnsupportedContentError(
        "DeepSeek Harness content block must be text or a supported block list"
    )


class DeepSeekHarnessManager:
    """Own lazily-created bridges for the configured DSH runtime profiles."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.config = config
        self.api_key = api_key
        self.base_url = base_url
        self._bridges: dict[tuple[str, str], DeepSeekHarnessBridge] = {}
        self._lock = asyncio.Lock()
        self._last_error: str | None = None
        self._last_state: str | None = None

    @property
    def bridges(self) -> Mapping[tuple[str, str], DeepSeekHarnessBridge]:
        """Expose a read-only view for lifecycle diagnostics and tests."""
        return self._bridges

    async def _get_bridge(self, provider: str, model: str) -> DeepSeekHarnessBridge:
        runtime_provider = resolve_runtime_provider(provider)
        key = (runtime_provider, model)
        async with self._lock:
            bridge = self._bridges.get(key)
            if bridge is None:
                bridge = DeepSeekHarnessBridge(
                    self.config,
                    provider=provider,
                    model=model,
                    api_key=self.api_key,
                    base_url=self.base_url,
                )
                self._bridges[key] = bridge
            return bridge

    async def ensure_ready(
        self,
        *,
        provider: str,
        model: str,
        cwd: str | Path | None = None,
    ) -> None:
        """Validate configuration and initialize the requested runtime profile."""
        state, error = _runtime_preflight_status(self.config)
        if error:
            self._last_error = error
            self._last_state = state
            raise HarnessBridgeError(error)
        bridge = await self._get_bridge(provider, model)
        try:
            await bridge.start(cwd=cwd)
        except HarnessRequestTimeoutError as exc:
            self._remember_failure(bridge, HARNESS_FAILURE_TIMEOUT, exc)
            raise
        except HarnessRpcError as exc:
            self._remember_failure(bridge, HARNESS_FAILURE_INITIALIZE, exc)
            raise
        except HarnessBridgeError as exc:
            self._remember_failure(bridge, HARNESS_FAILURE_INITIALIZE, exc)
            raise
        except HarnessRuntimeClosedError as exc:
            self._remember_failure(bridge, HARNESS_FAILURE_RUNTIME_START, exc)
            raise
        except HarnessProtocolError as exc:
            self._remember_failure(bridge, HARNESS_FAILURE_RUNTIME_START, exc)
            raise
        except HarnessProcessError as exc:
            self._remember_failure(bridge, HARNESS_FAILURE_RUNTIME_START, exc)
            raise
        except Exception as exc:
            self._remember_failure(bridge, HARNESS_FAILURE_RUNTIME_START, exc)
            raise
        self._last_error = None
        self._last_state = "ready"

    def diagnostics(self) -> dict[str, object]:
        """Return safe readiness information without exposing credentials."""
        preflight_state, preflight_error = _runtime_preflight_status(self.config)
        bridges = tuple(self._bridges.values())
        running = any(bridge.is_running for bridge in bridges)
        ready = any(bridge.is_ready for bridge in bridges)
        bridge_failure = next(
            (
                (bridge.last_failure_category, bridge.last_failure_detail)
                for bridge in bridges
                if bridge.last_failure_category is not None
            ),
            (None, None),
        )
        versions = {bridge.runtime_version for bridge in bridges}
        versions.discard(None)
        bridge_failure_category, bridge_failure_detail = bridge_failure
        error = preflight_error or self._last_error
        if preflight_error:
            state = preflight_state
        elif ready:
            state = "ready"
            error = None
        elif self._last_error:
            state = self._last_state or "initialize_failed"
        elif bridge_failure_category:
            state = bridge_failure_category
            error = bridge_failure_detail
        else:
            state = "configured"
        return {
            "enabled": self.config.enabled,
            "configured": True,
            "ready": self.config.enabled and ready and error is None,
            "running": running,
            "state": state,
            "failure_category": (
                None
                if state in {"configured", "ready"}
                else state
                if state
                in {
                    HARNESS_FAILURE_TIMEOUT,
                    HARNESS_FAILURE_RUNTIME_START,
                    HARNESS_FAILURE_RUNTIME,
                    HARNESS_FAILURE_INITIALIZE,
                }
                else None
            ),
            "runtime_version": next(iter(versions), None),
            "runtime_command": list(self.config.command_argv or ()),
            "cordis_config": (
                str(self.config.cordis_config)
                if self.config.cordis_config is not None
                else None
            ),
            "plugin_allowlist": list(self.config.plugin_allowlist),
            "error": error,
        }

    async def stream_messages(
        self,
        content_blocks: Sequence[Mapping[str, Any]],
        *,
        provider: str,
        model: str,
        response_model: str | None = None,
        session_id: str | None = None,
        cwd: str | Path | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        bridge = await self._get_bridge(provider, model)
        try:
            async for frame in bridge.stream_messages(
                content_blocks,
                model=response_model or f"dsh/{provider}/{model}",
                session_id=session_id,
                cwd=cwd,
                request_id=request_id,
            ):
                yield frame
        finally:
            self._sync_bridge_failure(bridge)

    def _remember_failure(
        self,
        bridge: DeepSeekHarnessBridge,
        fallback_category: str,
        _error: BaseException,
    ) -> None:
        category = bridge.last_failure_category or fallback_category
        self._last_state = category
        self._last_error = bridge.last_failure_detail or _failure_message(category)

    def _sync_bridge_failure(self, bridge: DeepSeekHarnessBridge) -> None:
        category = bridge.last_failure_category
        if category is None:
            return
        self._last_state = category
        self._last_error = bridge.last_failure_detail or _failure_message(category)

    async def close(self) -> None:
        """Close every sidecar process owned by this application."""
        async with self._lock:
            bridges = tuple(self._bridges.values())
            self._bridges.clear()
            self._last_error = None
            self._last_state = None
        if bridges:
            await asyncio.gather(*(bridge.close() for bridge in bridges))


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise HarnessUnsupportedContentError("DeepSeek Harness content block is invalid")


def _validate_content_blocks(content_blocks: Sequence[Mapping[str, Any]]) -> None:
    for index, raw_block in enumerate(content_blocks):
        if not isinstance(raw_block, Mapping):
            raise HarnessUnsupportedContentError(
                f"DeepSeek Harness content block {index} is not an object"
            )
        block_type = raw_block.get("type")
        if block_type == "text" and isinstance(raw_block.get("text"), str):
            continue
        if block_type == "thinking" and isinstance(raw_block.get("thinking"), str):
            continue
        raise HarnessUnsupportedContentError(
            f"DeepSeek Harness does not support this content block type: {block_type!r}"
        )


def _runtime_preflight_error(config: HarnessConfig) -> str | None:
    """Return the first actionable local readiness error, if any."""
    return _runtime_preflight_status(config)[1]


def _runtime_preflight_status(config: HarnessConfig) -> tuple[str, str | None]:
    """Return a stable state and actionable local readiness error."""
    if not config.enabled:
        return "disabled", "DeepSeek Harness is disabled"
    try:
        config.validate_cordis_config()
    except HarnessConfigError as exc:
        return "cordis_invalid", str(exc)
    argv = config.command_argv
    if not argv:
        return (
            "dependency_missing",
            "DeepSeek Harness runtime command is not configured",
        )
    executable = argv[0]
    has_path = os.sep in executable or (
        os.altsep is not None and os.altsep in executable
    )
    found = Path(executable).is_file() if has_path else shutil.which(executable)
    if not found:
        return "dependency_missing", (
            f"DeepSeek Harness runtime command not found: {executable}"
        )
    if has_path and not os.access(executable, os.X_OK):
        return "dependency_missing", (
            f"DeepSeek Harness runtime is not executable: {executable}"
        )
    if tuple(argv[:2]) == DEFAULT_RUNTIME_COMMAND:
        package_entry = (
            Path(DEFAULT_RUNTIME_COMMAND[1]).parent
            / "node_modules"
            / "@deepseek-ai"
            / "dsh-sdk-jsonrpc-demo"
            / "lib"
            / "bin.js"
        )
        if not package_entry.is_file():
            return "dependency_missing", (
                "DeepSeek Harness runtime dependencies are missing; "
                "run npm install in harness/runtime"
            )
    return "configured", None


# A short alias keeps call sites readable and preserves room for a future
# protocol-specific bridge implementation.
HarnessBridge = DeepSeekHarnessBridge


def _safe_session_id(value: str | None) -> str:
    if isinstance(value, str) and value.strip():
        session_id = value.strip()
        if len(session_id.encode("utf-8")) > 512 or any(
            ord(char) < 0x20 for char in session_id
        ):
            raise HarnessBridgeError("DeepSeek Harness session id is invalid")
        return session_id
    return f"session-{uuid.uuid4().hex}"


def _event_from_notification(
    notification: JsonRpcNotification,
) -> dict[str, Any] | None:
    params = _params_mapping(notification)
    if params is None:
        return None
    event = params.get("event")
    return dict(cast(Mapping[str, Any], event)) if isinstance(event, Mapping) else None


def _is_inbox_receipt(
    notification: JsonRpcNotification, session_id: str, message_id: str
) -> bool:
    params = _params_mapping(notification)
    if notification.method != "session.event" or params is None:
        return False
    if _param_value(params, "sessionId", "session_id") != session_id:
        return False
    event = params.get("event")
    if not isinstance(event, Mapping) or event.get("type") != "agent/inbox/spliced":
        return False
    data = event.get("data")
    inserted = data.get("inserted") if isinstance(data, Mapping) else None
    return isinstance(inserted, list) and any(
        isinstance(item, Mapping) and item.get("id") == message_id for item in inserted
    )


def _is_idle(notification: JsonRpcNotification, session_id: str) -> bool:
    params = _params_mapping(notification)
    return (
        notification.method == "session.status"
        and params is not None
        and _param_value(params, "sessionId", "session_id") == session_id
        and _param_value(params, "status") == "idle"
    )


def _params_mapping(
    notification: JsonRpcNotification,
) -> Mapping[str, Any] | None:
    if not isinstance(notification.params, Mapping):
        return None
    return cast(Mapping[str, Any], notification.params)


def _param_value(params: Any, *names: str) -> Any:
    if not isinstance(params, Mapping):
        return None
    for name in names:
        value = params.get(name)
        if value is not None:
            return value
    return None


def _failure_category(exc: BaseException, *, phase: str) -> str:
    if isinstance(exc, (HarnessRequestTimeoutError, TimeoutError)):
        return HARNESS_FAILURE_TIMEOUT
    if phase == "initialize":
        return (
            HARNESS_FAILURE_RUNTIME_START
            if isinstance(exc, (HarnessRuntimeClosedError, HarnessProtocolError))
            else HARNESS_FAILURE_INITIALIZE
        )
    if phase == "start":
        return HARNESS_FAILURE_RUNTIME_START
    return HARNESS_FAILURE_RUNTIME


def _failure_message(category: str) -> str:
    return _FAILURE_MESSAGES.get(category, _FAILURE_MESSAGES[HARNESS_FAILURE_RUNTIME])


def _update_turn_identity(
    turn: HarnessTurn,
    envelope: Mapping[str, Any],
) -> None:
    """Keep the first runtime IDs observed during one turn only."""
    for identity_field in (
        "turn_id",
        "item_id",
        "one_shot_id",
        "agent_id",
        "tool_id",
        "call_id",
    ):
        if getattr(turn, identity_field) is not None:
            continue
        value = envelope.get(identity_field)
        if isinstance(value, str) and value:
            setattr(turn, identity_field, value)


def _update_turn_result(turn: HarnessTurn, event: Mapping[str, Any]) -> None:
    if event.get("type") != "turn/end":
        if event.get("type") == "assistant/message":
            turn.final_text = _assistant_text(event)
        return
    data = event.get("data")
    reason = data.get("reason") if isinstance(data, Mapping) else None
    kind = reason.get("kind") if isinstance(reason, Mapping) else None
    if isinstance(kind, str) and kind:
        turn.finish_reason = kind


def _assistant_text(event: Mapping[str, Any]) -> str:
    data = event.get("data")
    message = data.get("message") if isinstance(data, Mapping) else None
    owner = message if isinstance(message, Mapping) else data
    content = owner.get("content") if isinstance(owner, Mapping) else None
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def _assistant_event_frames(builder: SSEBuilder, event: Mapping[str, Any]) -> list[str]:
    data = event.get("data")
    message = data.get("message") if isinstance(data, Mapping) else None
    owner = message if isinstance(message, Mapping) else data
    content = owner.get("content") if isinstance(owner, Mapping) else None
    if not isinstance(content, list):
        return []
    frames: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text":
            frames.extend(builder.ensure_text_block())
            text = block.get("text")
            if isinstance(text, str) and text:
                frames.append(builder.emit_text_delta(text))
        elif block_type == "thinking":
            frames.extend(builder.ensure_thinking_block())
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking:
                frames.append(builder.emit_thinking_delta(thinking))
    return frames


def _map_finish_reason(reason: str | None) -> str:
    return {
        "completed": "end_turn",
        "max-tokens": "max_tokens",
        "max_tokens": "max_tokens",
        "error": "end_turn",
        "cancelled": "end_turn",
    }.get(reason or "", "end_turn")


def _event_key(event: Mapping[str, Any]) -> str:
    seq = event.get("seq")
    if isinstance(seq, (str, int)):
        return str(seq)
    try:
        return json.dumps(event, ensure_ascii=False, sort_keys=True)
    except TypeError, ValueError:
        return uuid.uuid4().hex


def _serialize_sse(event: str, payload: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


__all__ = [
    "DeepSeekHarnessBridge",
    "DeepSeekHarnessManager",
    "HarnessBridge",
    "HarnessBridgeError",
    "HarnessTurn",
    "HarnessUnsupportedContentError",
    "messages_to_content_blocks",
]

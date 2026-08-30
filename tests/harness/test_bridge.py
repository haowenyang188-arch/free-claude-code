"""End-to-end bridge tests against a deterministic fake JSON-RPC runtime."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from harness.bridge import (
    HARNESS_FAILURE_INITIALIZE,
    HARNESS_FAILURE_RUNTIME,
    HARNESS_FAILURE_RUNTIME_START,
    HARNESS_FAILURE_TIMEOUT,
    DeepSeekHarnessBridge,
    DeepSeekHarnessManager,
    HarnessBridgeError,
    HarnessUnsupportedContentError,
    messages_to_content_blocks,
)
from harness.config import HarnessConfig
from harness.process import HarnessRequestTimeoutError, HarnessRuntimeClosedError
from harness.protocol import JsonRpcNotification


def _fake_runtime(tmp_path: Path) -> tuple[str, ...]:
    script = tmp_path / "fake_bridge_runtime.py"
    script.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{"serverInfo":{"name":"fake-runtime"}}}), flush=True)
    elif method == "session/prompt":
        params = request["params"]
        session_id = params["sessionId"]
        message_id = "message-1"
        def send(value):
            print(json.dumps(value), flush=True)
        send({"jsonrpc":"2.0","method":"session.event","params":{"sessionId":session_id,"event":{"type":"agent/inbox/spliced","data":{"inserted":[{"id":message_id}]}}}})
        send({"jsonrpc":"2.0","id":request["id"],"result":{"messageId":message_id}})
        send({"jsonrpc":"2.0","method":"session.event","params":{"sessionId":session_id,"event":{"type":"assistant/message","seq":1,"data":{"message":{"role":"assistant","content":[{"type":"text","text":"hello from DSH"}]}}}}})
        send({"jsonrpc":"2.0","method":"session.event","params":{"sessionId":session_id,"event":{"type":"turn/end","data":{"reason":{"kind":"completed"}}}}})
        send({"jsonrpc":"2.0","method":"session.status","params":{"sessionId":session_id,"status":"idle"}})
    elif method == "shutdown":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{}}), flush=True)
        break
""".strip(),
        encoding="utf-8",
    )
    return (sys.executable, str(script))


class _ConcurrentFakeProcess:
    def __init__(self) -> None:
        self.is_running = True
        self.active_prompts = 0
        self.max_active_prompts = 0
        self._next_message = 0
        self._subscribers: set[asyncio.Queue[object]] = set()

    def subscribe(self) -> asyncio.Queue[object]:
        queue: asyncio.Queue[object] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[object]) -> None:
        self._subscribers.discard(queue)

    async def request(self, method: str, params: object = None, **_: object) -> dict:
        if method == "initialize":
            return {}
        if method == "shutdown":
            return {}
        if method != "session/prompt" or not isinstance(params, dict):
            raise AssertionError(f"unexpected request: {method}")

        self._next_message += 1
        message_number = self._next_message
        message_id = f"message-{message_number}"
        params_mapping = cast(dict[str, object], params)
        session_id = params_mapping["sessionId"]
        assert isinstance(session_id, str)
        self.active_prompts += 1
        self.max_active_prompts = max(self.max_active_prompts, self.active_prompts)
        await asyncio.sleep(0)
        self._publish(
            JsonRpcNotification(
                "session.event",
                {
                    "sessionId": session_id,
                    "event": {
                        "type": "agent/inbox/spliced",
                        "data": {"inserted": [{"id": message_id}]},
                    },
                },
            )
        )
        self._publish(
            JsonRpcNotification(
                "session.event",
                {
                    "sessionId": session_id,
                    "event": {
                        "type": "assistant/message",
                        "data": {
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": f"reply-{message_number}"}
                                ],
                            }
                        },
                    },
                },
            )
        )
        self._publish(
            JsonRpcNotification(
                "session.event",
                {
                    "sessionId": session_id,
                    "event": {
                        "type": "turn/end",
                        "data": {"reason": {"kind": "completed"}},
                    },
                },
            )
        )
        self._publish(
            JsonRpcNotification(
                "session.status",
                {"sessionId": session_id, "status": "idle"},
            )
        )
        self.active_prompts -= 1
        return {"messageId": message_id}

    def _publish(self, notification: JsonRpcNotification) -> None:
        for queue in tuple(self._subscribers):
            queue.put_nowait(notification)

    async def close(self) -> None:
        self.is_running = False


class _IdentityFakeProcess(_ConcurrentFakeProcess):
    """Emit changing runtime ids so host-owned turn identity can be checked."""

    async def request(self, method: str, params: object = None, **_: object) -> dict:
        if method != "session/prompt" or not isinstance(params, dict):
            return await super().request(method, params)

        self._next_message += 1
        message_number = self._next_message
        message_id = f"message-{message_number}"
        session_id = params["sessionId"]
        assert isinstance(session_id, str)
        one_shot_id = f"one-shot-{message_number}"

        def event_notification(event: dict[str, Any]) -> JsonRpcNotification:
            return JsonRpcNotification(
                "session.event",
                {
                    "sessionId": session_id,
                    "event": event,
                },
            )

        self._publish(
            event_notification(
                {
                    "type": "agent/inbox/spliced",
                    "oneShotId": one_shot_id,
                    "turnId": f"turn-{message_number}",
                    "itemId": f"item-{message_number}",
                    "agentId": "agent-1",
                    "data": {"inserted": [{"id": message_id}]},
                }
            )
        )
        self._publish(
            event_notification(
                {
                    "type": "assistant/message",
                    # A later untrusted runtime value must not replace the
                    # first one-shot identity bound to this host turn.
                    "oneShotId": "runtime-conflict",
                    "data": {
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "reply"}],
                        }
                    },
                }
            )
        )
        self._publish(
            event_notification(
                {
                    "type": "turn/end",
                    "data": {"reason": {"kind": "completed"}},
                }
            )
        )
        self._publish(
            JsonRpcNotification(
                "session.status",
                {"sessionId": session_id, "status": "idle"},
            )
        )
        return {"messageId": message_id}


class _FailingFakeProcess(_ConcurrentFakeProcess):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    async def request(self, method: str, params: object = None, **_: object) -> dict:
        if method == "session/prompt":
            raise self.error
        return await super().request(method, params)


class _FakeBridge(DeepSeekHarnessBridge):
    def __init__(self, config: HarnessConfig, process: _ConcurrentFakeProcess) -> None:
        super().__init__(config)
        self.fake_process = process

    async def start(self, *, cwd: str | Path | None = None) -> None:
        cast(Any, self)._process = self.fake_process
        self._initialized = True


@pytest.mark.asyncio
async def test_bridge_collects_root_turn_and_projects_anthropic_sse(
    tmp_path: Path,
) -> None:
    bridge = DeepSeekHarnessBridge(
        HarnessConfig(
            enabled=True,
            runtime_command=_fake_runtime(tmp_path),
            workspace_root=tmp_path,
        ),
        api_key="test-key",
    )
    turn = await bridge.run(
        [{"type": "text", "text": "hello"}],
        session_id="root",
        cwd=tmp_path,
    )
    assert turn.final_text == "hello from DSH"
    assert turn.finish_reason == "completed"
    assert turn.message_id == "message-1"

    frames = [
        frame
        async for frame in bridge.stream_messages(
            [{"type": "text", "text": "hello"}],
            session_id="root-2",
            cwd=tmp_path,
        )
    ]
    assert any(frame.startswith("event: message_start") for frame in frames)
    assert any("hello from DSH" in frame for frame in frames)
    assert any(frame.startswith("event: message_stop") for frame in frames)
    await bridge.close()


def test_messages_to_content_blocks_preserves_text_and_thinking_structure() -> None:
    blocks = messages_to_content_blocks(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "thinking", "thinking": "internal"},
                ],
            }
        ],
        system="system prompt",
    )

    assert blocks == [
        {"type": "text", "text": "[system]\nsystem prompt"},
        {"type": "text", "text": "[user]\nhello"},
        {"type": "thinking", "thinking": "internal"},
    ]


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "source": {"type": "base64", "data": "x"}},
        {"type": "tool_use", "id": "t1", "name": "lookup", "input": {}},
        {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
    ],
)
def test_messages_to_content_blocks_rejects_non_text_blocks(block: dict) -> None:
    with pytest.raises(HarnessUnsupportedContentError, match="content block"):
        messages_to_content_blocks([{"role": "user", "content": [block]}])


@pytest.mark.asyncio
async def test_stream_messages_projects_assistant_event_before_idle(
    tmp_path: Path,
) -> None:
    script = tmp_path / "incremental_runtime.py"
    script.write_text(
        """
import json
import sys
import time

for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{}}), flush=True)
    elif request.get("method") == "session/prompt":
        session_id = request["params"]["sessionId"]
        emit = lambda value: print(json.dumps(value), flush=True)
        emit({"jsonrpc":"2.0","method":"session.event","params":{"sessionId":session_id,"event":{"type":"agent/inbox/spliced","data":{"inserted":[{"id":"m1"}]}}}})
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"messageId":"m1"}})
        emit({"jsonrpc":"2.0","method":"session.event","params":{"sessionId":session_id,"event":{"type":"assistant/message","seq":1,"data":{"message":{"role":"assistant","content":[{"type":"text","text":"early"}]}}}}})
        time.sleep(0.3)
        emit({"jsonrpc":"2.0","method":"session.event","params":{"sessionId":session_id,"event":{"type":"turn/end","data":{"reason":{"kind":"completed"}}}}})
        emit({"jsonrpc":"2.0","method":"session.status","params":{"sessionId":session_id,"status":"idle"}})
    elif request.get("method") == "shutdown":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{}}), flush=True)
        break
""".strip(),
        encoding="utf-8",
    )
    bridge = DeepSeekHarnessBridge(
        HarnessConfig(
            enabled=True,
            runtime_command=(sys.executable, str(script)),
            workspace_root=tmp_path,
            request_timeout_seconds=2,
        )
    )
    await bridge.start(cwd=tmp_path)
    stream = bridge.stream_messages(
        [{"type": "text", "text": "hello"}], session_id="root", cwd=tmp_path
    )
    assert "message_start" in await anext(stream)
    early_frames = []
    for _ in range(3):
        early_frames.append(await asyncio.wait_for(anext(stream), timeout=0.2))
        if "early" in early_frames[-1]:
            break
    assert any("early" in frame for frame in early_frames)
    remaining = [frame async for frame in stream]
    assert any("message_stop" in frame for frame in remaining)
    await bridge.close()


def test_runtime_session_mapping_is_stable_per_workspace(tmp_path: Path) -> None:
    bridge = DeepSeekHarnessBridge(
        HarnessConfig(workspace_root=tmp_path),
    )

    first = bridge._runtime_session_id("client-session", tmp_path)
    second = bridge._runtime_session_id("client-session", tmp_path)

    assert first == second
    assert first.startswith("session-")


@pytest.mark.asyncio
async def test_same_session_prompts_are_serialized(tmp_path: Path) -> None:
    process = _ConcurrentFakeProcess()
    bridge = _FakeBridge(
        HarnessConfig(enabled=False, max_concurrency=2),
        process,
    )

    turns = await asyncio.gather(
        bridge.run([{"type": "text", "text": "one"}], session_id="same", cwd=tmp_path),
        bridge.run([{"type": "text", "text": "two"}], session_id="same", cwd=tmp_path),
    )

    assert process.max_active_prompts == 1
    assert [turn.final_text for turn in turns] == ["reply-1", "reply-2"]
    await bridge.close()


@pytest.mark.asyncio
async def test_turn_identity_is_stable_and_one_shot_is_not_reused(
    tmp_path: Path,
) -> None:
    process = _IdentityFakeProcess()
    bridge = _FakeBridge(HarnessConfig(enabled=False), process)

    first, second = await asyncio.gather(
        bridge.run([{"type": "text", "text": "one"}], session_id="first", cwd=tmp_path),
        bridge.run(
            [{"type": "text", "text": "two"}], session_id="second", cwd=tmp_path
        ),
    )

    assert first.run_id != second.run_id
    assert first.one_shot_id == "one-shot-1"
    assert second.one_shot_id == "one-shot-2"
    for turn in (first, second):
        assert turn.message_id.startswith("message-")
        assert turn.turn_id is not None
        assert turn.item_id is not None
        assert turn.agent_id == "agent-1"
        assert turn.notifications
        assert all(item["run_id"] == turn.run_id for item in turn.notifications)
        assert all(item["message_id"] == turn.message_id for item in turn.notifications)
        # Event-scoped IDs are not copied from the first notification into
        # later events.  The second assistant event intentionally carries a
        # distinct runtime one-shot value; the turn object still retains the
        # first observed value for summary metadata only.
        one_shot_ids = [item.get("one_shot_id") for item in turn.notifications]
        assert turn.one_shot_id in one_shot_ids
        assert "runtime-conflict" in one_shot_ids
        assert any(item.get("item_id") is None for item in turn.notifications)
    await bridge.close()


@pytest.mark.asyncio
async def test_bridge_classifies_timeout_without_resuming_process(
    tmp_path: Path,
) -> None:
    process = _FailingFakeProcess(HarnessRequestTimeoutError("timed out"))
    bridge = _FakeBridge(HarnessConfig(enabled=False), process)

    with pytest.raises(HarnessRequestTimeoutError):
        await bridge.run([{"type": "text", "text": "timeout"}], cwd=tmp_path)

    assert bridge.last_failure_category == HARNESS_FAILURE_TIMEOUT
    assert bridge.last_failure_detail == "DeepSeek Harness runtime request timed out."
    assert bridge.process is None
    assert process.is_running is False


@pytest.mark.asyncio
async def test_bridge_classifies_runtime_process_failure(tmp_path: Path) -> None:
    process = _FailingFakeProcess(HarnessRuntimeClosedError("closed"))
    bridge = _FakeBridge(HarnessConfig(enabled=False), process)

    with pytest.raises(HarnessRuntimeClosedError):
        await bridge.run([{"type": "text", "text": "closed"}], cwd=tmp_path)

    assert bridge.last_failure_category == HARNESS_FAILURE_RUNTIME
    assert (
        bridge.last_failure_detail
        == "DeepSeek Harness runtime failed during execution."
    )
    assert bridge.process is None
    assert process.is_running is False


@pytest.mark.asyncio
async def test_manager_classifies_runtime_start_failure(tmp_path: Path) -> None:
    manager = DeepSeekHarnessManager(
        HarnessConfig(
            enabled=True,
            runtime_command=(sys.executable, "-c", "pass"),
            workspace_root=tmp_path,
            request_timeout_seconds=1,
        )
    )

    with pytest.raises(HarnessRuntimeClosedError):
        await manager.ensure_ready(
            provider="deepseek",
            model="deepseek-test",
            cwd=tmp_path,
        )

    status = manager.diagnostics()
    assert status["ready"] is False
    assert status["state"] == HARNESS_FAILURE_RUNTIME_START
    assert status["failure_category"] == HARNESS_FAILURE_RUNTIME_START
    assert status["error"] == "DeepSeek Harness runtime failed to start."
    await manager.close()


@pytest.mark.asyncio
async def test_manager_classifies_initialize_failure(tmp_path: Path) -> None:
    script = tmp_path / "initialize_error_runtime.py"
    script.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32001,"message":"bad initialize"}}), flush=True)
    elif request.get("method") == "shutdown":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{}}), flush=True)
        break
""".strip(),
        encoding="utf-8",
    )
    manager = DeepSeekHarnessManager(
        HarnessConfig(
            enabled=True,
            runtime_command=(sys.executable, str(script)),
            workspace_root=tmp_path,
            request_timeout_seconds=1,
        )
    )

    with pytest.raises(Exception, match="bad initialize"):
        await manager.ensure_ready(
            provider="deepseek",
            model="deepseek-test",
            cwd=tmp_path,
        )

    status = manager.diagnostics()
    assert status["ready"] is False
    assert status["state"] == HARNESS_FAILURE_INITIALIZE
    assert status["failure_category"] == HARNESS_FAILURE_INITIALIZE
    assert status["error"] == "DeepSeek Harness runtime initialization failed."
    await manager.close()


@pytest.mark.asyncio
async def test_runtime_version_mismatch_fails_closed(tmp_path: Path) -> None:
    script = tmp_path / "versioned_runtime.py"
    script.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{"serverInfo":{"version":"1.0.0"}}}), flush=True)
    elif request.get("method") == "shutdown":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{}}), flush=True)
        break
""".strip(),
        encoding="utf-8",
    )
    bridge = DeepSeekHarnessBridge(
        HarnessConfig(
            enabled=True,
            runtime_command=(sys.executable, str(script)),
            workspace_root=tmp_path,
            runtime_version="2.0.0",
        )
    )

    with pytest.raises(HarnessBridgeError, match="version mismatch"):
        await bridge.start(cwd=tmp_path)

    assert bridge.is_running is False


@pytest.mark.asyncio
async def test_bridge_uses_official_initialize_shape_and_model_environment(
    tmp_path: Path,
) -> None:
    script = tmp_path / "strict_runtime.py"
    script.write_text(
        """
import json
import os
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        params = request.get("params")
        if not isinstance(params, dict) or params.get("cwd") != os.getcwd():
            print(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32602,"message":"unexpected initialize params"}}), flush=True)
        elif params.get("provider") != "deepseek-official" or params.get("model") != "deepseek-chat" or params.get("maxTokens") != 49152:
            print(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32602,"message":"missing initialize fields"}}), flush=True)
        elif os.environ.get("DSH_MODEL") != "deepseek-chat":
            print(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32602,"message":"missing model"}}), flush=True)
        elif os.environ.get("DEEPSEEK_API_KEY") != "test-key" or os.environ.get("DEEPSEEK_BASE_URL") != "http://127.0.0.1:9010/v1":
            print(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32602,"message":"missing DeepSeek endpoint environment"}}), flush=True)
        else:
            print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{"serverInfo":{"name":"fake-runtime"}}}), flush=True)
    elif request.get("method") == "shutdown":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{}}), flush=True)
        break
""".strip(),
        encoding="utf-8",
    )
    bridge = DeepSeekHarnessBridge(
        HarnessConfig(
            enabled=True,
            runtime_command=(sys.executable, str(script)),
            workspace_root=tmp_path,
        ),
        provider="deepseek",
        model="deepseek-chat",
        api_key="test-key",
        base_url="http://127.0.0.1:9010/v1",
    )

    await bridge.start(cwd=tmp_path)
    assert bridge.is_running is True
    await bridge.close()


def test_bridge_keeps_api_provider_alias_but_uses_official_runtime_route(
    tmp_path: Path,
) -> None:
    bridge = DeepSeekHarnessBridge(
        HarnessConfig(enabled=True, workspace_root=tmp_path),
        provider="deepseek",
    )

    assert bridge.provider == "deepseek"
    assert bridge.runtime_provider == "deepseek-official"


def test_manager_diagnostics_reports_bundled_runtime_state() -> None:
    manager = DeepSeekHarnessManager(HarnessConfig.from_env({"DSH_ENABLED": "true"}))

    status = manager.diagnostics()

    assert status["ready"] is False
    assert status["state"] in {"configured", "dependency_missing"}
    if status["state"] == "dependency_missing":
        error = status["error"]
        assert isinstance(error, str)
        assert "runtime" in error
    else:
        assert status["error"] is None

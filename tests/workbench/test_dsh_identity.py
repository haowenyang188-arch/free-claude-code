from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from harness.config import HarnessConfig
from workbench.backend.agents.dsh_adapter import DeepSeekHarnessAdapter
from workbench.backend.models import Event, EventType


@pytest.mark.asyncio
async def test_dsh_adapter_preserves_provider_agent_tool_call_identity(
    tmp_path: Path,
) -> None:
    adapter = DeepSeekHarnessAdapter(
        "dsh-identity",
        config=HarnessConfig(enabled=True, plugin_allowlist=()),
    )

    class FakeTurn:
        notifications = [
            {
                "type": "session_event",
                "event_type": "tool/call",
                "provider": "deepseek-official",
                "session_id": "runtime-session",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "item_id": "item-1",
                "approval_id": "approval-1",
                "one_shot_id": "one-shot-1",
                "agent_id": "agent-1",
                "tool_id": "tool-1",
                "call_id": "call-1",
                "run_id": "runtime-run-must-not-redirect",
                "message_id": "message-1",
                "payload": {"secret": "must-not-be-logged"},
            }
        ]

    async def fake_run(*args: Any, **kwargs: Any) -> FakeTurn:
        return FakeTurn()

    cast(Any, adapter.bridge).run = fake_run
    events: list[Event] = []

    async def callback(event: Event) -> None:
        events.append(event)

    adapter.set_event_callback(callback)
    await adapter.start_task("host-run", "identity", str(tmp_path))
    assert adapter.monitor_task is not None
    await adapter.monitor_task

    notification = events[1]
    assert notification.type is EventType.TOOL_STARTED
    assert notification.run_id == "host-run"
    assert notification.identity is not None
    identity = notification.identity
    assert identity.provider == "deepseek-official"
    assert identity.session_id == "runtime-session"
    assert identity.thread_id == "thread-1"
    assert identity.turn_id == "turn-1"
    assert identity.item_id == "item-1"
    assert identity.approval_id == "approval-1"
    assert identity.one_shot_id == "one-shot-1"
    assert identity.agent_id == "agent-1"
    assert identity.tool_id == "tool-1"
    assert identity.call_id == "call-1"
    assert identity.message_id == "message-1"
    assert identity.run_id == "host-run"
    assert notification.data["notification"]["call_id"] == "call-1"
    assert "payload" not in notification.data["notification"]

    finished = events[-1]
    assert finished.type is EventType.RUN_FINISHED
    assert finished.run_id == "host-run"
    assert finished.identity is not None
    assert finished.identity.one_shot_id == "one-shot-1"
    assert finished.identity.call_id == "call-1"

    await adapter.cleanup()


@pytest.mark.asyncio
async def test_dsh_adapter_does_not_emit_terminal_event_after_cancel(
    tmp_path: Path,
) -> None:
    adapter = DeepSeekHarnessAdapter(
        "dsh-cancel",
        config=HarnessConfig(enabled=True, plugin_allowlist=()),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_run(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await release.wait()
        raise AssertionError("cancel should stop the monitor before completion")

    cast(Any, adapter.bridge).run = blocked_run
    events: list[Event] = []

    async def callback(event: Event) -> None:
        events.append(event)

    adapter.set_event_callback(callback)
    await adapter.start_task("cancel-run", "cancel", str(tmp_path))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert await adapter.cancel() is True

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_CANCELLED,
    ]
    assert all(event.run_id == "cancel-run" for event in events)
    await adapter.cleanup()

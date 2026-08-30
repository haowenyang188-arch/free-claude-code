"""Optional adapter for the repository's DeepSeek Harness bridge."""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from harness.bridge import DeepSeekHarnessBridge
from harness.config import HarnessConfig
from harness.events import safe_log_context
from providers.common.identity import RuntimeIdentity

from ..models import AgentStatus, AgentType, EventType
from .base import BaseAgentAdapter


def _dsh_event_identity(
    notification: Mapping[str, Any],
    *,
    provider: str,
    agent_id: str,
    run_id: str,
    session_id: str,
    generation: str,
) -> RuntimeIdentity:
    """Project event IDs while pinning provenance to the Workbench host."""
    runtime = RuntimeIdentity.from_mapping(notification)
    return RuntimeIdentity(
        provider=provider,
        runtime_id=runtime.runtime_id,
        session_id=session_id,
        generation=generation,
        thread_id=runtime.thread_id,
        turn_id=runtime.turn_id,
        item_id=runtime.item_id,
        approval_id=runtime.approval_id,
        one_shot_id=runtime.one_shot_id,
        agent_id=agent_id,
        tool_id=runtime.tool_id,
        call_id=runtime.call_id,
        message_id=runtime.message_id,
        run_id=run_id,
    )


class DeepSeekHarnessAdapter(BaseAgentAdapter):
    """Expose the existing DSH bridge through the workbench lifecycle."""

    def __init__(
        self,
        agent_id: str,
        *,
        config: HarnessConfig | None = None,
    ) -> None:
        super().__init__(agent_id, AgentType.DEEPSEEK_HARNESS)
        self.config = config or HarnessConfig.from_env()
        self.bridge = DeepSeekHarnessBridge(
            self.config,
            api_key=os.environ.get("DEEPSEEK_API_KEY"),
            base_url=os.environ.get("DEEPSEEK_BASE_URL"),
        )
        self.session_id: str | None = None
        self.generation: str | None = None

    async def check_availability(self) -> bool:
        """Return whether DSH is explicitly enabled and locally valid."""
        if not self.config.enabled or self.config.command_argv is None:
            return False
        try:
            self.config.validate_cordis_config()
        except Exception:
            return False
        return True

    async def start_task(
        self,
        run_id: str,
        task_description: str,
        workspace_path: str,
    ) -> bool:
        previous_run_id = self.last_run_id
        self.current_run_id = run_id
        self.last_run_id = run_id
        self.workspace_path = workspace_path
        if previous_run_id != run_id:
            self.session_id = None
        self.generation = uuid.uuid4().hex
        self.status = AgentStatus.BUSY
        self._cancel_requested = False
        self._terminal_event_emitted = False
        await self.emit_event(
            EventType.RUN_STARTED,
            {
                "message": f"Starting DeepSeek Harness task: {task_description}",
                "workspace": workspace_path,
            },
            run_id=run_id,
            identity=RuntimeIdentity(
                provider=self.bridge.runtime_provider,
                agent_id=self.agent_id,
                generation=self.generation,
                run_id=run_id,
            ),
        )
        self.monitor_task = asyncio.create_task(
            self._run_task(run_id, task_description, workspace_path)
        )
        return True

    async def _run_task(
        self,
        run_id: str,
        task_description: str,
        workspace_path: str,
    ) -> None:
        host_identity = RuntimeIdentity(
            provider=self.bridge.runtime_provider,
            agent_id=self.agent_id,
            generation=self.generation,
            run_id=run_id,
            session_id=self.session_id,
        )
        try:
            turn = await self.bridge.run(
                [{"type": "text", "text": task_description}],
                session_id=run_id,
                cwd=workspace_path,
            )
            runtime_session_id = getattr(turn, "session_id", None)
            if not isinstance(runtime_session_id, str) or not runtime_session_id:
                runtime_session_id = self.session_id or run_id
            self.session_id = runtime_session_id
            host_session_id = runtime_session_id
            generation = self.generation
            if generation is None:
                raise RuntimeError("DeepSeek Harness generation is unavailable")
            for notification in turn.notifications:
                identity = _dsh_event_identity(
                    notification,
                    provider=self.bridge.runtime_provider,
                    agent_id=self.agent_id,
                    run_id=run_id,
                    session_id=host_session_id,
                    generation=generation,
                )
                context = safe_log_context(notification)
                context.update(identity.to_mapping(include_unknown=False))
                await self.emit_event(
                    self._event_type(notification),
                    {"notification": context},
                    run_id=run_id,
                    identity=identity,
                )
            if not self._terminal_event_emitted and not self._cancel_requested:
                terminal_identity = RuntimeIdentity(
                    provider=self.bridge.runtime_provider,
                    agent_id=self.agent_id,
                    generation=self.generation,
                    run_id=run_id,
                    session_id=host_session_id,
                    turn_id=getattr(turn, "turn_id", None),
                    message_id=getattr(turn, "message_id", None),
                )
                await self.emit_event(
                    EventType.RUN_FINISHED,
                    {"message": "DeepSeek Harness task completed"},
                    run_id=run_id,
                    identity=terminal_identity,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._cancel_requested:
                await self.emit_event(
                    EventType.RUN_FAILED,
                    {"error": str(exc)},
                    run_id=run_id,
                    identity=host_identity,
                )
                self.status = AgentStatus.ERROR
        finally:
            if self.current_run_id == run_id:
                self.current_run_id = None
            if self.status is not AgentStatus.ERROR:
                self.status = AgentStatus.ONLINE

    @staticmethod
    def _event_type(notification: Mapping[str, Any]) -> EventType:
        event_type = notification.get("event_type")
        if isinstance(event_type, str) and "tool" in event_type:
            return EventType.TOOL_STARTED
        if isinstance(event_type, str) and "file" in event_type:
            return EventType.FILE_CHANGED
        return EventType.AGENT_MESSAGE

    async def send_message(self, message: str) -> bool:
        """Run a follow-up prompt in the same DSH session after the turn ends."""
        if not message.strip() or (
            self.monitor_task is not None and not self.monitor_task.done()
        ):
            return False
        run_id = self.last_run_id
        workspace = self.workspace_path
        if run_id is None or workspace is None:
            return False
        self.current_run_id = run_id
        self.status = AgentStatus.BUSY
        self._cancel_requested = False
        self._terminal_event_emitted = False
        self.generation = uuid.uuid4().hex
        await self.emit_event(
            EventType.RUN_STARTED,
            {"message": "Starting DeepSeek Harness follow-up"},
            run_id=run_id,
            identity=RuntimeIdentity(
                provider=self.bridge.runtime_provider,
                session_id=self.session_id,
                agent_id=self.agent_id,
                generation=self.generation,
                run_id=run_id,
            ),
        )
        self.monitor_task = asyncio.create_task(
            self._run_task(run_id, message, workspace)
        )
        return True

    async def pause(self) -> bool:
        if os.name == "nt" or self.bridge.process is None:
            return False
        process = self.bridge.process.process
        if process is None or process.returncode is not None:
            return False
        os.kill(process.pid, signal.SIGSTOP)
        self.status = AgentStatus.ONLINE
        await self.emit_event(
            EventType.RUN_PAUSED,
            {"message": "DeepSeek Harness task paused"},
        )
        return True

    async def resume(self) -> bool:
        if os.name == "nt" or self.bridge.process is None:
            return False
        process = self.bridge.process.process
        if process is None or process.returncode is not None:
            return False
        os.kill(process.pid, signal.SIGCONT)
        self.status = AgentStatus.BUSY
        await self.emit_event(
            EventType.RUN_STARTED,
            {"message": "DeepSeek Harness task resumed"},
        )
        return True

    async def cancel(self) -> bool:
        if not self.current_run_id:
            return False
        run_id = self.current_run_id
        self._cancel_requested = True
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.monitor_task
        await self.bridge.close()
        if not self._terminal_event_emitted:
            await self.emit_event(
                EventType.RUN_CANCELLED,
                {"message": "DeepSeek Harness task cancelled"},
                run_id=run_id,
            )
        self.current_run_id = None
        self.status = AgentStatus.ONLINE
        self.monitor_task = None
        return True

    async def get_status(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "type": self.agent_type.value,
            "status": self.status.value,
            "current_run_id": self.current_run_id,
            "workspace": self.workspace_path,
            "process_running": self.bridge.is_running,
            "ready": self.bridge.is_ready,
        }

    async def cleanup(self) -> None:
        await super().cleanup()
        await self.bridge.close()

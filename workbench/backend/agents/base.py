"""Agent基础适配器"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from typing import Any

from providers.common.identity import RuntimeIdentity

if __package__:
    from ..models import AgentStatus, AgentType, Event, EventType


class BaseAgentAdapter(ABC):
    """Agent适配器基类"""

    def __init__(self, agent_id: str, agent_type: AgentType):
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.status = AgentStatus.OFFLINE
        self.current_run_id: str | None = None
        self.last_run_id: str | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.workspace_path: str | None = None
        self.event_callback: Callable[[Event], Awaitable[None]] | None = None
        self.monitor_task: asyncio.Task[None] | None = None
        self._cancel_requested = False
        self._terminal_event_emitted = False

    async def initialize(self) -> bool:
        """初始化Agent"""
        try:
            # 检查CLI是否可用
            available = await self.check_availability()
            if available:
                self.status = AgentStatus.ONLINE
                await self.emit_event(
                    EventType.AGENT_MESSAGE,
                    {"message": f"{self.agent_type.value} initialized successfully"},
                )
                return True
            else:
                self.status = AgentStatus.ERROR
                return False
        except Exception as e:
            self.status = AgentStatus.ERROR
            await self.emit_event(EventType.RUN_FAILED, {"error": str(e)})
            return False

    @abstractmethod
    async def check_availability(self) -> bool:
        """检查CLI是否可用"""
        pass

    @abstractmethod
    async def start_task(
        self, run_id: str, task_description: str, workspace_path: str
    ) -> bool:
        """启动任务"""
        pass

    @abstractmethod
    async def send_message(self, message: str) -> bool:
        """发送消息给Agent"""
        pass

    @abstractmethod
    async def pause(self) -> bool:
        """暂停执行"""
        pass

    @abstractmethod
    async def resume(self) -> bool:
        """继续执行"""
        pass

    @abstractmethod
    async def cancel(self) -> bool:
        """取消执行"""
        pass

    @abstractmethod
    async def get_status(self) -> dict[str, Any]:
        """获取当前状态"""
        pass

    def set_event_callback(self, callback: Callable[[Event], Awaitable[None]]):
        """设置事件回调"""
        self.event_callback = callback

    async def emit_event(
        self,
        event_type: EventType,
        data: dict[str, Any],
        *,
        run_id: str | None = None,
        identity: RuntimeIdentity | None = None,
    ) -> None:
        """发出事件"""
        event_run_id = run_id or self.current_run_id
        if self.event_callback and event_run_id:
            event_identity = self._event_identity(event_run_id, identity)
            event = Event(
                id=str(uuid.uuid4()),
                run_id=event_run_id,
                type=event_type,
                timestamp=datetime.now(),
                data=data,
                message=data.get("message"),
                identity=event_identity,
            )
            await self.event_callback(event)
            if event_type in {
                EventType.RUN_FINISHED,
                EventType.RUN_FAILED,
                EventType.RUN_PAUSED,
                EventType.RUN_CANCELLED,
            }:
                self._terminal_event_emitted = True

    def _event_identity(
        self, run_id: str, identity: RuntimeIdentity | None = None
    ) -> RuntimeIdentity:
        """Attach adapter-owned provenance without trusting arbitrary payloads."""
        explicit = identity or RuntimeIdentity()
        if explicit.session_id is None and explicit.thread_id is not None:
            explicit = RuntimeIdentity(session_id=explicit.thread_id).merge(explicit)

        generation = getattr(self, "generation", None)
        if not isinstance(generation, str) or not generation.strip():
            session = getattr(self, "session", None)
            generation = getattr(session, "generation", None)
        session_id = getattr(self, "session_id", None)
        if not isinstance(session_id, str) or not session_id.strip():
            session = getattr(self, "session", None)
            session_id = getattr(session, "current_session_id", None)
            if not isinstance(session_id, str) or not session_id.strip():
                session_id = getattr(session, "session_id", None)
        fallback = RuntimeIdentity(
            provider=self.agent_type.value,
            agent_id=self.agent_id,
            run_id=run_id,
            generation=generation if isinstance(generation, str) else None,
            session_id=session_id if isinstance(session_id, str) else None,
        )
        merged = explicit.merge(fallback)
        values = merged.to_mapping()
        # The Workbench event stream is authoritative for its run key.  A
        # provider cannot redirect an event into another run via metadata.
        values["run_id"] = run_id
        return RuntimeIdentity(**values)

    async def verify_completion(self) -> dict[str, Any]:
        """验证Agent完成声明"""
        verification_script = (
            "/home/gnen/.claude/skills/agent-verify-before-done/verify.sh"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                verification_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace_path,
            )

            stdout, stderr = await proc.communicate()

            return {
                "passed": proc.returncode == 0,
                "output": stdout.decode(),
                "error": stderr.decode() if proc.returncode != 0 else None,
            }
        except Exception as e:
            return {
                "passed": False,
                "output": "",
                "error": f"验证脚本执行失败: {e!s}",
            }

    async def cleanup(self):
        """清理资源"""
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.monitor_task
        self.monitor_task = None
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        self.current_run_id = None
        self.last_run_id = None
        self._cancel_requested = False
        self._terminal_event_emitted = False
        self.status = AgentStatus.OFFLINE

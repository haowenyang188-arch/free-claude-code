"""Codex Agent适配器"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from cli.codex_session import CodexSession
from cli.runtime_registry import RuntimeBackend, RuntimeRegistry
from providers.common.identity import RuntimeIdentity

from ..runtime.approval import ApprovalManager, ApprovalRecord
from .codex_app_server import CodexAppServerSession

if __package__:
    from ..models import AgentStatus, AgentType, EventType
    from .base import BaseAgentAdapter


def _codex_event_identity(value: Any) -> RuntimeIdentity:
    """Project only Codex lifecycle ids; never promote event payload data."""
    normalized = RuntimeIdentity.from_mapping(value)
    tool_id = normalized.tool_id
    if (
        tool_id is None
        and isinstance(value, dict)
        and value.get("type") in {"tool_use", "tool_result"}
    ):
        candidate = value.get("id") or value.get("tool_use_id")
        if isinstance(candidate, str) and candidate.strip():
            tool_id = candidate.strip()
    return RuntimeIdentity(
        runtime_id=normalized.runtime_id,
        session_id=normalized.session_id,
        thread_id=normalized.thread_id,
        turn_id=normalized.turn_id,
        item_id=normalized.item_id,
        approval_id=normalized.approval_id,
        one_shot_id=normalized.one_shot_id,
        tool_id=tool_id,
        call_id=normalized.call_id,
        message_id=normalized.message_id,
    )


class CodexAdapter(BaseAgentAdapter):
    """Codex CLI适配器 - 使用 CodexSession 实现真实会话管理"""

    def __init__(
        self,
        agent_id: str,
        *,
        use_app_server: bool = False,
        approval_manager: ApprovalManager | None = None,
    ):
        super().__init__(agent_id, AgentType.CODEX)
        self.cli_path = "codex"
        self.workspace_path: str = ""
        self.runtime_registry = RuntimeRegistry(
            executables={RuntimeBackend.CODEX: self.cli_path}
        )
        self.session: CodexSession | CodexAppServerSession | None = None
        self.app_server_session: CodexAppServerSession | None = None
        self.use_app_server = use_app_server
        self.approval_manager = approval_manager
        self.session_id: str | None = None
        self.generation: str | None = None

    async def check_availability(self) -> bool:
        """Return bounded, cached local Codex CLI readiness."""
        probe = await self.runtime_registry.probe(RuntimeBackend.CODEX)
        if not probe.available:
            return False
        return (
            await self.runtime_registry.probe_safe_profile(RuntimeBackend.CODEX)
        ).available

    async def start_task(
        self, run_id: str, task_description: str, workspace_path: str
    ) -> bool:
        """Start a Codex task through the shared hardened CLI session."""
        self.current_run_id = run_id
        self.last_run_id = run_id
        self.workspace_path = workspace_path
        self.status = AgentStatus.BUSY
        self._cancel_requested = False
        self._terminal_event_emitted = False

        try:
            generation = uuid.uuid4().hex
            self.generation = generation
            await self.emit_event(
                EventType.RUN_STARTED,
                {
                    "message": f"Starting Codex task: {task_description}",
                    "workspace": workspace_path,
                },
            )

            if self.use_app_server:
                requested_workspace = (
                    Path(workspace_path).expanduser().resolve(strict=True)
                )
                if (
                    self.app_server_session is not None
                    and self.app_server_session.workspace != requested_workspace
                ):
                    await self.app_server_session.stop()
                    self.app_server_session = None
                if self.app_server_session is None:
                    self.app_server_session = CodexAppServerSession(
                        workspace_path=workspace_path,
                        sandbox_mode="workspace-write",
                        approval_manager=self.approval_manager,
                        on_approval_pending=self._on_approval_pending,
                    )
                self.session = self.app_server_session
            elif self.session is None:
                self.session = CodexSession(
                    workspace_path=workspace_path,
                    sandbox_mode="read-only",
                    isolation_mode="safe",
                    runtime_registry=self.runtime_registry,
                )

            # 启动输出监听任务
            self.monitor_task = asyncio.create_task(
                self._run_codex_task(
                    task_description,
                    session_id=self.session_id,
                    generation=generation,
                )
            )

            return True

        except Exception as e:
            run_id = self.current_run_id
            self.status = AgentStatus.ERROR
            await self.emit_event(
                EventType.RUN_FAILED, {"error": str(e)}, run_id=run_id
            )
            self.current_run_id = None
            return False

    async def _on_approval_pending(self, record: ApprovalRecord) -> None:
        """Expose the exact native approval request to the Workbench UI."""
        identity = RuntimeIdentity(
            provider=record.provider,
            session_id=record.session_id,
            thread_id=record.session_id,
            turn_id=record.turn_id,
            approval_id=record.call_id,
            one_shot_id=record.call_id,
            call_id=record.call_id,
        )
        await self.emit_event(
            EventType.USER_INPUT_REQUIRED,
            {
                "message": "等待一次性审批",
                "awaiting_approval": True,
                "approval": {
                    "provider": record.provider,
                    "session_id": record.session_id,
                    "call_id": record.call_id,
                    "turn_id": record.turn_id,
                    "normalized_command": record.normalized_command,
                    "argv": list(record.argv),
                    "command_hash": record.command_hash,
                    "cwd": str(record.cwd),
                    "workspace_target": (
                        str(record.workspace_target)
                        if record.workspace_target is not None
                        else None
                    ),
                    "requested_permission": record.requested_permission,
                    "permission_scope": record.permission_scope,
                    "patch_identity": record.patch_identity,
                    "risk": record.risk.value,
                    "status": record.status.value,
                },
            },
            identity=identity,
        )

    async def _run_codex_task(
        self,
        prompt: str,
        session_id: str | None = None,
        generation: str | None = None,
    ):
        """运行 Codex 任务并处理事件流"""
        run_id = self.current_run_id
        if not self.session:
            return

        try:
            async for event in self.session.start_task(
                prompt=prompt, session_id=session_id, generation=generation
            ):
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                event_identity = _codex_event_identity(event)

                # 捕获 session_id
                if event_type == "session_info":
                    self.session_id = (
                        event_identity.session_id
                        or event_identity.thread_id
                        or self.session_id
                    )
                    await self.emit_event(
                        EventType.AGENT_MESSAGE,
                        {"message": f"Session ID: {self.session_id}"},
                        identity=event_identity,
                    )

                # 处理助手消息
                elif event_type == "assistant":
                    message_content = event.get("message", {})
                    content_list = message_content.get("content", [])
                    if not isinstance(content_list, list):
                        continue
                    for content in content_list:
                        if not isinstance(content, dict):
                            continue
                        content_identity = event_identity.merge(
                            _codex_event_identity(content)
                        )
                        if content.get("type") == "text":
                            text = content.get("text", "")
                            if text:
                                if await self._check_completion_claim(text):
                                    await self._verify_before_done(text)
                                await self.emit_event(
                                    EventType.AGENT_MESSAGE,
                                    {"message": text},
                                    identity=content_identity,
                                )
                        elif content.get("type") == "tool_use":
                            await self.emit_event(
                                EventType.TOOL_STARTED,
                                content,
                                identity=content_identity,
                            )
                        elif content.get("type") == "tool_result":
                            await self.emit_event(
                                EventType.TOOL_FINISHED,
                                content,
                                identity=content_identity,
                            )

                # 处理文件变更
                elif event_type == "file_change":
                    await self.emit_event(
                        EventType.FILE_CHANGED,
                        event,
                        identity=event_identity,
                    )

                # 处理审批请求
                elif event_type == "approval_required":
                    await self.emit_event(
                        EventType.AGENT_MESSAGE,
                        {
                            "message": "⚠️ 文件修改需要审批",
                            "diff": event.get("diff"),
                            "changed_paths": event.get("changed_paths"),
                        },
                        identity=event_identity,
                    )

                elif event_type == "approval_waiting":
                    self.status = AgentStatus.ONLINE
                    await self.emit_event(
                        EventType.USER_INPUT_REQUIRED,
                        {"message": "等待文件审批", "awaiting_approval": True},
                        identity=event_identity,
                    )

                # 处理错误
                elif event_type == "error":
                    error_msg = event.get("error", {}).get("message", "Unknown error")
                    await self.emit_event(
                        EventType.AGENT_MESSAGE,
                        {"message": f"❌ Error: {error_msg}"},
                        identity=event_identity,
                    )

                # 处理退出
                elif event_type == "exit":
                    exit_code = event.get("code", 0)
                    stderr = event.get("stderr")
                    if not self._terminal_event_emitted:
                        if self._cancel_requested:
                            await self.emit_event(
                                EventType.RUN_CANCELLED,
                                {"message": "Task cancelled"},
                                run_id=run_id,
                                identity=event_identity,
                            )
                        elif exit_code == 0:
                            await self.emit_event(
                                EventType.RUN_FINISHED,
                                {"message": "Task completed successfully"},
                                run_id=run_id,
                                identity=event_identity,
                            )
                        else:
                            await self.emit_event(
                                EventType.RUN_FAILED,
                                {
                                    "error": stderr
                                    or f"Process exited with code {exit_code}"
                                },
                                run_id=run_id,
                                identity=event_identity,
                            )
                    # Keep draining the one-shot JSONL generator so its
                    # finally block releases the process lease and busy flag.
                    # Breaking here leaves a completed session looking busy
                    # and rejects the next resume/fork turn.
                    continue

            self.status = AgentStatus.ONLINE
            self.current_run_id = None
            self._cancel_requested = False

        except Exception as e:
            await self.emit_event(
                EventType.RUN_FAILED, {"error": str(e)}, run_id=run_id
            )
            self.status = AgentStatus.ERROR
            self.current_run_id = None

    async def _check_completion_claim(self, text: str) -> bool:
        """检测是否为完成声明"""
        completion_keywords = ["完成", "done", "finished", "成功", "已实现", "已完成"]
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in completion_keywords)

    async def _verify_before_done(self, claim_text: str):
        """完成前强制验证"""
        await self.emit_event(
            EventType.AGENT_MESSAGE, {"message": "🔍 检测到完成声明, 触发自动验证..."}
        )

        # 运行验证
        result = await self.verify_completion()

        if result["passed"]:
            await self.emit_event(
                EventType.AGENT_MESSAGE,
                {"message": f"✅ 验证通过!\n\n{result['output']}"},
            )
        else:
            await self.emit_event(
                EventType.AGENT_MESSAGE,
                {
                    "message": f"❌ 验证失败!\n\n{result['error']}\n\n请修复后再声称完成。"
                },
            )
            # 标记为需要修复
            self.status = AgentStatus.ERROR

    async def send_message(self, message: str) -> bool:
        """发送后续消息 - 使用 resume 恢复 session"""
        if not message.strip() or not self.session or not self.workspace_path:
            return False

        # 如果当前正在运行，不能发送
        if self.session.is_busy:
            return False

        # 使用现有 session_id 创建新的 run
        if self.last_run_id:
            new_run_id = f"{self.last_run_id}_followup"
            return await self.start_task(new_run_id, message, self.workspace_path)

        return False

    async def pause(self) -> bool:
        """暂停执行"""
        if self.session:
            result = await self.session.pause()
            if result:
                self.status = AgentStatus.ONLINE
                await self.emit_event(EventType.RUN_PAUSED, {"message": "Task paused"})
            return result
        return False

    async def resume(self) -> bool:
        """继续执行"""
        if self.session:
            result = await self.session.resume()
            if result:
                self.status = AgentStatus.BUSY
                await self.emit_event(
                    EventType.AGENT_MESSAGE, {"message": "Task resumed"}
                )
            return result
        return False

    async def cancel(self) -> bool:
        """取消执行"""
        if self.session:
            run_id = self.current_run_id
            self._cancel_requested = True
            result = await self.session.stop()
            if result:
                self.status = AgentStatus.ONLINE
                if not self._terminal_event_emitted:
                    await self.emit_event(
                        EventType.RUN_CANCELLED,
                        {"message": "Task cancelled"},
                        run_id=run_id,
                    )
                self.current_run_id = None
            return result
        return False

    async def get_status(self) -> dict[str, Any]:
        """获取当前状态"""
        return {
            "agent_id": self.agent_id,
            "type": self.agent_type.value,
            "status": self.status.value,
            "current_run_id": self.current_run_id,
            "session_id": self.session_id,
            "generation": self.session.generation if self.session else None,
            "workspace": self.workspace_path,
            "is_busy": self.session.is_busy if self.session else False,
        }

    async def cleanup(self) -> None:
        """Close the shared session before resetting adapter lifecycle state."""
        if self.session is not None:
            await self.session.stop()
            reject = getattr(self.session, "reject", None)
            if callable(reject):
                reject()
            self.session = None
            self.app_server_session = None
        await super().cleanup()

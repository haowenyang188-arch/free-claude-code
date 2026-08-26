"""Codex Agent适配器"""

from __future__ import annotations

import asyncio
from typing import Any

from .codex_session_wrapper import CodexSessionWrapper

if __package__:
    from ..models import AgentStatus, AgentType, EventType
    from .base import BaseAgentAdapter


class CodexAdapter(BaseAgentAdapter):
    """Codex CLI适配器 - 使用 CodexSession 实现真实会话管理"""

    def __init__(self, agent_id: str):
        super().__init__(agent_id, AgentType.CODEX)
        self.cli_path = "codex"
        self.workspace_path: str = ""
        self.session: CodexSessionWrapper | None = None
        self.session_id: str | None = None

    async def check_availability(self) -> bool:
        """检查Codex CLI是否可用"""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return proc.returncode == 0 and b"codex" in stdout.lower()
        except Exception:
            return False

    async def start_task(
        self, run_id: str, task_description: str, workspace_path: str
    ) -> bool:
        """启动Codex任务 - 使用 CodexSessionWrapper"""
        self.current_run_id = run_id
        self.last_run_id = run_id
        self.workspace_path = workspace_path
        self.status = AgentStatus.BUSY
        self._cancel_requested = False
        self._terminal_event_emitted = False

        try:
            await self.emit_event(
                EventType.RUN_STARTED,
                {
                    "message": f"Starting Codex task: {task_description}",
                    "workspace": workspace_path,
                },
            )

            # 创建 CodexSessionWrapper
            if self.session is None:
                self.session = CodexSessionWrapper(
                    workspace_path=workspace_path,
                    sandbox_mode="read-only",
                )

            # 启动输出监听任务
            self.monitor_task = asyncio.create_task(
                self._run_codex_task(task_description, session_id=self.session_id)
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

    async def _run_codex_task(self, prompt: str, session_id: str | None = None):
        """运行 Codex 任务并处理事件流"""
        run_id = self.current_run_id
        if not self.session:
            return

        try:
            async for event in self.session.start_task(
                prompt=prompt, session_id=session_id
            ):
                event_type = event.get("type")

                # 捕获 session_id
                if event_type == "session_info":
                    self.session_id = event.get("session_id")
                    await self.emit_event(
                        EventType.AGENT_MESSAGE,
                        {"message": f"Session ID: {self.session_id}"},
                    )

                # 处理助手消息
                elif event_type == "assistant":
                    message_content = event.get("message", {})
                    content_list = message_content.get("content", [])
                    for content in content_list:
                        if content.get("type") == "text":
                            text = content.get("text", "")
                            if text:
                                if await self._check_completion_claim(text):
                                    await self._verify_before_done(text)
                                await self.emit_event(
                                    EventType.AGENT_MESSAGE, {"message": text}
                                )
                        elif content.get("type") == "tool_use":
                            await self.emit_event(EventType.TOOL_STARTED, content)
                        elif content.get("type") == "tool_result":
                            await self.emit_event(EventType.TOOL_FINISHED, content)

                # 处理文件变更
                elif event_type == "file_change":
                    await self.emit_event(EventType.FILE_CHANGED, event)

                # 处理审批请求
                elif event_type == "approval_required":
                    await self.emit_event(
                        EventType.AGENT_MESSAGE,
                        {
                            "message": "⚠️ 文件修改需要审批",
                            "diff": event.get("diff"),
                            "changed_paths": event.get("changed_paths"),
                        },
                    )

                elif event_type == "approval_waiting":
                    self.status = AgentStatus.ONLINE
                    await self.emit_event(
                        EventType.USER_INPUT_REQUIRED,
                        {"message": "等待文件审批", "awaiting_approval": True},
                    )

                # 处理错误
                elif event_type == "error":
                    error_msg = event.get("error", {}).get("message", "Unknown error")
                    await self.emit_event(
                        EventType.AGENT_MESSAGE, {"message": f"❌ Error: {error_msg}"}
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
                            )
                        elif exit_code == 0:
                            await self.emit_event(
                                EventType.RUN_FINISHED,
                                {"message": "Task completed successfully"},
                                run_id=run_id,
                            )
                        else:
                            await self.emit_event(
                                EventType.RUN_FAILED,
                                {"error": stderr or f"Process exited with code {exit_code}"},
                                run_id=run_id,
                            )
                    break

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
            result = await self.session.resume_process()
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
            "workspace": self.workspace_path,
            "is_busy": self.session.is_busy if self.session else False,
        }

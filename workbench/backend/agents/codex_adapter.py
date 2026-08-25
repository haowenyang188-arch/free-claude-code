"""Codex Agent适配器"""

from __future__ import annotations

import asyncio
import json
from typing import Any

if __package__:
    from ..models import AgentStatus, AgentType, EventType
    from .base import BaseAgentAdapter


class CodexAdapter(BaseAgentAdapter):
    """Codex CLI适配器"""

    def __init__(self, agent_id: str):
        super().__init__(agent_id, AgentType.CODEX)
        self.cli_path = "codex"
        self.workspace_path: str = ""

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
            return proc.returncode == 0 and b"codex-cli" in stdout
        except Exception:
            return False

    async def start_task(
        self, run_id: str, task_description: str, workspace_path: str
    ) -> bool:
        """启动Codex任务"""
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

            # 启动Codex进程
            # 注意: 实际命令需要根据Codex CLI的实际接口调整
            self.process = await asyncio.create_subprocess_exec(
                self.cli_path,
                "exec",
                "--json",
                "--cd",
                workspace_path,
                task_description,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=workspace_path,
            )

            # 启动输出监听任务
            self.monitor_task = asyncio.create_task(self._monitor_output())

            return True

        except Exception as e:
            run_id = self.current_run_id
            self.status = AgentStatus.ERROR
            await self.emit_event(
                EventType.RUN_FAILED, {"error": str(e)}, run_id=run_id
            )
            self.current_run_id = None
            return False

    async def _monitor_output(self):
        """监听Codex输出"""
        process = self.process
        run_id = self.current_run_id
        if not process or not process.stdout:
            return

        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break

                try:
                    # 尝试解析JSON输出
                    data = json.loads(line.decode(errors="replace"))
                    await self._handle_codex_event(data)
                except json.JSONDecodeError:
                    # 普通文本输出
                    text = line.decode(errors="replace").strip()
                    if text:
                        # 检测完成声明
                        if await self._check_completion_claim(text):
                            await self._verify_before_done(text)

                        await self.emit_event(
                            EventType.AGENT_MESSAGE, {"message": text}
                        )

            # 进程结束
            returncode = await process.wait()
            if not self._terminal_event_emitted:
                if self._cancel_requested:
                    await self.emit_event(
                        EventType.RUN_CANCELLED,
                        {"message": "Task cancelled"},
                        run_id=run_id,
                    )
                elif returncode == 0:
                    await self.emit_event(
                        EventType.RUN_FINISHED,
                        {"message": "Task completed successfully"},
                        run_id=run_id,
                    )
                else:
                    await self.emit_event(
                        EventType.RUN_FAILED,
                        {"error": f"Process exited with code {returncode}"},
                        run_id=run_id,
                    )
            self.status = AgentStatus.ONLINE
            self.current_run_id = None
            self.process = None
            self._cancel_requested = False

        except Exception as e:
            await self.emit_event(
                EventType.RUN_FAILED, {"error": str(e)}, run_id=run_id
            )
            self.status = AgentStatus.ERROR
            self.current_run_id = None
            self.process = None

    async def _check_completion_claim(self, text: str) -> bool:
        """检测是否为完成声明"""
        completion_keywords = ["完成", "done", "finished", "成功", "已实现", "已完成"]
        return any(keyword in text.lower() for keyword in completion_keywords)

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

    async def _handle_codex_event(self, data: dict[str, Any]):
        """处理Codex事件"""
        event_type = data.get("type")

        if event_type == "message":
            await self.emit_event(EventType.AGENT_MESSAGE, data)
        elif event_type == "tool_call":
            await self.emit_event(EventType.TOOL_STARTED, data)
        elif event_type == "tool_result":
            await self.emit_event(EventType.TOOL_FINISHED, data)
        elif event_type == "file_change":
            await self.emit_event(EventType.FILE_CHANGED, data)
        elif event_type == "terminal":
            await self.emit_event(EventType.TERMINAL_OUTPUT, data)
        elif event_type == "user_input_required":
            self.status = AgentStatus.ONLINE  # 等待输入时释放busy状态
            await self.emit_event(EventType.USER_INPUT_REQUIRED, data)

    async def send_message(self, message: str) -> bool:
        """Run a follow-up ``exec`` turn after the current process is idle."""
        if (
            not message.strip()
            or self.process is not None
            or self.last_run_id is None
            or not self.workspace_path
        ):
            return False
        return await self.start_task(self.last_run_id, message, self.workspace_path)

    async def pause(self) -> bool:
        """暂停执行"""
        if self.process:
            try:
                self.process.send_signal(19)  # SIGSTOP
                self.status = AgentStatus.ONLINE
                await self.emit_event(EventType.RUN_PAUSED, {"message": "Task paused"})
                return True
            except Exception:
                return False
        return False

    async def resume(self) -> bool:
        """继续执行"""
        if self.process:
            try:
                self.process.send_signal(18)  # SIGCONT
                self.status = AgentStatus.BUSY
                await self.emit_event(
                    EventType.AGENT_MESSAGE, {"message": "Task resumed"}
                )
                return True
            except Exception:
                return False
        return False

    async def cancel(self) -> bool:
        """取消执行"""
        if self.process:
            try:
                process = self.process
                run_id = self.current_run_id
                self._cancel_requested = True
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
                self.status = AgentStatus.ONLINE
                if not self._terminal_event_emitted:
                    await self.emit_event(
                        EventType.RUN_CANCELLED,
                        {"message": "Task cancelled"},
                        run_id=run_id,
                    )
                self.current_run_id = None
                self.process = None
                return True
            except TimeoutError:
                process.kill()
                await process.wait()
                if not self._terminal_event_emitted:
                    await self.emit_event(
                        EventType.RUN_CANCELLED,
                        {"message": "Task cancelled"},
                        run_id=run_id,
                    )
                self.status = AgentStatus.ONLINE
                self.current_run_id = None
                self.process = None
                return True
            except Exception:
                return False
        return False

    async def get_status(self) -> dict[str, Any]:
        """获取当前状态"""
        return {
            "agent_id": self.agent_id,
            "type": self.agent_type.value,
            "status": self.status.value,
            "current_run_id": self.current_run_id,
            "workspace": self.workspace_path,
            "process_running": self.process is not None
            and self.process.returncode is None,
        }

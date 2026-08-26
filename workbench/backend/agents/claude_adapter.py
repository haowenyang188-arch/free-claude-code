"""Claude Code Agent适配器"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from cli.process_registry import register_process, unregister_process
from cli.runtime_environment import build_cli_environment
from cli.runtime_registry import RuntimeBackend, RuntimeRegistry

if __package__:
    from ..models import AgentStatus, AgentType, EventType
    from .base import BaseAgentAdapter


class ClaudeCodeAdapter(BaseAgentAdapter):
    """Claude Code CLI适配器"""

    def __init__(self, agent_id: str):
        super().__init__(agent_id, AgentType.CLAUDE_CODE)
        self.cli_path = "claude"
        self.workspace_path: str = ""
        self.generation: str | None = None
        self.runtime_registry = RuntimeRegistry(
            executables={RuntimeBackend.CLAUDE: self.cli_path}
        )

    async def check_availability(self) -> bool:
        """Return bounded, cached local Claude Code readiness."""
        return (await self.runtime_registry.probe(RuntimeBackend.CLAUDE)).available

    async def start_task(
        self, run_id: str, task_description: str, workspace_path: str
    ) -> bool:
        """启动Claude Code任务"""
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
                    "message": f"Starting Claude Code task: {task_description}",
                    "workspace": workspace_path,
                },
            )

            generation = uuid.uuid4().hex
            self.generation = generation
            self.process = await asyncio.create_subprocess_exec(
                self.cli_path,
                "--print",
                "--output-format",
                "text",
                "--safe-mode",
                "--strict-mcp-config",
                "--permission-mode",
                "plan",
                task_description,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=workspace_path,
                env=build_cli_environment(RuntimeBackend.CLAUDE),
            )
            if self.process.pid:
                register_process(self.process.pid, generation=generation)

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
        """监听Claude Code输出"""
        process = self.process
        run_id = self.current_run_id
        generation = self.generation
        if not process or not process.stdout:
            return

        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break

                text = line.decode(errors="replace").strip()
                if text:
                    # 检测完成声明
                    if await self._check_completion_claim(text):
                        await self._verify_before_done(text)

                    await self.emit_event(EventType.AGENT_MESSAGE, {"message": text})

            # Provider stderr is deliberately discarded: this adapter exposes
            # a stable exit code, not raw authentication diagnostics.
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
        finally:
            if process and process.pid and generation is not None:
                unregister_process(process.pid, generation=generation)

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

    async def send_message(self, message: str) -> bool:
        """Run a follow-up ``--print`` turn after the current process is idle."""
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
                generation = self.generation
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
                if process.pid and generation is not None:
                    unregister_process(process.pid, generation=generation)
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
                if process.pid and generation is not None:
                    unregister_process(process.pid, generation=generation)
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
            "generation": self.generation,
            "workspace": self.workspace_path,
            "process_running": self.process is not None
            and self.process.returncode is None,
        }

    async def cleanup(self) -> None:
        """Release the exact process lease after base cleanup stops the child."""
        process = self.process
        generation = self.generation
        await super().cleanup()
        if process and process.pid and generation is not None:
            unregister_process(process.pid, generation=generation)

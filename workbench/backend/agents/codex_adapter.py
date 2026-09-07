"""Codex Agent适配器"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from cli.codex_session import CodexSession
from cli.runtime_registry import RuntimeBackend, RuntimeRegistry
from providers.common.identity import RuntimeIdentity

from ..runtime.approval import ApprovalManager, ApprovalRecord, ApprovalScope
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
        self,
        run_id: str,
        task_description: str,
        workspace_path: str,
        *,
        sandbox_mode: str | None = None,
        approval_scope: str = ApprovalScope.NORMAL,
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
                resolved_sandbox = sandbox_mode or "workspace-write"
                if (
                    self.app_server_session is not None
                    and (
                        self.app_server_session.workspace != requested_workspace
                        or self.app_server_session.sandbox_mode != resolved_sandbox
                        or self.app_server_session.approval_scope != approval_scope
                    )
                ):
                    await self.app_server_session.stop()
                    self.app_server_session = None
                    self.session_id = None
                if self.app_server_session is None:
                    self.app_server_session = CodexAppServerSession(
                        workspace_path=workspace_path,
                        sandbox_mode=resolved_sandbox,
                        approval_manager=self.approval_manager,
                        on_approval_pending=self._on_approval_pending,
                        approval_scope=approval_scope,
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
            thread_id=record.thread_id or record.session_id,
            item_id=record.item_id,
            approval_id=record.approval_id,
            turn_id=record.turn_id,
            one_shot_id=record.one_shot_id,
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
                    "one_shot_id": record.one_shot_id,
                    "thread_id": record.thread_id,
                    "item_id": record.item_id,
                    "approval_id": record.approval_id,
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
                                    await self._surface_completion_claim(text)
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
                            # Role contract RC-2: the Reviewer reports that its
                            # own turn ended.  It must NOT declare the task
                            # complete -- terminality belongs to the SOP Engine.
                            await self.emit_event(
                                EventType.RUN_FINISHED,
                                {
                                    "message": "Reviewer turn finished; verdict "
                                    "subject to SOP Engine routing"
                                },
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

    async def _surface_completion_claim(self, claim_text: str):
        """把完成声明转成交给审核方的自检证据.

        自检只回答"我自己的检查跑没跑过", 不回答"这件事能不能算完":
        验收权在 Codex(审核) 与 Engine(终裁), adapter 无权自行结案(RC-4)。
        """
        await self.emit_event(
            EventType.AGENT_MESSAGE,
            {
                "message": "🔍 检测到完成声明, 执行自检"
                "(结果仅作为证据上报, 不构成验收结论)..."
            },
        )

        result = await self.run_self_check()

        if result["status"] == "skipped":
            await self.emit_event(
                EventType.AGENT_MESSAGE,
                {
                    "message": "⚠️ 自检脚本不可用, 已跳过自检;"
                    "该完成声明将由审核方独立核验。"
                },
            )
            return

        if result["passed"]:
            await self.emit_event(
                EventType.AGENT_MESSAGE,
                {"message": f"自检通过(非验收结论):\n\n{result['output']}"},
            )
        else:
            await self.emit_event(
                EventType.AGENT_MESSAGE,
                {
                    "message": f"自检未通过(非验收结论, 已作为证据上报):\n\n{result['error']}"
                },
            )
            # 自检确实失败 -> 标记本适配器需要修复(进程存活态, 非 SOP 终态)
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
        """取消执行 — turn/interrupt 优先，进程 kill 仅作 fallback"""
        if self.session:
            run_id = self.current_run_id
            self._cancel_requested = True
            if (
                isinstance(self.session, CodexAppServerSession)
                and self.session.is_busy
            ):
                result = await self.session.interrupt_turn()
                if not result:
                    result = await self.session.stop()
            else:
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

    async def interrupt(self) -> bool:
        """Turn-level interrupt (preferred over process kill).

        Only cancels the current codex turn; the app-server process and the
        thread survive, so a follow-up turn can run afterwards.  Falls back to
        ``cancel()`` (process stop) for non-app-server sessions.
        """
        if isinstance(self.session, CodexAppServerSession):
            return await self.session.interrupt_turn()
        return await self.cancel()

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

"""Claude Code Agent适配器"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar

from cli.process_registry import register_process, unregister_process
from cli.runtime_environment import build_cli_environment
from cli.runtime_registry import RuntimeBackend, RuntimeRegistry
from providers.common.identity import RuntimeIdentity

if __package__:
    from ..models import AgentStatus, AgentType, EventType
    from ..runtime.approval import ApprovalManager, ApprovalRecord
    from .base import BaseAgentAdapter
    from .claude_compatibility import ClaudeCompatibilitySession


class ClaudeCodeAdapter(BaseAgentAdapter):
    """Claude Code CLI适配器"""

    _HOOK_EVENTS: ClassVar[frozenset[str]] = frozenset(
        {"PreToolUse", "PermissionRequest"}
    )
    _HOOK_DECISIONS: ClassVar[frozenset[str]] = frozenset({"silent", "deny"})
    _RUNTIME_RESULTS: ClassVar[frozenset[str]] = frozenset(
        {"not_probed", "timeout", "environment_limitation", "failed"}
    )
    _RUNTIME_STATUS: ClassVar[dict[str, str]] = {
        "timeout": "TIMEOUT",
        "environment_limitation": "ENVIRONMENT_LIMITATION",
        "failed": "FAILED",
    }

    def __init__(
        self,
        agent_id: str,
        *,
        hook_installed: bool = False,
        use_compatibility_bridge: bool = False,
        approval_manager: ApprovalManager | None = None,
    ):
        super().__init__(agent_id, AgentType.CLAUDE_CODE)
        self.cli_path = "claude"
        self.workspace_path: str = ""
        self.generation: str | None = None
        self.session_id: str | None = None
        self.use_compatibility_bridge = use_compatibility_bridge
        self.approval_manager = approval_manager
        self.compatibility_session: ClaudeCompatibilitySession | None = None
        self.hook_installed = hook_installed
        self._runtime_generation_required = False
        self._approval_health = {
            "hook_installed": hook_installed,
            "hook_trust": "unverified" if hook_installed else "disabled",
            "hook_active": False,
            "last_hook_decision": None,
            "runtime_result": "not_probed",
        }
        self.runtime_registry = RuntimeRegistry(
            executables={RuntimeBackend.CLAUDE: self.cli_path}
        )

    async def check_availability(self) -> bool:
        """Return bounded, cached local Claude Code readiness."""
        probe = await self.runtime_registry.probe(RuntimeBackend.CLAUDE)
        if not probe.available:
            return False
        return (
            await self.runtime_registry.probe_safe_profile(RuntimeBackend.CLAUDE)
        ).available

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
            generation = uuid.uuid4().hex
            self.generation = generation
            self._runtime_generation_required = True
            self._reset_approval_health()
            await self.emit_event(
                EventType.RUN_STARTED,
                {
                    "message": f"Starting Claude Code task: {task_description}",
                    "workspace": workspace_path,
                },
            )

            if self.use_compatibility_bridge:
                requested_workspace = (
                    Path(workspace_path).expanduser().resolve(strict=True)
                )
                if (
                    self.compatibility_session is not None
                    and self.compatibility_session.workspace != requested_workspace
                ):
                    await self.compatibility_session.stop()
                    self.compatibility_session = None
                    self.session_id = None
                if self.compatibility_session is None:
                    self.compatibility_session = ClaudeCompatibilitySession(
                        workspace_path=workspace_path,
                        claude_bin=self.cli_path,
                        approval_manager=self.approval_manager,
                        on_approval_pending=self._on_approval_pending,
                    )
                self.monitor_task = asyncio.create_task(
                    self._monitor_compatibility(
                        task_description,
                        session_id=self.session_id,
                        generation=generation,
                    )
                )
                return True

            self.process = await asyncio.create_subprocess_exec(
                self.cli_path,
                "--print",
                "--output-format",
                "text",
                "--safe-mode",
                "--strict-mcp-config",
                "--permission-mode",
                "plan",
                # S0-R P3: HARD whitelist — removes Bash/Write/Edit/NotebookEdit
                # from the tool schema. permission-mode plan is NOT the boundary.
                "--tools=Read,Grep,Glob",
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

    async def _on_approval_pending(self, record: ApprovalRecord) -> None:
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
            identity=RuntimeIdentity(
                provider=record.provider,
                session_id=record.session_id,
                thread_id=record.thread_id,
                turn_id=record.turn_id,
                item_id=record.item_id,
                approval_id=record.approval_id,
                one_shot_id=record.one_shot_id,
                call_id=record.call_id,
            ),
        )

    async def _monitor_compatibility(
        self,
        prompt: str,
        *,
        session_id: str | None,
        generation: str,
    ) -> None:
        session = self.compatibility_session
        run_id = self.current_run_id
        if session is None:
            return
        try:
            async for event in session.start_task(
                prompt,
                session_id=session_id,
                generation=generation,
            ):
                event_type = event.get("type")
                # A compatibility monitor may outlive a cancelled/restarted
                # run.  Never let its delayed stream mutate the new run.
                if self.generation != generation:
                    return
                if event_type == "session_info":
                    value = event.get("session_id")
                    if isinstance(value, str) and value:
                        self.session_id = value
                elif event_type == "assistant":
                    message = event.get("message")
                    content = (
                        message.get("content", []) if isinstance(message, dict) else []
                    )
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "text" and isinstance(block.get("text"), str):
                            await self.emit_event(
                                EventType.AGENT_MESSAGE,
                                {"message": block["text"]},
                                run_id=run_id,
                            )
                        elif block_type == "tool_use":
                            await self.emit_event(
                                EventType.TOOL_STARTED,
                                block,
                                run_id=run_id,
                            )
                        elif block_type == "tool_result":
                            await self.emit_event(
                                EventType.TOOL_FINISHED,
                                block,
                                run_id=run_id,
                            )
                elif event_type == "error":
                    error = event.get("error")
                    message = error.get("message") if isinstance(error, dict) else None
                    await self.emit_event(
                        EventType.AGENT_MESSAGE,
                        {"message": message or "Claude compatibility bridge error"},
                        run_id=run_id,
                    )
                elif event_type == "exit" and not self._terminal_event_emitted:
                    code = event.get("code", 1)
                    if self._cancel_requested:
                        await self.emit_event(
                            EventType.RUN_CANCELLED,
                            {"message": "Task cancelled"},
                            run_id=run_id,
                        )
                    elif code == 0:
                        await self.emit_event(
                            EventType.RUN_FINISHED,
                            {"message": "Run finished (exit code 0)"},
                            run_id=run_id,
                        )
                    else:
                        await self.emit_event(
                            EventType.RUN_FAILED,
                            {"error": f"Process exited with code {code}"},
                            run_id=run_id,
                        )
            if self.generation == generation:
                self.status = AgentStatus.ONLINE
                self.current_run_id = None
                self._cancel_requested = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.generation == generation:
                await self.emit_event(
                    EventType.RUN_FAILED,
                    {"error": str(exc)},
                    run_id=run_id,
                )
                self.status = AgentStatus.ERROR
                self.current_run_id = None

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
                        await self._surface_completion_claim(text)

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
                        {"message": "Run finished (exit code 0)"},
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
        """Run a follow-up ``--print`` turn after the current process is idle."""
        if not message.strip() or self.last_run_id is None or not self.workspace_path:
            return False
        if self.use_compatibility_bridge:
            if (
                self.compatibility_session is not None
                and self.compatibility_session.is_busy
            ):
                return False
        elif self.process is not None:
            return False
        return await self.start_task(self.last_run_id, message, self.workspace_path)

    async def pause(self) -> bool:
        """暂停执行"""
        if self.use_compatibility_bridge and self.compatibility_session is not None:
            return await self.compatibility_session.pause()
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
        if self.use_compatibility_bridge and self.compatibility_session is not None:
            return await self.compatibility_session.resume()
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
        if self.use_compatibility_bridge and self.compatibility_session is not None:
            run_id = self.current_run_id
            self._cancel_requested = True
            if self.monitor_task is not None and not self.monitor_task.done():
                self.monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.monitor_task
            result = await self.compatibility_session.stop()
            if result:
                if not self._terminal_event_emitted:
                    await self.emit_event(
                        EventType.RUN_CANCELLED,
                        {"message": "Task cancelled"},
                        run_id=run_id,
                    )
                self.current_run_id = None
                self.status = AgentStatus.ONLINE
            return result
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
        approval_health = dict(self._approval_health)
        approval_health["status"] = self._approval_health_status(approval_health)
        return {
            "agent_id": self.agent_id,
            "type": self.agent_type.value,
            "status": self.status.value,
            "current_run_id": self.current_run_id,
            "generation": self.generation,
            "workspace": self.workspace_path,
            "process_running": (
                self.compatibility_session.process is not None
                and self.compatibility_session.process.returncode is None
                if self.use_compatibility_bridge
                and self.compatibility_session is not None
                else self.process is not None and self.process.returncode is None
            ),
            "approval_mode": (
                "compatibility_bridge"
                if self.use_compatibility_bridge
                else "legacy_cli"
            ),
            "approval_health": approval_health,
        }

    def observe_hook_activity(self, event: Mapping[str, Any]) -> None:
        """Trust only normalized activity from this adapter's current run.

        Hook configuration is not proof that Claude actually invoked the hook.
        A matching generation and valid telemetry decision are required before
        the health contract reports an active hook.
        """
        if not self.hook_installed:
            return
        generation = self.generation
        if not generation or event.get("generation") != generation:
            return
        self._runtime_generation_required = True
        if event.get("backend") != "claude":
            return
        if event.get("event") not in self._HOOK_EVENTS:
            return
        decision = event.get("decision")
        if decision not in self._HOOK_DECISIONS:
            return
        if self._approval_health.get("runtime_result") in self._RUNTIME_STATUS:
            return
        self._approval_health.update(
            {
                "hook_trust": "trusted",
                "hook_active": True,
                "last_hook_decision": decision,
            }
        )

    def record_hook_runtime_result(
        self, result: str, *, generation: str | None = None
    ) -> bool:
        """Record a probe outcome only for the adapter generation it belongs to.

        A result received while a run is active must carry that run's
        generation.  Calls made before the first run remain supported for
        startup diagnostics, where there is no generation to bind.
        """
        normalized = result.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized not in self._RUNTIME_RESULTS:
            allowed = ", ".join(sorted(self._RUNTIME_RESULTS))
            raise ValueError(
                f"unsupported Claude hook runtime result: {result!r}; "
                f"expected one of {allowed}"
            )
        current_generation = self.generation
        if current_generation is not None:
            self._runtime_generation_required = True
            if generation != current_generation:
                return False
        elif self._runtime_generation_required or generation is not None:
            # Once a run has existed, an unbound late result is stale by
            # definition and must not alter the next/idle health snapshot.
            return False
        self._approval_health["runtime_result"] = normalized
        # Explicit timeout/failure evidence supersedes any earlier activity.
        if normalized in self._RUNTIME_STATUS:
            self._approval_health["hook_active"] = False
            self._approval_health["hook_trust"] = (
                "unverified" if self.hook_installed else "disabled"
            )
        return True

    def set_hook_runtime_result(
        self, result: str, *, generation: str | None = None
    ) -> bool:
        """Compatibility alias for callers that use setter terminology."""
        return self.record_hook_runtime_result(result, generation=generation)

    def _reset_approval_health(self) -> None:
        self._approval_health.update(
            {
                "hook_trust": "unverified" if self.hook_installed else "disabled",
                "hook_active": False,
                "last_hook_decision": None,
                "runtime_result": "not_probed",
            }
        )

    @classmethod
    def _approval_health_status(cls, health: Mapping[str, Any]) -> str:
        if health.get("hook_installed") is not True:
            return "DISABLED"
        runtime_result = health.get("runtime_result")
        if runtime_result in cls._RUNTIME_STATUS:
            return cls._RUNTIME_STATUS[runtime_result]
        if health.get("hook_active") is True:
            return "ACTIVE"
        return "HOOK_REGISTERED_BUT_NOT_ACTIVE"

    async def cleanup(self) -> None:
        """Release the exact process lease after base cleanup stops the child."""
        process = self.process
        generation = self.generation
        await super().cleanup()
        if self.compatibility_session is not None:
            await self.compatibility_session.stop()
            self.compatibility_session = None
        if process and process.pid and generation is not None:
            unregister_process(process.pid, generation=generation)
        self.generation = None
        self.session_id = None
        self._reset_approval_health()

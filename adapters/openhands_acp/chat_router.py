"""ChatRouter:一条消息 → 多个 runtime 并行回答(标签推送 + 部分降级)。

设计契约(用户确认版):
- Claude/Codex 两个 runtime 同时存在:每个 Canvas session 下各自持有一条
  持久线程(thread dict 存 runtime 侧 session id),全部随 load_session 恢复。
  单一 runtime 的独立机器人（--runtime claude/codex）回答不带标签。
- 每完成一个 runtime 就推送一条消息;单个 runtime 失败只在自己这里显示
  原因,其余照常回答。两个 runtime 都经 cc-switch 网关,同源故障会同时降级。
- 系统规范(回答格式)由 chat_skills/*.md 注入各 runtime。
DSH 已按需求从聊天机器人移除;SOP 流水线的 DSH 执行角色不受影响。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from adapters.openhands_acp.chat_runtimes import (
    ChatRuntimeError,
    codex_chat_once,
    claude_chat_once,
)

LABELS = {"claude": "Claude", "codex": "Codex"}

_SKILLS_DIR = Path(__file__).resolve().parent / "chat_skills"


def load_system_prompt(skills_dir: str | os.PathLike | None = None) -> str:
    """读取回答规范:目录下全部 *.md 按文件名排序拼接。"""
    root = Path(skills_dir) if skills_dir else _SKILLS_DIR
    if not root.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(root.glob("*.md")):
        try:
            body = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if body:
            parts.append(body)
    return "\n\n---\n\n".join(parts)


class ChatRouter:
    """并行扇出调度器。一个实例挂在 SopAcpAgent 上,跨全部会话复用。"""

    def __init__(
        self,
        *,
        runtime_names: tuple[str, ...] | None = None,
        timeout: float | None = None,
        system_prompt: str | None = None,
        skills_dir: str | os.PathLike | None = None,
    ) -> None:
        configured = os.environ.get("WORKBENCH_CHAT_RUNTIMES", "claude,codex")
        self.runtime_names = tuple(
            runtime_names
            or tuple(
                name.strip()
                for name in configured.split(",")
                if name.strip() in LABELS
            )
        )
        self.timeout = timeout if timeout is not None else float(
            os.environ.get("WORKBENCH_CHAT_TIMEOUT", "240")
        )
        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else load_system_prompt(skills_dir)
        )

    async def send(
        self,
        *,
        state: dict[str, Any],
        text: str,
        cwd: str | None,
        push: Callable[[str], Awaitable[None]],
    ) -> None:
        """并行扇出一条消息;完成即推送。state["chat"] 是持久线程表。"""
        chat_state = state.setdefault("chat", {})
        tasks: dict[asyncio.Task, str] = {}
        for name in self.runtime_names:
            thread = chat_state.setdefault(name, {"session_id": None})
            task = asyncio.create_task(
                self._one(name, thread, text, cwd),
                name=f"chat-{name}",
            )
            tasks[task] = name
        if not tasks:
            await push("[Chat] ⚠️ 没有启用的聊天 runtime")
            return
        state["_chat_tasks"] = set(tasks)
        # 单一 runtime 的独立机器人不带标签（会话本身就是那个机器人）
        tag_prefix = len(self.runtime_names) > 1
        try:
            pending: set[asyncio.Task] = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    name = tasks[task]
                    label = LABELS[name]
                    try:
                        answer = task.result()
                        await push(f"[{label}]\n{answer}" if tag_prefix else answer)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await push(
                            f"[{label}] ⚠️ 本轮未回答: {exc}"
                            if tag_prefix
                            else f"⚠️ 本轮未回答: {exc}"
                        )
        except asyncio.CancelledError:
            # 整轮被取消(agent.cancel):终止全部 runtime 任务并等子进程收尾
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            state.pop("_chat_tasks", None)

    async def cancel(self, state: dict[str, Any]) -> None:
        for task in list(state.get("_chat_tasks") or ()):
            task.cancel()
        # 等待收尾,避免子进程逃逸;CancelledError 在 send 的 wait 循环处收束
        tasks = list(state.get("_chat_tasks") or ())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _one(
        self, name: str, thread: dict[str, Any], text: str, cwd: str | None
    ) -> str:
        if name == "claude":
            answer, session_id = await asyncio.wait_for(
                claude_chat_once(
                    prompt=text,
                    cwd=cwd,
                    session_id=thread.get("session_id"),
                    system_prompt=self.system_prompt,
                    timeout=self.timeout,
                ),
                timeout=self.timeout + 10.0,
            )
            thread["session_id"] = session_id or thread.get("session_id")
            return answer
        if name == "codex":
            answer, session_id = await asyncio.wait_for(
                codex_chat_once(
                    prompt=text,
                    cwd=cwd,
                    session_id=thread.get("session_id"),
                    timeout=self.timeout,
                ),
                timeout=self.timeout + 10.0,
            )
            thread["session_id"] = session_id or thread.get("session_id")
            return answer
        raise ChatRuntimeError(f"未知 runtime: {name}")

"""Chat runtimes:并行共存的会话 agent(Claude / Codex)。

聊天模式与 SOP 的分工:每条 Canvas 消息由 ChatRouter 并行扇出给全部
runtime,各自维护独立线程(续聊上下文),协作机器人答案带 [Claude]/[Codex]
标签回推;单个 runtime 失败只影响自己的标签位,不拖垮另一个。
两个 runtime 都经 cc-switch 网关(127.0.0.1:15721),网关不可用时同时降级。
DSH 已按需求从聊天机器人移除(SOP 流水线的 DSH 执行角色不受影响,
见 workbench/backend/workflow/runner_factory.py)。

复用的 backend 实现已验证可在 ACP venv 导入(tests/adapters 覆盖):
- Claude:claude --print stream-json,解析复用 claude_runner.parse_claude_stream_json
- Codex:codex exec --json(经 CodexSessionWrapper.build_command 组装,isolation
  inherit 保留用户 provider 配置),只收 item.completed 的最终消息
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from workbench.backend.agents.claude_runner import (
    ClaudeRunError,
    parse_claude_stream_json,
)
from workbench.backend.agents.codex_session_wrapper import CodexSessionWrapper

CLAUDE_BIN = os.environ.get("WORKBENCH_CHAT_CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("WORKBENCH_CHAT_CODEX_BIN", "codex")


class ChatRuntimeError(RuntimeError):
    """单个 runtime 的一轮回答失败(不等于整个聊天失败)。"""


# -- Claude -----------------------------------------------------------------


def build_chat_argv(
    *,
    prompt: str,
    session_id: str | None = None,
    system_prompt: str | None = None,
    claude_bin: str = CLAUDE_BIN,
) -> list[str]:
    """聊天模式 argv:无 SOP planner 白名单,只读工具 + 注入回答规范。

    ``--safe-mode --strict-mcp-config`` 挡掉 hooks/MCP/plugins(冒烟验证中
    SessionEnd hook 会把 CLI 挂死);``--tools=Read,Grep,Glob`` 允许读仓库
    回答问题,同时排除一切写操作。
    """
    argv = [
        claude_bin,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--safe-mode",
        "--strict-mcp-config",
        "--tools=Read,Grep,Glob",
    ]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    if session_id:
        argv += ["--resume", session_id]
    argv.append(prompt)
    return argv


def _cli_child_env() -> dict[str, str]:
    env = dict(os.environ)
    # the agent runtime poisons node CLIs with these two vars
    env.pop("NODE_OPTIONS", None)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    return env


async def claude_chat_once(
    *,
    prompt: str,
    cwd: str | None,
    session_id: str | None = None,
    system_prompt: str | None = None,
    timeout: float = 240.0,
    claude_bin: str = CLAUDE_BIN,
) -> tuple[str, str | None]:
    """一轮 Claude 聊天;返回 (回答文本, 新 session_id)。"""
    argv = build_chat_argv(
        prompt=prompt,
        session_id=session_id,
        system_prompt=system_prompt,
        claude_bin=claude_bin,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_cli_child_env(),
        )
    except FileNotFoundError as exc:
        raise ChatRuntimeError(f"claude 二进制不可用: {claude_bin}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ChatRuntimeError(f"claude 超时(>{timeout:g}s)") from exc
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    try:
        result = parse_claude_stream_json(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            returncode=proc.returncode or 0,
        )
    except ClaudeRunError as exc:
        raise ChatRuntimeError(str(exc)) from exc
    if result.is_error:
        raise ChatRuntimeError("claude 本轮报告 is_error,拒绝输出空/错误回答")
    if not result.text.strip():
        raise ChatRuntimeError("claude 未返回文本")
    return result.text, result.session_id


# -- Codex ------------------------------------------------------------------


def _codex_item_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text
    content = item.get("content")
    if isinstance(content, list):
        pieces = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "".join(pieces)
    return ""


async def codex_chat_once(
    *,
    prompt: str,
    cwd: str | None,
    session_id: str | None = None,
    timeout: float = 240.0,
    codex_bin: str = CODEX_BIN,
) -> tuple[str, str | None]:
    """一轮 Codex 聊天;返回 (回答文本, thread/session id)。

    isolation=inherit 保留用户 codex 配置(provider/model),sandbox 只读;
    只取 ``item.completed`` 的 agent_message —— 语义上就是该轮最终答案,
    避开 started/delta 事件的重复文本。
    """
    wrapper = CodexSessionWrapper(
        cwd or ".",
        codex_bin=codex_bin,
        sandbox_mode="read-only",
        isolation_mode="inherit",
    )
    command = wrapper.build_command(prompt, session_id=session_id)
    from cli.runtime_environment import build_cli_environment
    from cli.runtime_registry import RuntimeBackend

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=build_cli_environment(RuntimeBackend.CODEX),
        )
    except FileNotFoundError as exc:
        raise ChatRuntimeError(f"codex 二进制不可用: {codex_bin}") from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ChatRuntimeError(f"codex 超时(>{timeout:g}s)") from exc
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    texts: list[str] = []
    thread_id = session_id
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        for key in ("thread_id", "session_id", "threadId", "sessionId"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                thread_id = value
                break
        if record.get("type") == "error":
            error = record.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            raise ChatRuntimeError(f"codex 错误: {message or 'unknown'}")
        if record.get("type") != "item.completed":
            continue
        item = record.get("item")
        if isinstance(item, dict) and item.get("type") in {"agent_message", "message"}:
            text = _codex_item_text(item)
            if text.strip():
                texts.append(text)
    if not texts:
        raise ChatRuntimeError(f"codex 未返回文本(exit={proc.returncode})")
    return "\n".join(texts), thread_id


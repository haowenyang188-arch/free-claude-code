"""SOP ACP Agent：把 Canvas 的 session/prompt 转成 SOP run，流式回推。

两条路径（prompt 入口路由）：
    /sop <task>  → SopClient: login → register definition → start_run(task)
                   → 轮询 artifacts（去重 by id）→ 每个新 artifact 推送
    其他任意文本 → ChatRouter 并行扇出给 Claude/Codex 两个持久线程，
                   协作机器人答案带 [Claude]/[Codex] 标签陆续推送（真聊天模式）

注意：agent-client-protocol 0.10.1 的 router 以「关键字参数」调用 handler
（字段展开，接口签名见 acp.interfaces.Agent），不是单个 params 对象。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid

from acp import Agent
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
)

# 绝对导入：支持「直接脚本」与「-m」两种运行方式（见 __main__.py 的 sys.path 引导）
from adapters.openhands_acp.display import artifact_to_text
from adapters.openhands_acp.sop_client import SopApiError, SopClient

AGENT_NAME = "sop-workbench-acp"
AGENT_VERSION = "0.2.0"

_LOG_PATH = os.environ.get("SOP_ACP_LOG", "") or "/tmp/sop-acp-agent.log"

_SOP_PREFIX_RE = re.compile(r"^/sop(?:\s+(.+))?$", re.DOTALL)


def split_sop_prefix(text: str) -> str | None:
    """/sop 前缀分流:命中返回 SOP 任务文本(可为空串),未命中返回 None。"""
    match = _SOP_PREFIX_RE.match(text.strip())
    if match is None:
        return None
    return (match.group(1) or "").strip()


def _log(msg: str) -> None:
    """写文件日志（stdout 是协议线，禁止打印）。

    Canvas/agent-server 拉起的子进程通常没设 SOP_ACP_LOG，统一落到
    /tmp/sop-acp-agent.log，否则扇出与推送失败完全不可观测。
    """
    try:
        from time import strftime

        with open(_LOG_PATH, "a") as f:
            f.write(f"[{strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


_ACP_CONTEXT_MARKERS = ("<CUSTOM_SECRETS>", "<REPO_CONTEXT>", "<RUNTIME_SERVICES>")


def _blocks_text(blocks) -> str:
    """ACP prompt 内容块 → 用户任务文本。

    ACPAgent appends its system suffix as another text block.  That suffix is
    context for the ACP subprocess, not part of the user's task and must never
    be echoed into SOP artifacts or Canvas messages.
    """
    parts: list[str] = []
    for block in blocks or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if text:
                parts.append(str(text))
        else:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
    text = "\n".join(parts)
    for marker in _ACP_CONTEXT_MARKERS:
        text = text.split(marker, 1)[0]
    return text.strip()


def _new_session_state() -> dict:
    """会话状态:run_id/seen 服务 SOP;chat/cwd 服务聊天扇出。"""
    return {
        "run_id": None,
        "seen": set(),
        "push_count": 0,
        "chat": {},
        "cwd": None,
    }


def _build_chat_router(runtime_names: tuple[str, ...] | None = None):
    """构建默认 ChatRouter;失败时降级为 None(SOP-only 不受影响)。

    WORKBENCH_CHAT_ENABLED=0 可显式关闭聊天模式(逃生开关)。
    runtime_names 限定绑定的 runtime(None = Claude+Codex 两路)。
    """
    if os.environ.get("WORKBENCH_CHAT_ENABLED", "1").strip().lower() in {
        "0",
        "false",
        "off",
    }:
        return None
    try:
        from adapters.openhands_acp.chat_router import ChatRouter

        return ChatRouter(runtime_names=runtime_names)
    except Exception as exc:  # noqa: BLE001 - ACP 子进程必须能起来
        _log(f"chat router disabled: {exc!r}")
        return None


class SopAcpAgent(Agent):
    """Custom ACP Server 实现：聊天扇出 + SOP Engine 两种路径。

    runtime_names 限定聊天绑定哪些 runtime：None = Claude+Codex 两路扇出（协作机器人）；
    ("claude",) / ("codex",) = 单一 runtime 的独立机器人，回答不带标签。
    """

    def __init__(
        self,
        sop: SopClient | None = None,
        poll_interval: float = 1.0,
        chat_router=None,
        runtime_names: tuple[str, ...] | None = None,
    ) -> None:
        self._conn = None
        self._sessions: dict[
            str, dict
        ] = {}  # acp session_id -> session state
        self._sop = sop or SopClient()
        self._poll_interval = poll_interval
        self._runtime_names = runtime_names
        self._chat = (
            chat_router
            if chat_router is not None
            else _build_chat_router(runtime_names)
        )

    def on_connect(self, conn) -> None:
        self._conn = conn

    # -- protocol handlers（关键字参数形式，0.10.1 router 契约）-------------
    async def initialize(
        self,
        protocol_version: int,
        client_capabilities=None,
        client_info=None,
        **kwargs,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(load_session=True),
            agent_info=Implementation(name=AGENT_NAME, version=AGENT_VERSION),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list | None = None,
        mcp_servers: list | None = None,
        **kwargs,
    ) -> NewSessionResponse:
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = _new_session_state()
        self._sessions[session_id]["cwd"] = cwd
        _log(f"new_session kwargs: {kwargs!r}"[:400])
        return NewSessionResponse(session_id=session_id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list | None = None,
        mcp_servers: list | None = None,
        **kwargs,
    ) -> LoadSessionResponse:
        """Restore the lightweight state needed by a persisted Canvas session.

        ACP subprocesses are short-lived, while agent-server persists the
        session id. Recreate the in-memory entry so the next prompt can use
        the persisted id after a process restart.
        """
        self._sessions.setdefault(session_id, _new_session_state())
        self._sessions[session_id]["cwd"] = cwd
        _log(f"loaded session {session_id[-8:]}")
        return LoadSessionResponse()

    async def prompt(
        self,
        prompt: list,
        session_id: str,
        message_id: str | None = None,
        **kwargs,
    ) -> PromptResponse:
        _log(
            f"prompt called session={session_id[:8]} text={_blocks_text(prompt)[:60]!r}"
        )
        task_text = _blocks_text(prompt).strip()
        if session_id not in self._sessions:
            raise KeyError(f"unknown session: {session_id}")
        state = self._sessions[session_id]
        state["push_count"] = 0

        # -- 路由:/sop 进 SOP Engine,其余全部进聊天扇出 ----------------
        sop_goal = split_sop_prefix(task_text)
        if sop_goal is None:
            return await self._chat_prompt(session_id, state, task_text)
        task_text = sop_goal or "(empty task)"

        # -- 启动 SOP run（Engine 是唯一流程控制者）--------------------
        try:
            await self._run_sync(self._sop.login)
            _log("login ok")
            await self._run_sync(self._sop.register_definition)
            _log("register_definition ok")
            started = await self._run_sync(
                self._sop.start_run,
                task_text,
                metadata={"canvas_acp_session_id": session_id},
            )
            _log("start_run ok: " + str(started)[:120])
        except SopApiError as exc:
            _log("start_run FAIL: " + str(exc))
            await self._push(
                session_id, "[SOP Engine]\nSOP Engine 启动失败: " + str(exc)
            )
            # stop_reason 枚举不含 error（end_turn/max_tokens/max_turn_requests/refusal/cancelled）
            return PromptResponse(stop_reason="end_turn")
        run_id = started.get("sop_run_id") or started.get("id") or ""
        state["run_id"] = run_id
        _log("run started " + run_id + ", pushing")
        await self._push(
            session_id,
            f"[SOP Engine]\nSOP run 已启动: {run_id}\n任务: {task_text[:200]}",
        )
        _log("pushed run-started")

        # -- 轮询 artifacts 并流式回推 --------------------------------
        loops_left = 900  # ~15min cap
        last_status = "running"
        try:
            while loops_left > 0:
                loops_left -= 1
                status = "running"
                try:
                    run = await self._run_sync(self._sop.get_run, run_id)
                    status = run.get("status", "running")
                except SopApiError:
                    pass
                try:
                    artifacts = await self._run_sync(self._sop.get_artifacts, run_id)
                    for artifact in artifacts:
                        aid = artifact.get("id", "")
                        if aid in state["seen"]:
                            continue
                        state["seen"].add(aid)
                        try:
                            content = await self._run_sync(
                                self._sop.get_artifact_content, aid
                            )
                        except SopApiError:
                            content = artifact.get("summary") or ""
                        text = artifact_to_text(
                            artifact.get("type"), artifact.get("role_id"), content
                        )
                        await self._push(session_id, text)
                except SopApiError:
                    pass
                if status in ("completed", "failed", "cancelled", "paused"):
                    last_status = status
                    break
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            await self._run_sync(self._sop.cancel_run, run_id)
            raise

        # -- 终态映射 ---------------------------------------------------
        if last_status == "completed":
            _log("terminal completed")
            await self._push(session_id, "[SOP Engine]\n✅ SOP run 完成（completed）。")
            return PromptResponse(stop_reason="end_turn")
        _log(f"terminal {last_status}")
        await self._push(
            session_id,
            f"[SOP Engine]\n⚠️ SOP run 结束（{last_status}）。",
        )
        # stop_reason 枚举不含 error（end_turn/max_tokens/max_turn_requests/refusal/cancelled）
        return PromptResponse(stop_reason="end_turn")

    async def _chat_prompt(
        self, session_id: str, state: dict, text: str
    ) -> PromptResponse:
        """聊天路径:各 runtime 并行回答,完成即推送(部分降级)。"""
        if self._chat is None:
            await self._push(
                session_id,
                "[Chat] ⚠️ 聊天模式未启用(WORKBENCH_CHAT_ENABLED=0 或路由构建失败)。"
                "代码任务请用 /sop <任务>。",
            )
            return PromptResponse(stop_reason="end_turn")
        if not text:
            text = "(empty message)"
        _log(f"chat fanout session={session_id[:8]} text={text[:60]!r}")
        try:
            await self._chat.send(
                state=state,
                text=text,
                cwd=state.get("cwd"),
                push=lambda part: self._push(session_id, part),
            )
        except asyncio.CancelledError:
            await self._chat.cancel(state)
            raise
        finally:
            # 回合结束(会话空闲)后统一落盘:回合中 POST 事件会被
            # agent-server 以 500 拒绝,空闲时才接受。
            buffer = state.pop("_persist_buffer", None) or []
            for item in buffer:
                self._persist_best_effort(session_id, item)
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **kwargs) -> None:
        state = self._sessions.get(session_id)
        if state and state.get("run_id"):
            await self._run_sync(self._sop.cancel_run, state["run_id"])
        if state and self._chat is not None:
            await self._chat.cancel(state)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    async def _run_sync(fn, *args, **kwargs):
        """同步 HTTP 调用放线程池，避免阻塞 asyncio loop（否则推送/响应死锁）。"""
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _resolve_conversation_id(self, acp_session_id: str) -> str | None:
        """本地反查:dev_conversations/*/base_state.json 记录了 ACP session id,
        目录名即 conversation id。命中后缓存进会话状态。"""
        state = self._sessions.get(acp_session_id)
        if state is not None and state.get("conversation_id"):
            return state["conversation_id"]
        import glob as _glob

        root = os.path.expanduser("~/.openhands/agent-canvas/dev_conversations")
        try:
            for path in _glob.glob(os.path.join(root, "*", "base_state.json")):
                with open(path, encoding="utf-8") as f:
                    if acp_session_id in f.read():
                        conv_id = os.path.basename(os.path.dirname(path))
                        if state is not None:
                            state["conversation_id"] = conv_id
                        _log(f"conversation id resolved: {conv_id[:8]}")
                        return conv_id
        except Exception as exc:  # noqa: BLE001
            _log(f"conversation lookup failed: {exc!r}")
        return None

    def _persist_best_effort(self, session_id: str, text: str) -> None:
        """[实验已暂停]把回复落为 agent-server 会话事件。

        失败仅记日志,绝不影响实时推送;EXPERIMENT_PERSIST=0 可关闭。
        """
        # 结论(2026-09-06 实验,四轮):
        #  1) ID 映射已解:base_state.json 记录 ACP session id,可本地反查
        #     conversation id(_resolve_conversation_id)
        #  2) 但 agent-server 的 POST /api/conversations/<id>/events 只接受
        #     user 消息("Only user messages are allowed"),assistant 回复
        #     422/500 —— 该端点是"给 agent 发话"入口,非事件追加 API
        # 持久化需:上游内核持久化 ACP session_update,或 Workbench 自建
        # 存储叠加展示。实验默认关闭,保留代码备查。
        if os.environ.get("EXPERIMENT_PERSIST", "1").strip().lower() not in {
            "1", "true", "on"
        } or os.environ.get("PYTEST_CURRENT_TEST"):
            return
        try:
            import json as _json
            import threading
            import urllib.request

            key_path = os.path.expanduser(
                "~/.openhands/agent-canvas/api-key.txt"
            )
            with open(key_path, encoding="utf-8") as f:
                key = f.read().strip()
            base = os.environ.get(
                "OH_AGENT_SERVER_BASE", "http://127.0.0.1:18000"
            ).rstrip("/")
            conv_id = self._resolve_conversation_id(session_id)
            if not conv_id:
                _log("persist skipped: conversation id unresolved")
                return
            body = _json.dumps(
                {
                    # agent-server role 枚举: user/assistant/system/tool
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "run": False,
                }
            ).encode()

            def _post():
                try:
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({})
                    )
                    req = urllib.request.Request(
                        f"{base}/api/conversations/{conv_id}/events",
                        data=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-Session-API-Key": key,
                        },
                        method="POST",
                    )
                    with opener.open(req, timeout=8) as resp:
                        _log(f"persisted event status={resp.status}")
                except Exception as exc:  # noqa: BLE001
                    _log(f"persist POST failed: {exc!r}")

            threading.Thread(target=_post, daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            _log(f"persist best-effort failed: {exc!r}")

    async def _push(self, session_id: str, text: str) -> None:
        """推送一条 agent_message（session/update 通知）。

        AgentSideConnection.session_update 期望 update 是 AgentMessageChunk
        （内部自己包 SessionNotification），不要手动包 session_notification。
        """
        if self._conn is None:
            return
        try:
            state = self._sessions.get(session_id)
            if state is not None and state.get("push_count", 0):
                text = f"\n\n{text}"
            if state is not None:
                state["push_count"] = state.get("push_count", 0) + 1
            from acp import update_agent_message_text

            chunk = update_agent_message_text(text)
            await self._conn.session_update(session_id, chunk)
            _log("pushed msg len=" + str(len(text)))
            state = self._sessions.get(session_id)
            if state is not None:
                state.setdefault("_persist_buffer", []).append(text)
        except Exception as exc:
            import traceback

            traceback.print_exc(file=sys.stderr)
            _log(f"push FAIL len={len(text)}: {exc!r}")

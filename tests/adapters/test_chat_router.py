"""ChatRouter 并行扇出 / 降级 / 标签推送 / 线程状态 的单元测试。

全部用 fake runtime,不触碰真实 CLI 与 Desktop。
"""

import asyncio

import pytest

from adapters.openhands_acp.chat_router import (
    LABELS,
    ChatRouter,
    load_system_prompt,
)
from adapters.openhands_acp.chat_runtimes import ChatRuntimeError
from adapters.openhands_acp.sop_acp_agent import SopAcpAgent, split_sop_prefix


class RecordingPush:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, text: str) -> None:
        self.texts.append(text)


def make_router(answers: dict[str, object], **kwargs) -> ChatRouter:
    """answers: runtime 名 -> 文本 / Exception / asyncio.TimeoutError。

    通过 monkeypatch ChatRouter._one 注入 fake,保持 send/_one 生产逻辑不变。
    """

    async def fake_one(self, name, thread, text, cwd):
        thread["last_prompt"] = text
        answer = answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer

    router = ChatRouter(
        runtime_names=("claude", "codex"), system_prompt="sys", **kwargs
    )
    type(router)._one = fake_one
    return router


def test_split_sop_prefix_routing() -> None:
    assert split_sop_prefix("/sop 实现登录") == "实现登录"
    assert split_sop_prefix("/sop") == ""
    assert split_sop_prefix("/sop\n多行\n任务") == "多行\n任务"
    assert split_sop_prefix("聊聊 /sop 的设计") is None
    assert split_sop_prefix("/sopfoo") is None
    assert split_sop_prefix("你好") is None


@pytest.mark.asyncio
async def test_fanout_pushes_tagged_answers() -> None:
    router = make_router({"claude": "A1", "codex": "B1"})
    push = RecordingPush()
    state: dict = {}

    await router.send(state=state, text="你好", cwd="/tmp", push=push)

    pushed = "\n".join(push.texts)
    assert "[Claude]\nA1" in pushed
    assert "[Codex]\nB1" in pushed
    # 两个持久线程都收到同一条消息
    chat_state = state["chat"]
    assert chat_state["claude"]["last_prompt"] == "你好"
    assert chat_state["codex"]["last_prompt"] == "你好"
    assert state.get("_chat_tasks") is None  # 收尾清理


@pytest.mark.asyncio
async def test_partial_degradation_one_runtime_fails() -> None:
    router = make_router(
        {
            "claude": "A1",
            "codex": ChatRuntimeError("上游 502"),
        }
    )
    push = RecordingPush()
    state: dict = {}

    await router.send(state=state, text="你好", cwd=None, push=push)

    pushed = "\n".join(push.texts)
    assert "[Claude]\nA1" in pushed
    assert "[Codex] ⚠️ 本轮未回答" in pushed and "502" in pushed


@pytest.mark.asyncio
async def test_thread_session_ids_persist_across_turns() -> None:
    calls: dict[str, list] = {"claude": [], "codex": []}

    async def fake_one(self, name, thread, text, cwd):
        calls[name].append(thread.get("session_id"))
        thread["session_id"] = f"{name}-thread-1"
        return f"{LABELS[name]} ok"

    router = ChatRouter(
        runtime_names=("claude", "codex"), system_prompt="sys"
    )
    type(router)._one = fake_one
    push = RecordingPush()
    state: dict = {}

    await router.send(state=state, text="第一句", cwd="/tmp", push=push)
    await router.send(state=state, text="第二句", cwd="/tmp", push=push)

    # 第一轮线程 id 为空(新线程),第二轮拿到第一轮建立的持久线程
    for name in ("claude", "codex"):
        assert calls[name] == [None, f"{name}-thread-1"]


@pytest.mark.asyncio
async def test_cancel_terminates_pending_runtime_tasks() -> None:
    started = asyncio.Event()

    async def hang_one(self, name, thread, text, cwd):
        started.set()
        await asyncio.sleep(60)

    router = make_router({})
    type(router)._one = hang_one
    push = RecordingPush()
    state: dict = {}

    send_task = asyncio.create_task(
        router.send(state=state, text="你好", cwd=None, push=push)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0.05)

    await router.cancel(state)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(send_task, timeout=2)
    assert state.get("_chat_tasks") is None


@pytest.mark.asyncio
async def test_agent_routes_plain_text_to_chat_and_sop_prefix_to_engine() -> None:
    class FakeChat:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, *, state, text, cwd, push):
            self.sent.append(text)
            await push(f"[Claude]\nchat:{text}")

        async def cancel(self, state) -> None:
            return None

    class FakeSop:
        def login(self) -> None:
            pass

        def register_definition(self) -> None:
            pass

        def start_run(self, goal, *, metadata=None):
            return {"sop_run_id": "run-x"}

        def get_run(self, _run_id):
            return {"status": "completed"}

        def get_artifacts(self, _run_id):
            return []

    chat = FakeChat()
    agent = SopAcpAgent(sop=FakeSop(), chat_router=chat)
    connection_updates: list = []

    class Connection:
        async def session_update(self, _session_id, update) -> None:
            connection_updates.append(update)

    agent.on_connect(Connection())
    session = await agent.new_session(cwd="/tmp")

    await agent.prompt(
        prompt=[{"text": "帮我看看这个报错"}], session_id=session.session_id
    )
    assert chat.sent == ["帮我看看这个报错"]
    assert "[Claude]" in connection_updates[-1].content.text

    connection_updates.clear()
    await agent.prompt(
        prompt=[{"text": "/sop 实现登录"}], session_id=session.session_id
    )
    assert chat.sent == ["帮我看看这个报错"]  # SOP 消息不进聊天
    assert "run-x" in connection_updates[0].content.text


def test_load_system_prompt_joins_skill_files(tmp_path) -> None:
    (tmp_path / "a.md").write_text("规范A", encoding="utf-8")
    (tmp_path / "b.md").write_text("规范B", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("不收", encoding="utf-8")

    prompt = load_system_prompt(tmp_path)
    assert prompt == "规范A\n\n---\n\n规范B"
    assert load_system_prompt(tmp_path / "missing") == ""


# -- 独立机器人(单 runtime,无标签)---------------------------------------------


@pytest.mark.asyncio
async def test_single_runtime_answer_has_no_tag_prefix() -> None:
    router = ChatRouter(runtime_names=("claude",), system_prompt="sys")

    async def fake_one_impl(self, name, thread, text, cwd):
        thread["last_prompt"] = text
        return "单机器人回答"

    type(router)._one = fake_one_impl  # type: ignore[attr-defined]
    push = RecordingPush()
    state: dict = {}

    await router.send(state=state, text="你好", cwd=None, push=push)

    assert push.texts == ["单机器人回答"]


@pytest.mark.asyncio
async def test_single_runtime_error_has_no_tag_prefix() -> None:
    router = ChatRouter(runtime_names=("codex",), system_prompt="sys")
    push = RecordingPush()
    state: dict = {}

    async def fake_fail(self, name, thread, text, cwd):
        raise ChatRuntimeError("上游 502")

    type(router)._one = fake_fail  # type: ignore[attr-defined]
    await router.send(state=state, text="你好", cwd=None, push=push)

    assert push.texts == ["⚠️ 本轮未回答: 上游 502"]


def test_parse_args_runtime_binding() -> None:
    from adapters.openhands_acp.__main__ import parse_args

    assert parse_args([]).runtime == "all"
    assert parse_args(["--runtime", "claude"]).runtime == "claude"
    assert parse_args(["--runtime", "codex"]).runtime == "codex"
    # dsh 已移除:不再接受
    with pytest.raises(SystemExit):
        parse_args(["--runtime", "dsh"])


def test_agent_builds_router_bound_to_single_runtime() -> None:
    from adapters.openhands_acp.sop_acp_agent import _build_chat_router

    router = _build_chat_router(("codex",))
    assert router is not None
    assert router.runtime_names == ("codex",)

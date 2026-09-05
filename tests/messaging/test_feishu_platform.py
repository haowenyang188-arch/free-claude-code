"""FeishuPlatform 单元测试:解析 / @门控 / 白名单 / 去重 / 发送 / 工厂。

不连真实飞书:事件用 SimpleNamespace 按 lark-oapi 的对象形状伪造。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from messaging.models import IncomingMessage
from messaging.platforms.feishu import (
    FeishuPlatform,
    extract_message_text,
    strip_mention_placeholders,
)


def make_mention(key: str, open_id: str, name: str = "某用户"):
    return SimpleNamespace(key=key, name=name, mentioned_type="user",
                           id=SimpleNamespace(open_id=open_id))


def make_event(
    text: str = "你好",
    *,
    chat_type: str = "p2p",
    chat_id: str = "oc_chat1",
    sender_open_id: str = "ou_user1",
    message_type: str = "text",
    mentions: list | None = None,
    event_id: str = "evt-1",
    parent_id: str | None = None,
):
    message = SimpleNamespace(
        message_id="om_1",
        root_id=None,
        parent_id=parent_id,
        chat_id=chat_id,
        chat_type=chat_type,
        message_type=message_type,
        content=json.dumps({"text": text}) if message_type == "text" else json.dumps({}),
        mentions=mentions or [],
        create_time="1",
    )
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id=sender_open_id))
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(message=message, sender=sender),
    )


def make_platform(**kwargs) -> FeishuPlatform:
    return FeishuPlatform(
        app_id="cli_test",
        app_secret="secret",
        **kwargs,
    )


# -- 纯函数 -------------------------------------------------------------------


def test_extract_text_message() -> None:
    assert extract_message_text("text", json.dumps({"text": "  你好  "})) == "  你好  "
    assert extract_message_text("image", "{}") == ""
    assert extract_message_text("text", "not-json") == ""


def test_extract_post_message_flattens_elements() -> None:
    content = {
        "title": "周报",
        "content": [
            [{"tag": "text", "text": "第一段"}, {"tag": "a", "text": "链接", "href": "http://x"}],
            [{"tag": "text", "text": "第二段"}],
        ],
    }
    text = extract_message_text("post", json.dumps(content))
    assert text == "周报\n第一段链接\n第二段"


def test_strip_mention_placeholders() -> None:
    mentions = [make_mention("@_user_1", "ou_bot")]
    assert strip_mention_placeholders("@_user_1 帮我查一下", mentions) == "帮我查一下"
    assert strip_mention_placeholders("无提及", []) == "无提及"


# -- 事件门控 -------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []

    async def __call__(self, message: IncomingMessage) -> None:
        self.messages.append(message)


def wire(platform: FeishuPlatform, recorder: Recorder) -> None:
    platform._loop = asyncio.get_running_loop()
    platform.on_message(recorder)


@pytest.mark.asyncio
async def test_p2p_message_dispatches_to_handler() -> None:
    platform = make_platform(require_mention=True)
    recorder = Recorder()
    wire(platform, recorder)

    platform._on_event(make_event(text="你好"))

    await asyncio.sleep(0.05)
    assert [m.text for m in recorder.messages] == ["你好"]
    assert recorder.messages[0].platform == "feishu"
    assert recorder.messages[0].chat_id == "oc_chat1"


@pytest.mark.asyncio
async def test_group_message_requires_mention_by_default() -> None:
    platform = make_platform()
    recorder = Recorder()
    wire(platform, recorder)

    # 无 @:丢弃
    platform._on_event(make_event(text="大家好", chat_type="group"))
    await asyncio.sleep(0.05)
    assert recorder.messages == []

    # @机器人(open_id 匹配):放行,并剥掉占位符
    platform.bot_open_id = "ou_bot"
    platform._on_event(
        make_event(
            text="@_user_1 帮我查进度",
            chat_type="group",
            mentions=[make_mention("@_user_1", "ou_bot")],
            event_id="evt-mention",
        )
    )
    await asyncio.sleep(0.05)
    assert [m.text for m in recorder.messages] == ["帮我查进度"]


@pytest.mark.asyncio
async def test_chat_allowlist_blocks_unknown_chat() -> None:
    platform = make_platform(allowed_chat_ids="oc_allowed")
    recorder = Recorder()
    wire(platform, recorder)

    platform._on_event(make_event(text="你好", chat_id="oc_other"))
    await asyncio.sleep(0.05)
    assert recorder.messages == []

    platform._on_event(make_event(text="你好", chat_id="oc_allowed", event_id="evt-allowed"))
    await asyncio.sleep(0.05)
    assert len(recorder.messages) == 1


@pytest.mark.asyncio
async def test_open_id_allowlist_blocks_unknown_user() -> None:
    platform = make_platform(allowed_open_ids="ou_boss")
    recorder = Recorder()
    wire(platform, recorder)

    platform._on_event(make_event(text="你好", sender_open_id="ou_stranger"))
    platform._on_event(make_event(text="你好", sender_open_id="ou_boss", event_id="evt-2"))
    await asyncio.sleep(0.05)
    assert [m.user_id for m in recorder.messages] == ["ou_boss"]


@pytest.mark.asyncio
async def test_duplicate_event_is_delivered_once() -> None:
    platform = make_platform()
    recorder = Recorder()
    wire(platform, recorder)

    platform._on_event(make_event(text="你好", event_id="evt-dup"))
    platform._on_event(make_event(text="你好", event_id="evt-dup"))
    await asyncio.sleep(0.05)
    assert len(recorder.messages) == 1


@pytest.mark.asyncio
async def test_unsupported_message_type_is_skipped() -> None:
    platform = make_platform()
    recorder = Recorder()
    wire(platform, recorder)

    platform._on_event(make_event(text="", message_type="image", event_id="evt-img"))
    await asyncio.sleep(0.05)
    assert recorder.messages == []


# -- 发送 ---------------------------------------------------------------------


class FakeMessageApi:
    def __init__(self) -> None:
        self.last_request: object | None = None
        self.next_id = "om_new"

    def create(self, request):
        self.last_request = request
        return SimpleNamespace(
            success=lambda: True, code=0, msg="ok", data=SimpleNamespace(message_id=self.next_id)
        )

    def reply(self, request):
        self.last_request = request
        return SimpleNamespace(
            success=lambda: True, code=0, msg="ok", data=SimpleNamespace(message_id=self.next_id)
        )


@pytest.mark.asyncio
async def test_send_message_uses_create_with_chat_id() -> None:
    platform = make_platform()
    fake = FakeMessageApi()
    platform._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=fake)))

    message_id = await platform.send_message("oc_chat1", "回复内容")

    assert message_id == "om_new"
    body = fake.last_request
    assert body is not None


@pytest.mark.asyncio
async def test_send_message_reply_path() -> None:
    platform = make_platform()
    fake = FakeMessageApi()
    platform._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=fake)))

    await platform.send_message("oc_chat1", "回复", reply_to="om_1")

    assert fake.last_request is not None  # reply API 被调用


# -- 工厂 ---------------------------------------------------------------------


def test_factory_skips_feishu_without_credentials() -> None:
    from messaging.platforms.factory import create_messaging_platform

    assert create_messaging_platform("feishu") is None
    assert create_messaging_platform("feishu", feishu_app_id="cli_x") is None


def test_factory_builds_feishu_platform_with_credentials() -> None:
    from messaging.platforms.factory import create_messaging_platform

    platform = create_messaging_platform(
        "feishu",
        feishu_app_id="cli_x",
        feishu_app_secret="s3cret",
        allowed_feishu_open_ids="ou_a,ou_b",
    )
    assert isinstance(platform, FeishuPlatform)
    assert platform.allowed_open_ids == {"ou_a", "ou_b"}
    assert platform.require_mention is True

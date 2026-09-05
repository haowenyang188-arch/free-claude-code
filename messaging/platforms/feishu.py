"""Feishu (Lark) Platform Adapter.

Implements MessagingPlatform for Feishu via the official ``lark-oapi`` SDK in
WebSocket long-connection mode — no public callback URL is required, which is
what makes it suitable for a WSL/local deployment.

Feishu Open Platform setup (one-time, per app):
1. 企业自建应用 → 添加「机器人」能力。
2. 申请 ``im:message`` 读写权限（接收 + 发送消息）。
3. 事件与回调 → 选择「长连接」模式 → 订阅 ``im.message.receive_v1``。
4. 发布版本并通过审核。

Env config:
    MESSAGING_PLATFORM=feishu
    FEISHU_APP_ID / FEISHU_APP_SECRET        必填
    ALLOWED_FEISHU_OPEN_IDS / ALLOWED_FEISHU_CHAT_IDS   可选白名单（逗号分隔）
    FEISHU_REQUIRE_MENTION                   群聊是否必须 @机器人（默认开）
    FEISHU_BOT_OPEN_ID                       可选，群聊 @ 判定更精确

Known v1 limitations:
- 纯文本消息不支持编辑（飞书 API 只允许改卡片/富文本），``edit_message`` 是
  有意设计的 no-op——进度类消息在飞书里不会原地刷新，最终回复以新消息送达。
- 语音消息暂不转写，直接忽略。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from ..models import IncomingMessage
from .base import MessagingPlatform

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        DeleteMessageRequest,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    LARK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra
    LARK_AVAILABLE = False

MAX_TEXT_CHARS = 10000


def _parse_ids(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def extract_message_text(message_type: str, content_json: str) -> str:
    """Flatten a Feishu im message body into plain text.

    Supports ``text`` and ``post`` (rich text); other message types return ""
    so the caller can skip them.
    """
    try:
        content = json.loads(content_json or "{}")
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(content, dict):
        return ""
    if message_type == "text":
        return str(content.get("text") or "")
    if message_type == "post":
        parts: list[str] = []
        title = content.get("title")
        if title:
            parts.append(str(title))
        for block in content.get("content") or []:
            if not isinstance(block, list):
                continue
            line = ""
            for element in block:
                if not isinstance(element, dict):
                    continue
                tag = element.get("tag")
                if tag == "text":
                    line += str(element.get("text") or "")
                elif tag == "a":
                    line += str(element.get("text") or element.get("href") or "")
            if line.strip():
                parts.append(line.strip())
        return "\n".join(parts)
    return ""


def strip_mention_placeholders(
    text: str, mentions: list[Any]
) -> str:
    """Remove ``@_user_N`` mention placeholders left inside text content."""
    cleaned = text
    for mention in mentions or []:
        key = getattr(mention, "key", None)
        if key:
            cleaned = cleaned.replace(key, "")
    return cleaned.strip()


class FeishuPlatform(MessagingPlatform):
    """Feishu adapter over lark-oapi WebSocket long connection."""

    name = "feishu"

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        allowed_open_ids: str | None = None,
        allowed_chat_ids: str | None = None,
        require_mention: bool = True,
        bot_open_id: str | None = None,
    ):
        if not LARK_AVAILABLE:
            raise ImportError(
                "lark-oapi is required. Install with: pip install lark-oapi"
            )

        self.app_id = app_id or os.getenv("FEISHU_APP_ID")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET")
        self.allowed_open_ids = _parse_ids(
            allowed_open_ids or os.getenv("ALLOWED_FEISHU_OPEN_IDS")
        )
        self.allowed_chat_ids = _parse_ids(
            allowed_chat_ids or os.getenv("ALLOWED_FEISHU_CHAT_IDS")
        )
        raw_require_mention = os.getenv("FEISHU_REQUIRE_MENTION")
        if raw_require_mention is not None:
            self.require_mention = (
                raw_require_mention.strip().lower() not in {"0", "false", "off"}
            )
        else:
            self.require_mention = require_mention
        self.bot_open_id = bot_open_id or os.getenv("FEISHU_BOT_OPEN_ID")

        if not self.app_id or not self.app_secret:
            logger.warning("FEISHU_APP_ID / FEISHU_APP_SECRET not set")

        self._client: Any | None = None
        self._ws_client: Any | None = None
        self._ws_thread: threading.Thread | None = None
        self._message_handler: Callable[[IncomingMessage], Awaitable[None]] | None = None
        self._connected = False
        self._limiter: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seen_event_ids: deque[str] = deque(maxlen=256)
        self._seen_lock = threading.Lock()

    # -- event intake (runs on the lark ws thread) ----------------------------

    def _remember_event(self, event_id: str) -> bool:
        """Redelivery guard. Returns True when the event is new."""
        if not event_id:
            return True
        with self._seen_lock:
            if event_id in self._seen_event_ids:
                return False
            self._seen_event_ids.append(event_id)
            return True

    def _mentioned_bot(self, mentions: list[Any]) -> bool:
        if not mentions:
            return False
        if self.bot_open_id:
            return any(
                getattr(getattr(m, "id", None), "open_id", None) == self.bot_open_id
                for m in mentions
            )
        # 没有 bot open_id 时的宽松回退：任何 @ 都算（配合聊天白名单使用）
        return True

    def _on_event(self, data: Any) -> None:
        """lark ws 回调（同步、非 asyncio 线程）：解析 → 过滤 → 桥接主循环。"""
        try:
            event = getattr(data, "event", None)
            message = getattr(event, "message", None)
            if message is None:
                return
            header = getattr(data, "header", None)
            event_id = getattr(header, "event_id", "") or ""
            if not self._remember_event(event_id):
                logger.debug("Feishu event redelivered, skipped: {}", event_id)
                return
            self._connected = True

            message_type = getattr(message, "message_type", "") or ""
            if message_type not in {"text", "post"}:
                logger.info(
                    "Feishu message type not supported yet: {} (message skipped)",
                    message_type,
                )
                return

            chat_id = getattr(message, "chat_id", "") or ""
            chat_type = getattr(message, "chat_type", "") or "p2p"
            mentions = list(getattr(message, "mentions", None) or [])
            sender = getattr(event, "sender", None)
            sender_id = (
                getattr(getattr(sender, "sender_id", None), "open_id", None) or ""
            )

            if chat_type == "group" and self.require_mention:
                if not self._mentioned_bot(mentions):
                    return

            if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
                logger.debug("Feishu chat {} not allowlisted, skipped", chat_id)
                return
            if self.allowed_open_ids and sender_id not in self.allowed_open_ids:
                logger.debug("Feishu user {} not allowlisted, skipped", sender_id)
                return

            text = strip_mention_placeholders(
                extract_message_text(message_type, getattr(message, "content", "") or ""),
                mentions,
            )
            if not text.strip():
                return

            incoming = IncomingMessage(
                text=text,
                chat_id=chat_id,
                user_id=sender_id,
                message_id=getattr(message, "message_id", "") or "",
                platform="feishu",
                reply_to_message_id=getattr(message, "parent_id", None) or None,
                raw_event=message,
            )
            self._dispatch_threadsafe(incoming)
        except Exception as exc:  # never let the ws thread die
            logger.error("Feishu event handling failed: {}", exc)

    def _dispatch_threadsafe(self, incoming: IncomingMessage) -> None:
        if self._loop is None or self._message_handler is None:
            logger.warning("Feishu message arrived before start() finished; dropped")
            return
        asyncio.run_coroutine_threadsafe(self._safe_handle(incoming), self._loop)

    async def _safe_handle(self, incoming: IncomingMessage) -> None:
        if self._message_handler is None:
            return
        try:
            await self._message_handler(incoming)
        except Exception as exc:
            logger.error("Feishu message handler failed: {}", exc)

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Initialize and connect to Feishu (ws long connection)."""
        if not self.app_id or not self.app_secret:
            raise ValueError("FEISHU_APP_ID / FEISHU_APP_SECRET are required")

        from ..limiter import MessagingRateLimiter

        self._limiter = await MessagingRateLimiter.get_instance()
        self._loop = asyncio.get_running_loop()

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_event)
            .build()
        )
        self._client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )
        self._ws_thread = threading.Thread(
            target=self._ws_client.start,
            name="feishu-ws-client",
            daemon=True,
        )
        self._ws_thread.start()
        self._connected = True
        logger.info("Feishu platform started (ws long connection)")

    async def stop(self) -> None:
        """Stop the bot. lark ws client has no public close(); the daemon
        thread exits with the process."""
        self._connected = False
        logger.info("Feishu platform stopped")

    # -- outbound --------------------------------------------------------------

    def _truncate(self, text: str) -> str:
        return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS]

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        parse_mode: str | None = None,
        message_thread_id: str | None = None,
    ) -> str:
        if self._client is None:
            raise RuntimeError("Feishu platform not started")
        return await asyncio.to_thread(self._send_sync, chat_id, text, reply_to)

    def _send_sync(self, chat_id: str, text: str, reply_to: str | None) -> str:
        text = self._truncate(text)
        body_content = json.dumps({"text": text})
        if reply_to:
            request = (
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(body_content)
                    .msg_type("text")
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.reply(request)
        else:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .content(body_content)
                    .msg_type("text")
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu send failed: code={response.code} msg={response.msg}"
            )
        return response.data.message_id

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        parse_mode: str | None = None,
    ) -> None:
        """Feishu 纯文本消息不可编辑（API 仅支持卡片/富文本），有意 no-op。"""
        logger.debug("Feishu edit_message is a no-op (text messages are immutable)")

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        if self._client is None:
            return
        try:
            request = DeleteMessageRequest.builder().message_id(message_id).build()
            response = await asyncio.to_thread(
                self._client.im.v1.message.delete, request
            )
            if not response.success():
                logger.warning(
                    "Feishu delete failed: code={} msg={}", response.code, response.msg
                )
        except Exception as exc:
            logger.warning("Feishu delete raised: {}", exc)

    # -- rate-limited queues -----------------------------------------------------

    async def queue_send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        parse_mode: str | None = None,
        fire_and_forget: bool = True,
        message_thread_id: str | None = None,
    ) -> str | None:
        """Enqueue a message to be sent."""
        if not self._limiter:
            return await self.send_message(
                chat_id, text, reply_to, parse_mode, message_thread_id
            )

        async def _send():
            return await self.send_message(
                chat_id, text, reply_to, parse_mode, message_thread_id
            )

        if fire_and_forget:
            self._limiter.fire_and_forget(_send)
            return None
        return await self._limiter.enqueue(_send)

    async def queue_edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        parse_mode: str | None = None,
        fire_and_forget: bool = True,
    ) -> None:
        """Enqueue a message edit (no-op on Feishu text messages)."""
        await self.edit_message(chat_id, message_id, text, parse_mode)

    async def queue_delete_message(
        self,
        chat_id: str,
        message_id: str,
        fire_and_forget: bool = True,
    ) -> None:
        """Enqueue a message deletion."""
        if not self._limiter:
            await self.delete_message(chat_id, message_id)
            return

        async def _delete():
            await self.delete_message(chat_id, message_id)

        dedup_key = f"del:{chat_id}:{message_id}"
        if fire_and_forget:
            self._limiter.fire_and_forget(_delete, dedup_key=dedup_key)
        else:
            await self._limiter.enqueue(_delete, dedup_key=dedup_key)

    # -- handler registration ----------------------------------------------------

    def on_message(
        self,
        handler: Callable[[IncomingMessage], Awaitable[None]],
    ) -> None:
        """Register a message handler callback."""
        self._message_handler = handler

    def fire_and_forget(self, task: Awaitable[Any]) -> None:
        """Execute a coroutine without awaiting it."""
        asyncio.get_running_loop().create_task(task)

    @property
    def is_connected(self) -> bool:
        return self._connected

"""MiniMax provider implementation using Anthropic-compatible API."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from loguru import logger

from providers.base import BaseProvider, ProviderConfig
from providers.common import SSEBuilder, get_user_facing_error_message, map_error
from providers.common.message_converter import (
    normalize_messages,
    summarize_messages,
)
from providers.rate_limit import GlobalRateLimiter

MINIMAX_BASE_URL = "https://api.minimaxi.com/anthropic"


class MiniMaxProvider(BaseProvider):
    """MiniMax provider using Anthropic-compatible endpoint."""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self._provider_name = "MINIMAX"
        self._base_url = (config.base_url or MINIMAX_BASE_URL).rstrip("/")
        self._api_key = config.api_key

        self._global_rate_limiter = GlobalRateLimiter.get_instance(
            rate_limit=config.rate_limit,
            rate_window=config.rate_window,
            max_concurrency=config.max_concurrency,
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            proxy=config.proxy or None,
            trust_env=False,
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
        )

    async def cleanup(self) -> None:
        """Release HTTP client resources."""
        await self._client.aclose()

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream response via MiniMax's Anthropic-compatible endpoint."""
        tag = self._provider_name
        req_tag = f" request_id={request_id}" if request_id else ""
        thinking_enabled = self._is_thinking_enabled(request)

        body = request.model_dump(exclude_none=True)

        # Normalize message content blocks to strings before sending to backend
        body["messages"] = normalize_messages(body.get("messages", []))
        logger.debug("{}_NORMALIZED_MESSAGES: {}", tag, summarize_messages(body["messages"]))

        body.pop("extra_body", None)
        body.pop("original_model", None)
        body.pop("resolved_provider_model", None)

        if "thinking" in body:
            thinking_cfg = body.pop("thinking")
            if (
                thinking_enabled
                and isinstance(thinking_cfg, dict)
                and thinking_cfg.get("enabled")
            ):
                body["thinking"] = {"type": "enabled"}

        if "max_tokens" not in body:
            body["max_tokens"] = 81920

        # Use upstream_model (pure model name) for MiniMax API, not the full provider/model string
        upstream = request.upstream_model or body.get("model")
        body["model"] = upstream
        logger.info(
            "{}_STREAM:{} model={} upstream={} msgs={} tools={}",
            tag,
            req_tag,
            body.get("model"),
            upstream,
            len(body.get("messages", [])),
            len(body.get("tools", [])),
        )

        async with self._global_rate_limiter.concurrency_slot():
            try:
                async def _make_request():
                    request_obj = self._client.build_request(
                        "POST",
                        "/v1/messages",
                        json=body,
                        headers={
                            "Content-Type": "application/json",
                            "x-api-key": self._api_key,
                        },
                    )
                    return await self._client.send(request_obj, stream=True)

                response = await self._global_rate_limiter.execute_with_retry(
                    _make_request
                )

                if response.status_code != 200:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        text = await response.aread()
                        logger.error(
                            "{}_ERROR:{} HTTP {}: {}",
                            tag,
                            req_tag,
                            response.status_code,
                            text.decode("utf-8", errors="replace"),
                        )
                        raise e

                async for line in response.aiter_lines():
                    if line:
                        yield f"{line}\n"
                    else:
                        yield "\n"

            except Exception as e:
                logger.error("{}_ERROR:{} {}: {}", tag, req_tag, type(e).__name__, e)
                mapped_e = map_error(e)
                if getattr(mapped_e, "status_code", None) == 405:
                    error_message = (
                        f"Upstream provider {tag} rejected the request method "
                        "or endpoint (HTTP 405)."
                    )
                else:
                    error_message = get_user_facing_error_message(
                        mapped_e, read_timeout_s=self._config.http_read_timeout
                    )
                if request_id:
                    error_message += f"\nRequest ID: {request_id}"

                logger.info(
                    "{}_STREAM: Emitting Anthropic-compatible error message for {}{}",
                    tag,
                    type(e).__name__,
                    req_tag,
                )

                sse = SSEBuilder(
                    f"msg_{uuid.uuid4().hex}",
                    getattr(request, "model", "unknown"),
                    input_tokens,
                )
                yield sse.message_start()
                for event in sse.emit_error(error_message):
                    yield event
                yield sse.message_delta("end_turn", 1)
                yield sse.message_stop()

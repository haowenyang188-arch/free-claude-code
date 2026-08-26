from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from providers.base import ProviderConfig
from providers.minimax import MINIMAX_BASE_URL, MiniMaxProvider


def _make_provider(*, enable_thinking: bool = True) -> MiniMaxProvider:
    return MiniMaxProvider(
        ProviderConfig(
            api_key="test-minimax-key",
            base_url=MINIMAX_BASE_URL,
            rate_limit=10,
            rate_window=60,
            enable_thinking=enable_thinking,
        )
    )


def _make_request() -> MagicMock:
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "Read",
                    "input": {"file_path": "/tmp/example"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": "example",
                }
            ],
        },
    ]
    request = MagicMock()
    request.model = "minimax/MiniMax-M3"
    request.upstream_model = "MiniMax-M3"
    request.thinking.enabled = True
    request.model_dump.return_value = {
        "model": request.model,
        "messages": messages,
        "stream": True,
        "thinking": {"enabled": True},
        "extra_body": {},
        "original_model": "claude-sonnet-4",
        "resolved_provider_model": request.model,
        "upstream_model": request.upstream_model,
    }
    return request


@pytest.mark.asyncio
async def test_build_request_body_preserves_anthropic_content_blocks():
    provider = _make_provider()
    request = _make_request()

    body = provider._build_request_body(request)

    assert body["model"] == "MiniMax-M3"
    assert body["messages"] == request.model_dump.return_value["messages"]
    assert body["thinking"] == {"type": "enabled"}
    assert body["max_tokens"] == 81920
    assert "extra_body" not in body
    assert "original_model" not in body
    assert "resolved_provider_model" not in body
    assert "upstream_model" not in body
    await provider.cleanup()


@pytest.mark.asyncio
async def test_build_request_body_removes_thinking_when_disabled():
    provider = _make_provider(enable_thinking=False)
    request = _make_request()

    body = provider._build_request_body(request)

    assert "thinking" not in body
    await provider.cleanup()


@pytest.mark.asyncio
async def test_request_uses_anthropic_headers_and_expected_url():
    provider = _make_provider()
    request = provider._client.build_request(
        "POST",
        "/v1/messages",
        json={"model": "MiniMax-M3"},
        headers=provider._request_headers(),
    )

    assert str(request.url) == "https://api.minimaxi.com/anthropic/v1/messages"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["x-api-key"] == "test-minimax-key"
    await provider.cleanup()


class _SSEStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'


@pytest.mark.asyncio
async def test_stream_response_closes_upstream_response():
    provider = _make_provider()
    request = _make_request()
    response = httpx.Response(
        200,
        request=httpx.Request("POST", f"{MINIMAX_BASE_URL}/v1/messages"),
        stream=_SSEStream(),
    )
    with patch.object(
        provider._global_rate_limiter,
        "execute_with_retry",
        new_callable=AsyncMock,
        return_value=response,
    ):
        events = [event async for event in provider.stream_response(request)]

    assert "message_stop" in "".join(events)
    assert response.is_closed
    await provider.cleanup()

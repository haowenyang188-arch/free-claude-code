"""Canvas 只读集成挂载点测试（P1-A）。

覆盖：禁用态 503 / 路由查询串拼装 / 转发鉴权头 / 上游错误与不可达映射。
仓库测试未启用 pytest-asyncio，这里统一用 asyncio.run 包裹异步调用。
不依赖真实 agent-server（httpx.MockTransport 注入或 patch _proxy_get）。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import workbench.backend.integrations.canvas as canvas_mod
from workbench.backend.integrations.canvas import (
    _proxy_get,
    _proxy_post,
    canvas_get_session,
    canvas_get_sessions,
    canvas_search_events,
    canvas_search_sessions,
    canvas_send_message,
    is_enabled,
)

KEY = "test-session-api-key"


def _mock_factory(status: int, payload, *, handler=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if handler is not None:
            handler(request)
        return httpx.Response(status, json=payload)

    transport = httpx.MockTransport(handle)
    return lambda: httpx.AsyncClient(transport=transport)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("WORKBENCH_CANVAS_SESSION_API_KEY", raising=False)
    monkeypatch.delenv("WORKBENCH_CANVAS_AGENT_BASE", raising=False)
    yield


def test_disabled_returns_503():
    assert is_enabled() is False
    response = asyncio.run(canvas_search_sessions(limit=5))
    assert response.status_code == 503
    assert "canvas_integration_disabled" in response.body.decode()


def test_health_reports_disabled():
    health = asyncio.run(canvas_mod.canvas_health())
    assert health["enabled"] is False


def test_search_router_builds_query(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    seen: list[str] = []

    async def fake_proxy(path_and_query: str, **kwargs):
        seen.append(path_and_query)
        return JSONResponse(status_code=200, content={"items": []})

    monkeypatch.setattr(canvas_mod, "_proxy_get", fake_proxy)
    response = asyncio.run(canvas_search_sessions(limit=3, status="running"))
    assert response.status_code == 200
    assert seen == [
        "/api/conversations/search?limit=3&sort_order=CREATED_AT_DESC&include_skills=false&status=running"
    ]


def test_get_sessions_router_builds_query(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    seen: list[str] = []

    async def fake_proxy(path_and_query: str, **kwargs):
        seen.append(path_and_query)
        return JSONResponse(status_code=200, content=[])

    monkeypatch.setattr(canvas_mod, "_proxy_get", fake_proxy)
    response = asyncio.run(canvas_get_sessions(ids=["a", " b ", "c"]))
    assert response.status_code == 200
    assert seen == ["/api/conversations?ids=a&ids=b&ids=c"]


def test_get_sessions_router_accepts_repeated_query_parameters(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    seen: list[str] = []

    async def fake_proxy(path_and_query: str, **kwargs):
        seen.append(path_and_query)
        return JSONResponse(status_code=200, content=[])

    app = FastAPI()
    app.include_router(canvas_mod.router)
    monkeypatch.setattr(canvas_mod, "_proxy_get", fake_proxy)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.get(
                "/api/integrations/canvas/conversations?ids=a&ids=b"
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert seen == ["/api/conversations?ids=a&ids=b"]


def test_get_session_router_builds_escaped_uuid_query(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    seen: list[str] = []

    async def fake_proxy(path_and_query: str, **kwargs):
        seen.append(path_and_query)
        return JSONResponse(
            status_code=200, content={"id": path_and_query.rsplit("/", 1)[-1]}
        )

    monkeypatch.setattr(canvas_mod, "_proxy_get", fake_proxy)
    response = asyncio.run(canvas_get_session("f8560f42-a1dd-412a-96fe-2494d6335226"))
    assert response.status_code == 200
    assert seen == ["/api/conversations/f8560f42-a1dd-412a-96fe-2494d6335226"]


def test_events_router_forwards_filters(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    seen: list[str] = []

    async def fake_proxy(path_and_query: str, **kwargs):
        seen.append(path_and_query)
        return JSONResponse(
            status_code=200, content={"items": [], "next_page_id": None}
        )

    monkeypatch.setattr(canvas_mod, "_proxy_get", fake_proxy)
    response = asyncio.run(
        canvas_search_events(
            "f8560f42-a1dd-412a-96fe-2494d6335226",
            limit=25,
            page_id="next page",
            kind="MessageEvent",
            source="user",
            body="hello & goodbye",
            sort_order="TIMESTAMP_DESC",
            timestamp_gte="2026-09-04T00:00:00Z",
            timestamp_lt=None,
        )
    )
    assert response.status_code == 200
    assert seen == [
        "/api/conversations/f8560f42-a1dd-412a-96fe-2494d6335226/events/search?"
        "limit=25&page_id=next%20page&kind=MessageEvent&source=user&body=hello%20%26%20goodbye&"
        "sort_order=TIMESTAMP_DESC&timestamp__gte=2026-09-04T00%3A00%3A00Z"
    ]


def test_send_message_proxy_preserves_upstream_payload(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("X-Session-API-Key")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    payload = {
        "role": "assistant",
        "content": [{"type": "text", "text": "继续"}],
        "run": False,
    }
    response = asyncio.run(
        _proxy_post(
            "/api/conversations/f8560f42-a1dd-412a-96fe-2494d6335226/events",
            payload,
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
    )
    assert response.status_code == 200
    assert seen["auth"] == KEY
    assert seen["payload"] == payload


def test_send_message_router_uses_run_true(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    seen: dict[str, object] = {}

    async def fake_proxy(path_and_query: str, payload, **kwargs):
        seen["path"] = path_and_query
        seen["payload"] = payload
        return JSONResponse(status_code=200, content={"ok": True})

    monkeypatch.setattr(canvas_mod, "_proxy_post", fake_proxy)
    response = asyncio.run(
        canvas_send_message(
            "f8560f42-a1dd-412a-96fe-2494d6335226",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "继续"}],
                "run": False,
            },
        )
    )
    assert response.status_code == 200
    assert (
        seen["path"] == "/api/conversations/f8560f42-a1dd-412a-96fe-2494d6335226/events"
    )
    assert seen["payload"] == {
        "role": "user",
        "content": [{"type": "text", "text": "继续"}],
        "run": True,
    }


def test_proxy_sends_auth_header_and_base(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    monkeypatch.setenv("WORKBENCH_CANVAS_AGENT_BASE", "http://127.0.0.1:18000")
    seen: list[str] = []

    def handler(request: httpx.Request) -> None:
        seen.append(str(request.url))
        assert request.headers.get("X-Session-API-Key") == KEY
        assert request.url.params.get("sort_order") == "CREATED_AT_DESC"

    response = asyncio.run(
        _proxy_get(
            "/api/conversations/search?limit=1&sort_order=CREATED_AT_DESC&include_skills=false",
            client_factory=_mock_factory(200, {"items": []}, handler=handler),
        )
    )
    assert response.status_code == 200
    assert "items" in response.body.decode()
    assert seen and seen[0].startswith(
        "http://127.0.0.1:18000/api/conversations/search"
    )


def test_upstream_http_error_maps_to_502(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    response = asyncio.run(
        _proxy_get(
            "/api/conversations/search?limit=1",
            client_factory=_mock_factory(500, {"detail": "boom"}),
        )
    )
    assert response.status_code == 502
    assert "canvas_upstream_error" in response.body.decode()


def test_upstream_connection_error_maps_to_502(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)

    def raise_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    response = asyncio.run(
        _proxy_get(
            "/api/conversations/search?limit=1",
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(raise_error)
            ),
        )
    )
    assert response.status_code == 502
    assert "canvas_upstream_unreachable" in response.body.decode()


def test_non_json_upstream_maps_to_502(monkeypatch):
    monkeypatch.setenv("WORKBENCH_CANVAS_SESSION_API_KEY", KEY)
    response = asyncio.run(
        _proxy_get(
            "/api/conversations/search",
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, text="not-json")
                )
            ),
        )
    )
    assert response.status_code == 502
    assert "canvas_upstream_invalid_response" in response.body.decode()

"""Canvas（Agent Canvas / agent-server 18000）集成挂载点 —— WebUI P1-A。

架构定案（E:\\WorkBuddy\\WEBUI_ARCH_ADOPTED.md）：
  - Workbench 是系统（根），Canvas 是界面能力（枝）；本挂载点是 Workbench 侧
  为「会话状态联动与继续对话」提供的受控转发边界。
- 独立命名空间 /api/integrations/canvas/*，与冻结业务路由隔离，可整体开关。
- 会话读取与受控用户消息透传；SOP 状态唯一属主仍是 Workbench + SOP Engine。

配置（env，缺省=禁用，返回 503 canvas_integration_disabled）：
  WORKBENCH_CANVAS_AGENT_BASE        默认 http://127.0.0.1:18000
  WORKBENCH_CANVAS_SESSION_API_KEY   访问 agent-server 所需的 X-Session-API-Key
                                     （由编排脚本从 agent-canvas 侧获取后注入；
                                      不在代码里硬编码/猜测）

上游对齐（agent-server /openapi.json，2026-09-03 实测）：
  GET /api/conversations/search?limit&status&sort_order=CREATED_AT_DESC -> {"items": [...]}
  GET /api/conversations?ids=a&ids=b                                     -> [...]
两者返回元素同构（ConversationSession，frontend/src/types/conversation.ts）。事件发送
保持 agent-server 原生 SendMessageRequest 形状，固定为用户消息并继续运行。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/integrations/canvas", tags=["integrations"])

_AGENT_BASE_DEFAULT = "http://127.0.0.1:18000"


def _query_text(value: object) -> str | None:
    """FastAPI Query defaults are Query objects when a route is unit-called directly."""
    return value if isinstance(value, str) and value else None


def _query_int(value: object, default: int) -> int:
    """FastAPI Query defaults are Query objects when a route is unit-called directly."""
    return value if isinstance(value, int) else default


def _agent_base() -> str:
    return os.environ.get("WORKBENCH_CANVAS_AGENT_BASE", _AGENT_BASE_DEFAULT).rstrip(
        "/"
    )


def is_enabled() -> bool:
    """转发是否可用：显式配置了 Session API Key 才算启用。"""
    key = os.environ.get("WORKBENCH_CANVAS_SESSION_API_KEY", "").strip()
    return bool(key)


def _auth_headers() -> dict[str, str]:
    return {"X-Session-API-Key": os.environ["WORKBENCH_CANVAS_SESSION_API_KEY"]}


async def _proxy_get(
    path_and_query: str,
    client_factory=None,
) -> JSONResponse:
    """把上游 JSON 原样透传；错误统一映射为 5xx JSON（保持 workbench 错误信封）。

    client_factory 仅供测试注入 httpx.MockTransport；生产默认每次请求新建
    AsyncClient（timeout 6s）。
    """
    if not is_enabled():
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "canvas_integration_disabled",
                    "message": "Canvas 集成未启用：未配置 WORKBENCH_CANVAS_SESSION_API_KEY",
                }
            },
        )
    url = f"{_agent_base()}{path_and_query}"
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=6.0))
    try:
        async with factory() as client:
            response = await client.get(url, headers=_auth_headers())
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "canvas_upstream_unreachable",
                    "message": f"无法连接 agent-server({_agent_base()}): {exc.__class__.__name__}",
                }
            },
        )
    if response.status_code >= 400:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "canvas_upstream_error",
                    "message": f"agent-server 返回 {response.status_code}",
                }
            },
        )
    try:
        content = response.json()
    except ValueError:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "canvas_upstream_invalid_response",
                    "message": "agent-server 返回了非 JSON 响应",
                }
            },
        )
    return JSONResponse(status_code=200, content=content)


async def _proxy_post(
    path: str,
    payload: Mapping[str, Any],
    client_factory=None,
) -> JSONResponse:
    """把受控消息写入 agent-server；只允许 Workbench 同源调用。"""
    if not is_enabled():
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "canvas_integration_disabled",
                    "message": "Canvas 集成未启用：未配置 WORKBENCH_CANVAS_SESSION_API_KEY",
                }
            },
        )
    url = f"{_agent_base()}{path}"
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=6.0))
    try:
        async with factory() as client:
            response = await client.post(
                url, headers=_auth_headers(), json=dict(payload)
            )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "canvas_upstream_unreachable",
                    "message": f"无法连接 agent-server({_agent_base()}): {exc.__class__.__name__}",
                }
            },
        )
    if response.status_code >= 400:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "canvas_upstream_error",
                    "message": f"agent-server 返回 {response.status_code}",
                }
            },
        )
    try:
        content = response.json()
    except ValueError:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "canvas_upstream_invalid_response",
                    "message": "agent-server 返回了非 JSON 响应",
                }
            },
        )
    return JSONResponse(status_code=response.status_code, content=content)


@router.get("/health")
async def canvas_health() -> dict:
    """集成健康（恒 200；enabled=false 表示未配置 key，前端据此隐藏状态卡）。"""
    return {"enabled": is_enabled(), "agent_base": _agent_base()}


@router.get("/conversations/search")
async def canvas_search_sessions(
    limit: int = Query(default=8, ge=1, le=100),
    status: str | None = Query(default=None),
) -> JSONResponse:
    """转发 GET /api/conversations/search（最近会话倒序，供会话状态卡）。"""
    query = f"/api/conversations/search?limit={_query_int(limit, 8)}&sort_order=CREATED_AT_DESC&include_skills=false"
    status_value = _query_text(status)
    if status_value:
        query += f"&status={quote(status_value, safe='')}"
    return await _proxy_get(query)


@router.get("/conversations")
async def canvas_get_sessions(ids: list[str] = Query(...)) -> JSONResponse:
    """转发批量会话查询；上游要求每个 ID 使用一个重复的 ids 参数。"""
    safe_ids = [
        quote(part.strip(), safe="")
        for value in ids
        for part in value.split(",")
        if part.strip()
    ]
    if not safe_ids:
        raise HTTPException(status_code=422, detail="ids 不能为空")
    query = "&".join(f"ids={conversation_id}" for conversation_id in safe_ids)
    return await _proxy_get(f"/api/conversations?{query}")


@router.get("/conversations/{conversation_id}")
async def canvas_get_session(conversation_id: str) -> JSONResponse:
    """转发单个会话详情，供深链页和历史面板校验会话是否存在。"""
    safe_id = quote(conversation_id.strip(), safe="")
    if not safe_id:
        raise HTTPException(status_code=422, detail="conversation_id 不能为空")
    return await _proxy_get(f"/api/conversations/{safe_id}")


@router.get("/conversations/{conversation_id}/events/search")
async def canvas_search_events(
    conversation_id: str,
    page_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=100),
    kind: str | None = Query(default=None),
    source: str | None = Query(default=None),
    body: str | None = Query(default=None),
    sort_order: str = Query(default="TIMESTAMP"),
    timestamp_gte: str | None = Query(default=None, alias="timestamp__gte"),
    timestamp_lt: str | None = Query(default=None, alias="timestamp__lt"),
) -> JSONResponse:
    """转发会话事件历史，保留 agent-server 的分页与过滤语义。"""
    safe_id = quote(conversation_id.strip(), safe="")
    if not safe_id:
        raise HTTPException(status_code=422, detail="conversation_id 不能为空")
    query_parts = [f"limit={_query_int(limit, 100)}"]
    for key, value in (
        ("page_id", page_id),
        ("kind", kind),
        ("source", source),
        ("body", body),
        ("sort_order", sort_order),
        ("timestamp__gte", timestamp_gte),
        ("timestamp__lt", timestamp_lt),
    ):
        value_text = _query_text(value)
        if value_text is not None:
            query_parts.append(f"{key}={quote(value_text, safe='')}")
    query = "&".join(query_parts)
    return await _proxy_get(f"/api/conversations/{safe_id}/events/search?{query}")


@router.post("/conversations/{conversation_id}/events")
async def canvas_send_message(
    conversation_id: str,
    payload: dict[str, Any] = Body(...),
) -> JSONResponse:
    """把用户消息发送到指定会话，并要求 agent-server 继续运行。"""
    safe_id = quote(conversation_id.strip(), safe="")
    if not safe_id:
        raise HTTPException(status_code=422, detail="conversation_id 不能为空")
    message = dict(payload)
    message["role"] = "user"
    message["run"] = True
    content = message.get("content")
    if not isinstance(content, list) or not content:
        raise HTTPException(status_code=422, detail="content 不能为空")
    return await _proxy_post(
        f"/api/conversations/{safe_id}/events",
        message,
    )

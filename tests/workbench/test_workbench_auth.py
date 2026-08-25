from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from workbench.backend.runtime.auth import WorkbenchAuth


def test_auth_without_configured_token_allows_loopback_only():
    auth = WorkbenchAuth(None)

    assert auth.is_request_allowed(None, "127.0.0.1") is True
    assert auth.is_request_allowed(None, "::1") is True
    assert auth.is_request_allowed(None, "100.64.0.2") is False


def test_auth_with_configured_token_accepts_bearer_or_cookie():
    auth = WorkbenchAuth("secret-token")

    assert auth.verify("Bearer secret-token", None) is True
    assert auth.verify(None, "secret-token") is True
    assert auth.verify("Bearer wrong", None) is False
    assert auth.verify(None, "wrong") is False


def test_auth_rejects_malformed_authorization_headers():
    auth = WorkbenchAuth("secret-token")

    assert auth.verify("Basic secret-token", None) is False
    assert auth.verify("Bearer", None) is False
    assert auth.verify("Bearer secret-token extra", None) is False


@pytest.mark.asyncio
async def test_workbench_api_requires_login_and_sets_http_only_cookie():
    from workbench.backend import main as main_module

    previous_auth = main_module.auth
    main_module.auth = WorkbenchAuth("secret-token")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=main_module.app), base_url="http://test"
        ) as client:
            unauthorized = await client.get("/api/agents")
            wrong = await client.post("/api/auth/login", json={"token": "wrong"})
            login = await client.post("/api/auth/login", json={"token": "secret-token"})
            authorized = await client.get("/api/agents")
    finally:
        main_module.auth = previous_auth

    assert unauthorized.status_code == 401
    assert wrong.status_code == 401
    assert login.status_code == 200
    assert "httponly" in login.headers.get("set-cookie", "").lower()
    assert authorized.status_code == 200

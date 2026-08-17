from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import app
from api.dependencies import _is_loopback_bind, get_settings
from config.settings import Settings


def test_anthropic_auth_token_required_and_accepts_x_api_key():
    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "s3cr3t"
    app.dependency_overrides[get_settings] = lambda: settings

    payload = {
        "model": "claude-3-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch("api.routes.get_token_count", return_value=1):
        # No header -> 401
        r = client.post("/v1/messages/count_tokens", json=payload)
        assert r.status_code == 401

        # X-API-Key header -> 200
        r = client.post(
            "/v1/messages/count_tokens", json=payload, headers={"X-API-Key": "s3cr3t"}
        )
        assert r.status_code == 200
        assert r.json()["input_tokens"] == 1

    app.dependency_overrides.clear()


def test_anthropic_auth_token_accepts_bearer_authorization():
    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "b3artoken"
    app.dependency_overrides[get_settings] = lambda: settings

    payload = {
        "model": "claude-3-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch("api.routes.get_token_count", return_value=2):
        # Authorization Bearer -> 200
        r = client.post(
            "/v1/messages/count_tokens",
            json=payload,
            headers={"Authorization": "Bearer b3artoken"},
        )
        assert r.status_code == 200
        assert r.json()["input_tokens"] == 2

    app.dependency_overrides.clear()


def test_anthropic_auth_token_applies_to_models_endpoint():
    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "models-token"
    app.dependency_overrides[get_settings] = lambda: settings

    r = client.get("/v1/models")
    assert r.status_code == 401

    r = client.get("/v1/models", headers={"X-API-Key": "models-token"})
    assert r.status_code == 200
    assert "data" in r.json()

    app.dependency_overrides.clear()


def test_non_loopback_bind_requires_api_key_configuration():
    client = TestClient(app)
    settings = Settings()
    settings.host = "0.0.0.0"
    settings.anthropic_auth_token = ""
    app.dependency_overrides[get_settings] = lambda: settings

    try:
        response = client.get("/v1/models")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "ANTHROPIC_AUTH_TOKEN" in response.json()["detail"]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        (" LOCALHOST ", True),
        ("127.0.0.1", True),
        ("127.255.255.254", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("::", False),
        ("localhost.localdomain", False),
        ("", False),
        (None, False),
        (8082, False),
    ],
)
def test_is_loopback_bind_fails_closed(host, expected):
    assert _is_loopback_bind(host) is expected

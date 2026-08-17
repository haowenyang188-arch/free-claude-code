"""API routing tests for the optional DeepSeek Harness sidecar."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import app
from harness.bridge import HarnessBridgeError


class _FakeHarnessManager:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def _stream(self, content_blocks, **kwargs):
        self.calls.append({"content_blocks": content_blocks, **kwargs})
        yield "event: message_start\ndata: {}\n\n"
        yield "event: message_stop\ndata: {}\n\n"

    def stream_messages(self, content_blocks, **kwargs):
        return self._stream(content_blocks, **kwargs)


class _PreflightFailHarnessManager(_FakeHarnessManager):
    async def ensure_ready(self, **kwargs):
        raise HarnessBridgeError("DSH_CORDIS_CONFIG is missing")


class _StatusHarnessManager:
    def diagnostics(self):
        return {
            "enabled": True,
            "configured": True,
            "ready": False,
            "running": False,
            "error": "runtime command not found",
        }


def test_dsh_route_bypasses_native_provider_and_keeps_structured_content():
    manager = _FakeHarnessManager()
    app.state.harness_bridge = manager
    client = TestClient(app)
    try:
        with patch("api.routes.get_provider_for_type") as get_provider:
            response = client.post(
                "/v1/messages",
                json={
                    "model": "dsh/deepseek/deepseek-v4-flash",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "hello"},
                                {"type": "thinking", "thinking": "context"},
                            ],
                        }
                    ],
                    "max_tokens": 32,
                    "stream": True,
                },
            )

        assert response.status_code == 200
        get_provider.assert_not_called()
        assert len(manager.calls) == 1
        call = manager.calls[0]
        assert call["provider"] == "deepseek"
        assert call["model"] == "deepseek-v4-flash"
        assert call["content_blocks"] == [
            {"type": "text", "text": "[user]\nhello"},
            {"type": "thinking", "thinking": "context"},
        ]
    finally:
        delattr(app.state, "harness_bridge")


def test_dsh_route_rejects_dynamic_tools_without_fallback():
    manager = _FakeHarnessManager()
    app.state.harness_bridge = manager
    client = TestClient(app)
    try:
        with patch("api.routes.get_provider_for_type") as get_provider:
            response = client.post(
                "/v1/messages",
                json={
                    "model": "dsh/deepseek/deepseek-v4-flash",
                    "messages": [{"role": "user", "content": "hello"}],
                    "tools": [
                        {
                            "name": "lookup",
                            "description": "lookup",
                            "input_schema": {"type": "object"},
                        }
                    ],
                    "max_tokens": 32,
                    "stream": True,
                },
            )

        assert response.status_code == 400
        assert "dynamic" in response.json()["error"]["message"]
        get_provider.assert_not_called()
        assert manager.calls == []
    finally:
        delattr(app.state, "harness_bridge")


def test_dsh_route_rejects_image_content_without_fallback():
    manager = _FakeHarnessManager()
    app.state.harness_bridge = manager
    client = TestClient(app)
    try:
        with patch("api.routes.get_provider_for_type") as get_provider:
            response = client.post(
                "/v1/messages",
                json={
                    "model": "dsh/deepseek/deepseek-v4-flash",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "abc",
                                    },
                                }
                            ],
                        }
                    ],
                    "max_tokens": 32,
                    "stream": True,
                },
            )

        assert response.status_code == 400
        assert "content block" in response.json()["error"]["message"]
        get_provider.assert_not_called()
        assert manager.calls == []
    finally:
        delattr(app.state, "harness_bridge")


def test_dsh_route_fails_closed_when_manager_is_not_configured():
    if hasattr(app.state, "harness_bridge"):
        delattr(app.state, "harness_bridge")
    client = TestClient(app)

    with patch("api.routes.get_provider_for_type") as get_provider:
        response = client.post(
            "/v1/messages",
            json={
                "model": "dsh/deepseek/deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 32,
                "stream": True,
            },
        )

    assert response.status_code == 503
    assert "Harness" in response.json()["detail"]
    get_provider.assert_not_called()


def test_dsh_route_returns_preflight_reason_before_opening_stream():
    app.state.harness_bridge = _PreflightFailHarnessManager()
    client = TestClient(app)
    try:
        response = client.post(
            "/v1/messages",
            json={
                "model": "dsh/deepseek/deepseek-chat",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 32,
                "stream": True,
            },
        )
        assert response.status_code == 503
        assert "DSH_CORDIS_CONFIG" in response.json()["detail"]
    finally:
        delattr(app.state, "harness_bridge")


def test_harness_status_exposes_runtime_diagnostics():
    app.state.harness_bridge = _StatusHarnessManager()
    client = TestClient(app)
    try:
        response = client.get("/v1/harness/status")
        assert response.status_code == 200
        assert response.json() == {
            "enabled": True,
            "configured": True,
            "ready": False,
            "running": False,
            "error": "runtime command not found",
        }
    finally:
        delattr(app.state, "harness_bridge")

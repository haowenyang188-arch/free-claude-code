from __future__ import annotations

from fastapi.testclient import TestClient

from runner import server


def test_start_rejects_command_injection_fields(monkeypatch) -> None:
    called = False

    async def unexpected_start(_payload):
        nonlocal called
        called = True
        raise AssertionError("runner must not start for an invalid payload")

    monkeypatch.setattr(server.manager, "start", unexpected_start)
    response = TestClient(server.app).post(
        "/runs/start",
        json={"channel": "xhs", "cmd": "whoami"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "UNSUPPORTED_FIELDS"
    assert called is False


def test_start_returns_truthful_process_identity(monkeypatch) -> None:
    async def fake_start(request):
        assert request.channel == "xhs"
        assert request.limit == 4
        return {"run_id": "run-1", "status": "running", "pid": 1234}

    monkeypatch.setattr(server.manager, "start", fake_start)
    response = TestClient(server.app).post(
        "/runs/start",
        json={"channel": "xhs", "limit": 4, "boot": False},
    )

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1", "status": "running", "pid": 1234}


def test_health_is_local_runner_probe() -> None:
    response = TestClient(server.app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["channels"] == ["douyin", "xhs"]


def test_server_defaults_to_loopback_runner_port() -> None:
    assert server.HOST == "127.0.0.1"
    assert server.PORT == 8790

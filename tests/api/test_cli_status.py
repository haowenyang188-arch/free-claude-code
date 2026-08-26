from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient


def test_cli_status_reports_uninitialized_manager() -> None:
    from api.app import create_app

    app = create_app()
    app.state.cli_manager = None

    with TestClient(app) as client:
        response = client.get("/v1/cli/status")

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "backend": None,
        "runtime": None,
        "error": "CLI session manager is not initialized",
    }


def test_cli_status_returns_redacted_runtime_report() -> None:
    from api.app import create_app

    app = create_app()
    manager = MagicMock()
    manager.runtime_status = AsyncMock(
        return_value={
            "backend": "codex",
            "runtime": {
                "backend": "codex",
                "executable": "codex",
                "available": True,
                "version": "0.149.1",
                "capabilities": ["jsonl_events", "resume"],
                "reason": None,
            },
        }
    )
    with TestClient(app) as client:
        app.state.cli_manager = manager
        response = client.get("/v1/cli/status")

    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert response.json()["runtime"]["version"] == "0.149.1"
    assert response.json()["error"] is None
    manager.runtime_status.assert_awaited_once_with()

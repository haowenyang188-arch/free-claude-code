import pytest

from adapters.openhands_acp.sop_client import SopClient


def test_start_run_forwards_canvas_correlation_metadata(
    monkeypatch: pytest.MonkeyPatch,
):
    client = SopClient()
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method: str, path: str, body: dict | None = None) -> dict:
        calls.append((method, path, body))
        return {"sop_run_id": "run-1"}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.start_run(
        "linked goal", metadata={"canvas_acp_session_id": "acp-session-1"}
    ) == {"sop_run_id": "run-1"}
    assert calls == [
        (
            "POST",
            "/api/sop-runs",
            {
                "goal_description": "linked goal",
                "sop_definition_id": "sop-console-claude-codex-v2",
                "acceptance_criteria": ["linked goal"],
                "constraints": [
                    "No paid operations",
                    "Only touch the workspace directory",
                ],
                "metadata": {"canvas_acp_session_id": "acp-session-1"},
                "auto_execute": True,
            },
        )
    ]

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from cli.mcp_preflight import (
    check_proxy_connectivity,
    load_codex_server_config,
    main,
    parse_response_payload,
    probe_mcp_server,
    summarize_proxy_environment,
)


def test_proxy_summary_redacts_credentials_and_keeps_connection_target() -> None:
    summary = summarize_proxy_environment(
        {
            "HTTPS_PROXY": "http://user:secret@127.0.0.1:7890",
            "NO_PROXY": "localhost,127.0.0.1",
        }
    )

    assert summary["HTTPS_PROXY"] == {
        "configured": True,
        "scheme": "http",
        "host": "127.0.0.1",
        "port": 7890,
        "has_credentials": True,
    }
    assert "secret" not in json.dumps(summary)
    assert summary["NO_PROXY"] == {"configured": True, "value": "localhost,127.0.0.1"}


def test_parse_response_payload_supports_json_and_sse() -> None:
    json_body = '{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
    sse_body = 'event: message\ndata: {"jsonrpc":"2.0","id":1}\n\n'

    assert parse_response_payload(json_body, "application/json")["result"] == {
        "tools": []
    }
    assert parse_response_payload(sse_body, "text/event-stream")["id"] == 1


class _McpHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[dict, dict[str, str]]]] = []
    mode = "ok"
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(
            (payload, {key.lower(): value for key, value in self.headers.items()})
        )

        if self.mode == "forbidden":
            body_bytes = b"forbidden"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        method = payload["method"]
        if method == "initialize":
            body = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test-mcp", "version": "1"},
                },
            }
            self.send_response(200)
            self.send_header("Mcp-Session-Id", "test-session")
            self.send_header("Mcp-Protocol-Version", "2025-11-25")
            if self.mode == "sse_initialize":
                event = f"event: message\ndata: {json.dumps(body)}\n\n".encode()
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(event)
                self.wfile.flush()
                time.sleep(0.5)
                return
        elif method == "notifications/initialized":
            body = None
            self.send_response(202)
        elif method == "tools/list":
            body = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"tools": [{"name": "echo", "description": "Echo"}]},
            }
            self.send_response(200)
        else:
            self.send_response(400)
            body = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": -32601},
            }

        body_bytes = b"" if body is None else json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        if body_bytes:
            self.wfile.write(body_bytes)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def mcp_server() -> tuple[ThreadingHTTPServer, str]:
    _McpHandler.requests = []
    _McpHandler.mode = "ok"
    _McpHandler.protocol_version = "HTTP/1.0"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _McpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_port}/mcp"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_probe_runs_initialize_initialized_and_tools_list(
    mcp_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _server, url = mcp_server

    result = probe_mcp_server(url, timeout=2, no_proxy=True)

    assert result["status"] == "ok"
    assert result["session_id"] == "test-session"
    assert result["tools"]["count"] == 1
    assert [payload["method"] for payload, _headers in _McpHandler.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert _McpHandler.requests[2][1]["mcp-session-id"] == "test-session"


def test_probe_reads_sse_event_without_waiting_for_stream_close(
    mcp_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _server, url = mcp_server
    _McpHandler.mode = "sse_initialize"
    _McpHandler.protocol_version = "HTTP/1.1"

    result = probe_mcp_server(url, timeout=0.2, no_proxy=True)

    assert result["status"] == "ok"
    assert result["classification"] == "mcp_ready"


def test_probe_classifies_http_forbidden_without_calling_it_handshake_failure(
    mcp_server: tuple[ThreadingHTTPServer, str],
) -> None:
    _server, url = mcp_server
    _McpHandler.mode = "forbidden"

    result = probe_mcp_server(url, timeout=2, no_proxy=True)

    assert result["status"] == "error"
    assert result["classification"] == "http_forbidden"
    assert result["http_status"] == 403
    assert result["handshake_completed"] is False


def test_load_codex_server_config_reads_only_safe_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[mcp_servers.openaiDeveloperDocs]
url = "https://developers.openai.com/mcp"
startup_timeout_sec = 60
tool_timeout_sec = 60
enabled = true
""".lstrip(),
        encoding="utf-8",
    )

    result = load_codex_server_config(config_path, "openaiDeveloperDocs")

    assert result == {
        "configured": True,
        "url": "https://developers.openai.com/mcp",
        "enabled": True,
        "startup_timeout_sec": 60,
        "tool_timeout_sec": 60,
        "transport": "streamable_http",
    }


def test_load_codex_server_config_redacts_url_query(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[mcp_servers.docs]\nurl = "https://example.test/mcp?token=secret"\n',
        encoding="utf-8",
    )

    result = load_codex_server_config(config_path, "docs")

    assert result["url"] == "https://example.test/mcp"
    assert "secret" not in json.dumps(result)


def test_probe_rejects_and_redacts_url_credentials() -> None:
    result = probe_mcp_server(
        "https://user:secret@example.test/mcp?token=query-secret",
        timeout=1,
        no_proxy=True,
    )

    assert result["classification"] == "credentials_in_url_rejected"
    assert result["url"] == "https://example.test/mcp"
    assert "secret" not in json.dumps(result)


def test_proxy_connectivity_reports_refused_endpoint_without_echoing_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(socket, "create_connection", refuse)

    result = check_proxy_connectivity(
        {"HTTP_PROXY": "http://user:secret@127.0.0.1:7890"}, timeout=1
    )

    assert result["HTTP_PROXY"]["classification"] == "connection_refused"
    assert result["HTTP_PROXY"]["host"] == "127.0.0.1"
    assert "secret" not in json.dumps(result)


def test_proxy_connectivity_distinguishes_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", timeout)

    result = check_proxy_connectivity(
        {"HTTP_PROXY": "http://127.0.0.1:7890"}, timeout=1
    )

    assert result["HTTP_PROXY"]["classification"] == "connection_timeout"


def test_main_json_output_is_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cli.mcp_preflight.check_proxy_connectivity",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "cli.mcp_preflight.probe_mcp_server",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "classification": "mcp_ready",
            "handshake_completed": True,
            "tools": {"count": 0, "names": []},
        },
    )

    exit_code = main(["--url", "http://127.0.0.1:1/mcp", "--no-proxy", "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mcp"]["classification"] == "mcp_ready"
    assert output["proxy_mode"] == "direct"

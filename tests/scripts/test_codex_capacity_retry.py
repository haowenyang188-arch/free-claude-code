from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

from scripts.codex_capacity_retry import (
    CapacityRetryProxy,
    is_capacity_response,
    load_provider_urls,
)


class FakeProvider(BaseHTTPRequestHandler):
    attempts = 0
    bodies: ClassVar[list[bytes]] = []
    seen_headers: ClassVar[list[dict[str, str]]] = []
    responses: ClassVar[list[tuple[int, bytes, str]]] = []

    def do_POST(self) -> None:
        type(self).attempts += 1
        length = int(self.headers.get("content-length", "0"))
        type(self).bodies.append(self.rfile.read(length))
        type(self).seen_headers.append(dict(self.headers.items()))
        status, body, content_type = type(self).responses.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def provider():
    FakeProvider.attempts = 0
    FakeProvider.bodies = []
    FakeProvider.seen_headers = []
    FakeProvider.responses = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def capacity_body() -> bytes:
    return json.dumps(
        {
            "error": {
                "code": "server_is_overloaded",
                "message": "selected model is at capacity",
            }
        }
    ).encode()


def test_capacity_response_classifier_is_strict() -> None:
    assert is_capacity_response(503, capacity_body())
    assert not is_capacity_response(503, b'{"error":{"code":"temporary"}}')
    assert not is_capacity_response(429, capacity_body())


def test_proxy_retries_capacity_until_success(provider, monkeypatch) -> None:
    success = b'{"id":"ok"}'
    FakeProvider.responses = [
        (503, capacity_body(), "application/json"),
        (503, capacity_body(), "application/json"),
        (200, success, "application/json"),
    ]
    monkeypatch.setenv("CODEX_CAPACITY_RETRY_DELAYS", "0")
    proxy = CapacityRetryProxy(
        {"test": f"http://127.0.0.1:{provider.server_port}/v1"},
        retry_delays=(0,),
    )
    proxy.start()
    try:
        request = Request(
            proxy.url("test", "/responses"),
            data=b'{"input":"same"}',
            headers={"Authorization": "Bearer test", "X-Test": "same"},
        )
        with urlopen(request, timeout=3) as response:
            assert response.read() == success
        assert FakeProvider.attempts == 3
        assert FakeProvider.bodies == [b'{"input":"same"}'] * 3
        assert all(
            item["Authorization"] == "Bearer test" for item in FakeProvider.seen_headers
        )
        assert all(item["X-Test"] == "same" for item in FakeProvider.seen_headers)
    finally:
        proxy.stop()


def test_proxy_does_not_retry_ordinary_503(provider, monkeypatch) -> None:
    body = b'{"error":{"code":"temporary","message":"try later"}}'
    FakeProvider.responses = [(503, body, "application/json")]
    monkeypatch.setenv("CODEX_CAPACITY_RETRY_DELAYS", "0")
    proxy = CapacityRetryProxy(
        {"test": f"http://127.0.0.1:{provider.server_port}"},
        retry_delays=(0,),
    )
    proxy.start()
    try:
        request = Request(proxy.url("test", "/responses"), data=b"{}")
        with pytest.raises(HTTPError):
            urlopen(request, timeout=3)
        assert FakeProvider.attempts == 1
    finally:
        proxy.stop()


def test_proxy_streams_sse(provider, monkeypatch) -> None:
    chunks = b'data: {"ok":true}\n\n'
    FakeProvider.responses = [(200, chunks, "text/event-stream")]
    monkeypatch.setenv("CODEX_CAPACITY_RETRY_DELAYS", "0")
    proxy = CapacityRetryProxy(
        {"test": f"http://127.0.0.1:{provider.server_port}"},
        retry_delays=(0,),
    )
    proxy.start()
    try:
        with urlopen(
            Request(proxy.url("test", "/responses"), data=b"{}"), timeout=3
        ) as response:
            assert response.headers["Content-Type"] == "text/event-stream"
            assert response.read() == chunks
    finally:
        proxy.stop()


def test_wrapper_injects_profile_proxy_and_preserves_exit(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[model_providers.demo]\nbase_url = "http://127.0.0.1:9/v1"\n'
        'wire_api = "responses"\n'
    )
    (codex_home / "work.config.toml").write_text(
        '[model_providers.demo]\nbase_url = "http://127.0.0.1:8/profile"\n'
    )
    fake = tmp_path / "fake-codex.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps(sys.argv[1:]))\n"
        "raise SystemExit(7)\n"
    )
    fake.chmod(0o755)
    script = Path(__file__).parents[2] / "scripts" / "codex_capacity_retry.py"
    environment = os.environ | {
        "CODEX_HOME": str(codex_home),
        "CODEX_REAL_EXECUTABLE": str(fake),
    }
    result = subprocess.run(
        [sys.executable, str(script), "--profile", "work", "app-server", "--stdio"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 7
    arguments = json.loads(result.stdout)
    assert arguments[-3:] == ["--profile", "work", "app-server", "--stdio"][-3:]
    override = arguments[1]
    assert arguments[0] == "-c"
    assert override.startswith('model_providers.demo.base_url="http://127.0.0.1:')
    assert urlsplit(override.split('"', 2)[1]).path.endswith("/demo")
    assert load_provider_urls(["--profile", "work"], codex_home=codex_home) == {
        "demo": "http://127.0.0.1:8/profile"
    }

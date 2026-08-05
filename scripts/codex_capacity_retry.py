#!/usr/bin/env python3
"""Transparent local Responses proxy for Codex model-capacity errors."""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
import tomllib
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit, urlunsplit

DEFAULT_RETRY_DELAYS = (5.0, 15.0, 30.0, 60.0)
CAPACITY_CODES = {"server_is_overloaded", "server_overloaded"}
CAPACITY_MESSAGES = {
    "selected model is at capacity",
    "selected model is at capacity. please try a different model.",
}
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}


def is_capacity_response(status: int, body: bytes) -> bool:
    if status != 503:
        return False
    try:
        payload = json.loads(body)
    except TypeError, ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    code = str(error.get("code", "")).lower()
    message = " ".join(str(error.get("message", "")).lower().split())
    return code in CAPACITY_CODES or message in CAPACITY_MESSAGES


def _retry_delays_from_env() -> tuple[float, ...]:
    raw = os.environ.get("CODEX_CAPACITY_RETRY_DELAYS", "")
    if not raw:
        return DEFAULT_RETRY_DELAYS
    try:
        values = tuple(max(0.0, float(value.strip())) for value in raw.split(","))
    except ValueError:
        return DEFAULT_RETRY_DELAYS
    return values or DEFAULT_RETRY_DELAYS


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], proxy: CapacityRetryProxy):
        super().__init__(address, _ProxyHandler)
        self.proxy = proxy


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _forward(self) -> None:
        proxy = cast(_ProxyServer, self.server).proxy
        parsed = urlsplit(self.path)
        parts = parsed.path.lstrip("/").split("/", 1)
        if not parts[0] or parts[0] not in proxy.upstreams:
            self.send_error(404)
            return
        provider = parts[0]
        suffix = "/" + parts[1] if len(parts) == 2 else "/"
        body = self._read_body()
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }
        upstream_url = urlsplit(proxy.upstreams[provider])
        target_path = upstream_url.path.rstrip("/") + suffix
        target = urlunsplit(("", "", target_path or "/", parsed.query, ""))

        attempt = 0
        while True:
            if self._disconnected():
                return
            connection: HTTPConnection | HTTPSConnection | None = None
            try:
                connection_class = (
                    HTTPSConnection
                    if upstream_url.scheme == "https"
                    else HTTPConnection
                )
                connection = connection_class(upstream_url.netloc, timeout=120)
                connection.request(self.command, target, body=body, headers=headers)
                response = connection.getresponse()
                if response.status == 503:
                    response_body = response.read()
                    if is_capacity_response(response.status, response_body):
                        connection.close()
                        attempt += 1
                        delay = proxy.retry_delays[
                            min(attempt - 1, len(proxy.retry_delays) - 1)
                        ]
                        proxy._log_retry(attempt, delay)
                        if not proxy._wait_for_retry(self, delay):
                            return
                        continue
                    self._send_buffered(
                        response.status, response.getheaders(), response_body
                    )
                    return
                self._send_stream(response)
                return
            except BrokenPipeError, ConnectionResetError:
                return
            except OSError as exc:
                if not self.wfile.closed:
                    self.send_error(502, explain=str(exc))
                return
            finally:
                if connection is not None:
                    connection.close()

    def _read_body(self) -> bytes:
        value = self.headers.get("content-length")
        if not value:
            return b""
        try:
            length = max(0, int(value))
        except ValueError:
            return b""
        return self.rfile.read(length)

    def _send_buffered(
        self, status: int, headers: list[tuple[str, str]], body: bytes
    ) -> None:
        self.send_response(status)
        for key, value in headers:
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            if key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _send_stream(self, response) -> None:
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in HOP_BY_HOP_HEADERS or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except BrokenPipeError, ConnectionResetError:
            return

    def _disconnected(self) -> bool:
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError, OSError:
            return False


class CapacityRetryProxy:
    def __init__(
        self,
        upstreams: dict[str, str],
        *,
        retry_delays: tuple[float, ...] | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self.upstreams = upstreams
        self.retry_delays = retry_delays or _retry_delays_from_env()
        self.host = host
        self.server: _ProxyServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.server = _ProxyServer((self.host, 0), self)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        if self.server is None:
            raise RuntimeError("proxy is not running")
        return self.server.server_port

    def url(self, provider: str, path: str = "/responses") -> str:
        return f"http://{self.host}:{self.port}/{quote(provider, safe='')}{path}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _log_retry(self, attempt: int, delay: float) -> None:
        print(
            f"[codex-capacity-retry] model at capacity; retry {attempt} in {delay:g}s",
            file=sys.stderr,
            flush=True,
        )

    def _wait_for_retry(self, handler: _ProxyHandler, delay: float) -> bool:
        deadline = time.monotonic() + delay
        while True:
            if handler._disconnected():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.2, remaining))


def _merge_config(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    providers = dict(base.get("model_providers", {}))
    providers.update(overlay.get("model_providers", {}))
    if providers:
        result["model_providers"] = providers
    result.update(
        {key: value for key, value in overlay.items() if key != "model_providers"}
    )
    return result


def _wait_for_parent_exit(parent_pid: int) -> None:
    try:
        parent_fd = os.pidfd_open(parent_pid)
    except AttributeError, OSError:
        while True:
            try:
                with Path(f"/proc/{parent_pid}/stat").open() as stream:
                    if stream.read().split()[2] == "Z":
                        return
            except FileNotFoundError, IndexError:
                return
            time.sleep(0.5)
    else:
        try:
            select.select([parent_fd], [], [], None)
        finally:
            os.close(parent_fd)


def load_provider_urls(
    argv: list[str], *, codex_home: Path | None = None
) -> dict[str, str]:
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config_path = home / "config.toml"
    config: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    profile: str | None = None
    for index, argument in enumerate(argv):
        if argument in {"-p", "--profile"} and index + 1 < len(argv):
            profile = argv[index + 1]
        elif argument.startswith("--profile="):
            profile = argument.split("=", 1)[1]
    if profile:
        profile_path = home / f"{profile}.config.toml"
        if profile_path.exists():
            with profile_path.open("rb") as stream:
                config = _merge_config(config, tomllib.load(stream))
    providers = {
        name: str(value["base_url"])
        for name, value in config.get("model_providers", {}).items()
        if isinstance(value, dict) and value.get("base_url")
    }
    for index, argument in enumerate(argv):
        if argument in {"-c", "--config"} and index + 1 < len(argv):
            override = argv[index + 1]
        elif argument.startswith("--config="):
            override = argument.split("=", 1)[1]
        else:
            continue
        key, separator, value = override.partition("=")
        if not separator:
            continue
        if key == "model_provider":
            config["model_provider"] = value.strip('"')
        elif key.startswith("model_providers.") and key.endswith(".base_url"):
            name = key[len("model_providers.") : -len(".base_url")]
            providers[name] = value.strip('"')
    return providers


def _serve_proxy(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--upstream", action="append", default=[])
    options = parser.parse_args(arguments)
    upstreams = dict(item.split("=", 1) for item in options.upstream)
    proxy = CapacityRetryProxy(upstreams)
    proxy.start()
    print(f"READY {proxy.port}", flush=True)
    try:
        _wait_for_parent_exit(options.parent_pid)
        return 0
    finally:
        proxy.stop()


def run_wrapper(argv: list[str]) -> int:
    real = os.environ.get(
        "CODEX_REAL_EXECUTABLE",
        "/home/gnen/.codex/packages/standalone/current/bin/codex",
    )
    providers = load_provider_urls(argv)
    if not providers:
        os.execv(real, [real, *argv])
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_serve",
        "--parent-pid",
        str(os.getpid()),
    ]
    for name, upstream in providers.items():
        command.extend(("--upstream", f"{name}={upstream}"))
    proxy = subprocess.Popen(
        command, stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        ready = proxy.stdout.readline().strip() if proxy.stdout else ""
        if not ready.startswith("READY "):
            raise RuntimeError("capacity retry proxy failed to start")
        port = int(ready.split()[1])
        overrides = [
            item
            for name in providers
            for item in (
                "-c",
                f'model_providers.{name}.base_url="http://127.0.0.1:{port}/{quote(name, safe="")}"',
            )
        ]
        os.execv(real, [real, *overrides, *argv])
    finally:
        if proxy.poll() is None:
            proxy.terminate()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_serve":
        return _serve_proxy(sys.argv[2:])
    return run_wrapper(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())

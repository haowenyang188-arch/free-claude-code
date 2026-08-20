"""Read-only MCP connectivity diagnostics for Codex/WSL environments."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import tomllib
from collections.abc import Mapping
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
_PROTOCOL_VERSION = "2025-11-25"
_MAX_RESPONSE_BYTES = 1024 * 1024


def _proxy_value_summary(value: str) -> dict[str, Any]:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        port = None
    return {
        "configured": True,
        "scheme": parsed.scheme or None,
        "host": parsed.hostname,
        "port": port,
        "has_credentials": bool(parsed.username or parsed.password),
    }


def summarize_proxy_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return proxy metadata without exposing URLs, credentials, or tokens."""
    environment = os.environ if environment is None else environment
    summary: dict[str, dict[str, Any]] = {}
    for key in _PROXY_KEYS:
        value = environment.get(key)
        if value is None:
            continue
        if key.upper() == "NO_PROXY":
            summary[key] = {"configured": bool(value), "value": value}
        elif value:
            summary[key] = _proxy_value_summary(value)
        else:
            summary[key] = {"configured": False}
    return summary


def check_proxy_connectivity(
    environment: Mapping[str, str] | None = None,
    timeout: float = 2.0,
) -> dict[str, dict[str, Any]]:
    """Check configured proxy host/ports without printing proxy URLs."""
    environment = os.environ if environment is None else environment
    results: dict[str, dict[str, Any]] = {}
    for key in _PROXY_KEYS:
        if key.upper() == "NO_PROXY" or not environment.get(key):
            continue
        value = environment[key]
        parsed = urlsplit(value)
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            port = None
        target = {"configured": True, "host": parsed.hostname, "port": port}
        if not parsed.hostname or not port:
            target.update({"status": "error", "classification": "invalid_proxy"})
            results[key] = target
            continue
        started = time.monotonic()
        try:
            connection = socket.create_connection(
                (parsed.hostname, port), timeout=timeout
            )
            connection.close()
        except TimeoutError:
            target.update({"status": "error", "classification": "connection_timeout"})
        except ConnectionRefusedError:
            target.update({"status": "error", "classification": "connection_refused"})
        except socket.gaierror:
            target.update({"status": "error", "classification": "dns_error"})
        except OSError:
            target.update({"status": "error", "classification": "connection_error"})
        else:
            target.update(
                {
                    "status": "ok",
                    "classification": "reachable",
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
            )
        results[key] = target
    return results


def parse_response_payload(body: str, content_type: str) -> dict[str, Any]:
    """Decode a JSON or Streamable HTTP SSE response into one JSON-RPC object."""
    body = body.strip()
    if not body:
        return {}
    if "text/event-stream" in content_type.lower() or body.startswith("event:"):
        data_lines = [
            line[5:].lstrip() for line in body.splitlines() if line.startswith("data:")
        ]
        for data in data_lines:
            if data and data != "[DONE]":
                payload = json.loads(data)
                if isinstance(payload, dict):
                    return payload
        raise ValueError("SSE response did not contain a JSON object")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("MCP response was not a JSON object")
    return payload


def load_codex_server_config(
    path: Path,
    server_name: str,
) -> dict[str, Any]:
    """Read only non-sensitive MCP server fields from a Codex TOML file."""
    if not path.exists():
        return {"configured": False, "reason": "config_not_found"}
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"configured": False, "reason": "config_unreadable", "error": str(exc)}

    servers = document.get("mcp_servers")
    section = servers.get(server_name) if isinstance(servers, dict) else None
    if not isinstance(section, dict):
        return {"configured": False, "reason": "server_not_found"}

    result: dict[str, Any] = {"configured": True}
    for key in ("url", "enabled", "startup_timeout_sec", "tool_timeout_sec"):
        value = section.get(key)
        if key == "url" and isinstance(value, str):
            result[key] = _safe_url(value)
        elif isinstance(value, (str, int, float, bool)):
            result[key] = value
    if "url" in section:
        result["transport"] = "streamable_http"
    elif "command" in section:
        result["transport"] = "stdio"
    else:
        result["transport"] = "unknown"
    return result


def probe_mcp_server(url: str, timeout: float, no_proxy: bool) -> dict[str, Any]:
    """Probe initialize, initialized, and tools/list against an MCP endpoint."""
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        return {
            "status": "error",
            "classification": "invalid_url",
            "handshake_completed": False,
            "url": _safe_url(url),
        }
    if parsed_url.username or parsed_url.password:
        return {
            "status": "error",
            "classification": "credentials_in_url_rejected",
            "handshake_completed": False,
            "url": _safe_url(url),
        }

    opener = build_opener(ProxyHandler({})) if no_proxy else build_opener()
    common_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": "mcp-preflight/0.1",
    }
    result: dict[str, Any] = {
        "url": _safe_url(url),
        "transport": "streamable_http",
        "proxy_mode": "direct" if no_proxy else "environment",
        "handshake_completed": False,
    }

    initialize = _post_jsonrpc(
        opener,
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-preflight", "version": "0.1"},
            },
        },
        common_headers,
        timeout,
        no_proxy,
    )
    result["initialize"] = _summarize_exchange(initialize)
    if initialize["status"] != "ok":
        return _with_exchange_error(result, initialize)

    initialize_payload = initialize.get("payload")
    if not isinstance(initialize_payload, dict) or "error" in initialize_payload:
        result["classification"] = (
            "mcp_jsonrpc_error" if initialize_payload else "invalid_mcp_response"
        )
        return result

    response_result = initialize_payload.get("result")
    if not isinstance(response_result, dict):
        result["classification"] = "invalid_initialize_response"
        return result

    response_headers = initialize["headers"]
    session_id = response_headers.get("mcp-session-id")
    protocol_version = (
        response_headers.get("mcp-protocol-version")
        or response_result.get("protocolVersion")
        or _PROTOCOL_VERSION
    )
    session_headers = {
        **common_headers,
        "Mcp-Protocol-Version": str(protocol_version),
    }
    if session_id:
        session_headers["Mcp-Session-Id"] = session_id

    initialized = _post_jsonrpc(
        opener,
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_headers,
        timeout,
        no_proxy,
    )
    result["initialized"] = _summarize_exchange(initialized)
    if initialized["status"] != "ok":
        return _with_exchange_error(result, initialized)

    tools_list = _post_jsonrpc(
        opener,
        url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        session_headers,
        timeout,
        no_proxy,
    )
    result["tools_list"] = _summarize_exchange(tools_list)
    if tools_list["status"] != "ok":
        return _with_exchange_error(result, tools_list)

    tools_payload = tools_list.get("payload")
    tool_result = (
        tools_payload.get("result") if isinstance(tools_payload, dict) else None
    )
    tools = tool_result.get("tools") if isinstance(tool_result, dict) else None
    if not isinstance(tools, list):
        result["classification"] = "invalid_tools_list_response"
        return result

    result.update(
        {
            "status": "ok",
            "classification": "mcp_ready",
            "handshake_completed": True,
            "session_id": session_id,
            "protocol_version": protocol_version,
            "tools": {
                "count": len(tools),
                "names": [
                    tool["name"]
                    for tool in tools
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str)
                ],
            },
        }
    )
    return result


def _safe_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "content-type",
        "mcp-protocol-version",
        "mcp-session-id",
        "server",
        "via",
        "x-vercel-mitigated",
    }
    return {key: value for key, value in headers.items() if key in allowed}


def _http_classification(status: int) -> str:
    if status == 401:
        return "http_unauthorized"
    if status == 403:
        return "http_forbidden"
    if status == 405:
        return "http_method_not_allowed"
    if status == 408:
        return "http_timeout"
    if status == 429:
        return "http_rate_limited"
    if status >= 500:
        return "http_server_error"
    return f"http_{status}"


def _post_jsonrpc(
    opener: Any,
    url: str,
    payload: dict[str, Any],
    headers: Mapping[str, str],
    timeout: float,
    no_proxy: bool,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(response.status)
            response_headers = {
                key.lower(): value for key, value in response.headers.items()
            }
            body = _read_response_body(
                response, response_headers.get("content-type", "")
            )
    except HTTPError as exc:
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        return {
            "status": "error",
            "classification": _http_classification(exc.code),
            "http_status": exc.code,
            "headers": _safe_response_headers(response_headers),
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    except TimeoutError:
        return {
            "status": "error",
            "classification": "proxy_timeout" if not no_proxy else "network_timeout",
            "http_status": None,
            "headers": {},
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    except URLError as exc:
        reason = str(exc.reason).lower()
        if "timed out" in reason or "timeout" in reason:
            classification = "proxy_timeout" if not no_proxy else "network_timeout"
        elif "refused" in reason or "failed to establish" in reason:
            classification = (
                "proxy_connection_refused"
                if not no_proxy
                else "network_connection_refused"
            )
        else:
            classification = "proxy_error" if not no_proxy else "network_error"
        return {
            "status": "error",
            "classification": classification,
            "http_status": None,
            "headers": {},
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    except (OSError, HTTPException) as exc:
        return {
            "status": "error",
            "classification": "network_error",
            "http_status": None,
            "headers": {},
            "error": type(exc).__name__,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }

    content_type = response_headers.get("content-type", "")
    try:
        response_payload = parse_response_payload(body, content_type) if body else {}
    except ValueError, json.JSONDecodeError:
        response_payload = None
    return {
        "status": "ok" if 200 <= status < 300 else "error",
        "classification": "ok" if 200 <= status < 300 else _http_classification(status),
        "http_status": status,
        "headers": _safe_response_headers(response_headers),
        "payload": response_payload,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def _read_response_body(response: Any, content_type: str) -> str:
    if "text/event-stream" not in content_type.lower():
        return response.read(_MAX_RESPONSE_BYTES).decode("utf-8", "replace")

    chunks: list[bytes] = []
    total = 0
    saw_data = False
    while total < _MAX_RESPONSE_BYTES:
        line = response.readline(min(64 * 1024, _MAX_RESPONSE_BYTES - total))
        if not line:
            break
        chunks.append(line)
        total += len(line)
        saw_data = saw_data or line.startswith(b"data:")
        if saw_data and line in {b"\n", b"\r\n"}:
            break
    return b"".join(chunks).decode("utf-8", "replace")


def _summarize_exchange(exchange: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: exchange[key]
        for key in ("status", "classification", "http_status", "headers", "duration_ms")
        if key in exchange
    }


def _with_exchange_error(
    result: dict[str, Any], exchange: Mapping[str, Any]
) -> dict[str, Any]:
    result["status"] = "error"
    result["classification"] = exchange.get("classification", "mcp_request_failed")
    result["http_status"] = exchange.get("http_status")
    return result


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        return "[invalid-url]"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        port = ""
    return f"{parsed.scheme}://{host}{port}{parsed.path or '/'}"


def _positive_timeout(value: str) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only MCP Streamable HTTP diagnostics for Codex/WSL."
    )
    parser.add_argument(
        "--url",
        default="https://developers.openai.com/mcp",
        help="MCP endpoint (default: OpenAI Developer Docs MCP)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".codex" / "config.toml",
        help="Codex config path",
    )
    parser.add_argument(
        "--server-name",
        default="openaiDeveloperDocs",
        help="MCP server name in Codex config",
    )
    parser.add_argument("--timeout", type=_positive_timeout, default=15.0)
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Use a direct opener without HTTP(S)/ALL_PROXY variables",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    mcp = probe_mcp_server(args.url, timeout=args.timeout, no_proxy=args.no_proxy)
    report = {
        "overall": "ok" if mcp.get("status") == "ok" else "error",
        "url": _safe_url(args.url),
        "proxy_mode": "direct" if args.no_proxy else "environment",
        "proxy_environment": summarize_proxy_environment(),
        "proxy_connectivity": check_proxy_connectivity(timeout=min(args.timeout, 2.0)),
        "codex": load_codex_server_config(args.config, args.server_name),
        "mcp": mcp,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(f"MCP preflight: {report['overall']}")
        print(f"Endpoint: {report['url']}")
        print(f"Proxy mode: {report['proxy_mode']}")
        print(f"MCP: {mcp.get('classification', 'not_run')}")
        if isinstance(mcp.get("tools"), dict):
            print(f"Tools: {mcp['tools'].get('count', 0)}")
        codex = report["codex"]
        print(f"Codex config: {codex.get('transport', codex.get('reason', 'unknown'))}")
    return 0 if report["overall"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

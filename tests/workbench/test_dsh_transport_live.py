"""Live integration tests for the DSH Typert transport probe.

These hit the REAL running DSH Desktop v2.0.3 (via loopback) and are skipped
cleanly when the Desktop is not reachable, so the suite never depends on the
Desktop being up for the unit tests to pass.

Discovery is dynamic: DSH Desktop.exe PIDs -> netstat listeners -> probe, so
the tests do NOT hardcode 43120 as a contract (it is only an observed value).
"""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from workbench.backend.agents.dsh_transport import (
    K_DSH_API,
    K_NOT_FOUND,
    K_PROTOCOL_ERROR,
    K_UNAUTHORIZED,
    _default_http_post,
    build_rpc_request,
    discover_endpoint,
    is_dsh_api_endpoint,
    parse_rpc_response,
    probe_endpoint,
)

_NETSTAT = "/mnt/c/Windows/System32/netstat.exe"
_TASKLIST = "/mnt/c/Windows/System32/tasklist.exe"

API_ENDPOINT = "127.0.0.1:43120"  # observed-stable default (not a contract)


def _dsh_listeners() -> list[str]:
    """Return 'addr:port' strings for every listener owned by DSH Desktop.exe.

    Uses Windows interop (netstat/tasklist) when available; empty on failure.
    """
    try:
        task = subprocess.run(
            [_TASKLIST, "/FI", "IMAGENAME eq DSH Desktop.exe"],
            capture_output=True, timeout=10,
        )
        task_stdout = task.stdout.decode("utf-8", errors="replace")
        pids = set(re.findall(r"\b(\d{3,6})\b", task_stdout))
        if not pids:
            return []
        net = subprocess.run(
            [_NETSTAT, "-ano"], capture_output=True, timeout=10,
        )
        net_stdout = net.stdout.decode("utf-8", errors="replace")
        out: list[str] = []
        for line in net_stdout.splitlines():
            m = re.match(r"\s*TCP\s+(\S+:\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", line)
            if m and m.group(2) in pids:
                out.append(m.group(1))
        return sorted(out)
    except (OSError, subprocess.SubprocessError):
        return []


def _api_endpoint() -> str | None:
    """Dynamically find the passing loopback API endpoint (or None)."""
    listeners = _dsh_listeners()
    candidates = [e for e in listeners if e.split(":", 1)[0] in {"127.0.0.1", "[::1]"}]
    if not candidates and is_dsh_api_endpoint(API_ENDPOINT):
        return API_ENDPOINT
    return discover_endpoint(candidates) if candidates else None


def _post(url: str, envelope: dict) -> tuple[int, dict | bytes]:
    status, body = _default_http_post(url, json.dumps(envelope).encode("utf-8"))
    try:
        return status, json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return status, body


API = _api_endpoint()
pytestmark = pytest.mark.skipif(
    API is None,
    reason="DSH Desktop v2.0.3 not reachable (start it, then re-run)",
)


def test_live_session_list_5x():
    """5 sequential session.list probes with fresh rpcId each time."""
    for i in range(5):
        req = build_rpc_request("session.list")
        status, body = _post(f"http://{API}/api/session.list", req)
        parsed = parse_rpc_response(json.dumps(body) if isinstance(body, dict) else body, req["rpcId"])
        assert status == 200, f"probe {i}: http {status}"
        assert parsed.kind == "ok", f"probe {i}: {parsed.kind} {parsed.error_code}"
        assert isinstance(parsed.value, dict) and "items" in parsed.value


def test_live_concurrent_rpcid_no_crossmatch():
    """5 concurrent session.list with distinct rpcIds; no response cross-match."""
    reqs = [build_rpc_request("session.list") for _ in range(5)]
    assert len({r["rpcId"] for r in reqs}) == 5

    def one(req: dict):
        status, body = _post(f"http://{API}/api/session.list", req)
        parsed = parse_rpc_response(json.dumps(body) if isinstance(body, dict) else body, req["rpcId"])
        return status, parsed.kind

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(one, reqs))
    assert all(status == 200 and kind == "ok" for status, kind in results)


def test_live_unknown_method_404():
    """Unknown URL path -> HTTP 404 'not found' (URL routing rejects)."""
    req = build_rpc_request("s0r.nonexistent")
    status, body = _post(f"http://{API}/api/s0r.nonexistent", req)
    assert status == 404
    assert body == b"not found" or "not found" in str(body)


def test_live_url_method_mismatch():
    """URL is authoritative: envelope.method != URL method -> bad-request."""
    req = build_rpc_request("host.describe")
    status, body = _post(f"http://{API}/api/session.list", req)
    parsed = parse_rpc_response(json.dumps(body) if isinstance(body, dict) else body, req["rpcId"])
    assert status == 200
    assert parsed.kind == K_PROTOCOL_ERROR
    assert parsed.error_code == "bad-request"
    assert "does not match path" in parsed.error_message


def test_live_envelope_negatives():
    """Malformed envelopes -> HTTP 200 + ok:false + bad-request (+ issues)."""
    cases = {
        "missing_type": {"rpcId": "n1", "method": "session.list", "payload": {"args": {}}},
        "wrong_type": {"type": "server-response", "rpcId": "n2", "method": "session.list", "payload": {"args": {}}},
        "missing_rpcid": {"type": "client-request", "method": "session.list", "payload": {"args": {}}},
        "payload_not_object": {"type": "client-request", "rpcId": "n4", "method": "session.list", "payload": "oops"},
    }
    for name, env in cases.items():
        expected = env.get("rpcId", "expect-placeholder")  # do NOT inject rpcId
        status, body = _post(f"http://{API}/api/session.list", env)
        parsed = parse_rpc_response(json.dumps(body) if isinstance(body, dict) else body, expected)
        assert status == 200, f"{name}: http {status}"
        assert parsed.kind in (K_PROTOCOL_ERROR, "rpcid_mismatch"), f"{name}: {parsed.kind}"
        if parsed.kind == K_PROTOCOL_ERROR:
            assert parsed.error_code == "bad-request", f"{name}: {parsed.error_code}"


def test_live_wechat_bridge_rejected():
    """The dsh-wechat-bridge listener (401) is rejected by the probe."""
    listeners = _dsh_listeners()
    api_addr = API.split(":", 1)[0]
    candidates = [
        e for e in listeners
        if e.startswith("127.0.0.1:") or e.startswith("[::1]:")
    ]
    other = [e for e in candidates if f"127.0.0.1:{API.split(':',1)[1]}" != e and f"[::1]:{API.split(':',1)[1]}" != e]
    if not other:
        pytest.skip("no non-API loopback DSH listener found to test")
    for endpoint in other:
        res = probe_endpoint(endpoint)
        if res.kind == K_UNAUTHORIZED:
            assert is_dsh_api_endpoint(endpoint) is False
            return
    pytest.skip("no 401-class listener found (wechat-bridge port may differ)")


def test_live_0_0_0_0_3082_not_formal():
    """0.0.0.0 listeners are never accepted by the identity predicate."""
    assert is_dsh_api_endpoint("0.0.0.0:3082") is False
    listeners = _dsh_listeners()
    zero = [e for e in listeners if e.startswith("0.0.0.0:")]
    assert zero, "expected at least the dsh-bridge 0.0.0.0 listener"
    # the loopback address of the proxy still speaks the protocol, but discovery
    # filters by bind address, so it is never selected as the formal endpoint.
    res = probe_endpoint(zero[0].replace("0.0.0.0", "127.0.0.1"))
    assert res.kind in (K_DSH_API, K_PROTOCOL_ERROR), res.kind


def test_live_discover_endpoint():
    """discover_endpoint picks the loopback API from the live listener set."""
    listeners = _dsh_listeners()
    loopback = [e for e in listeners if e.split(":", 1)[0] in {"127.0.0.1", "[::1]"}]
    got = discover_endpoint(loopback) if loopback else None
    assert got is not None
    assert is_dsh_api_endpoint(got) is True
    assert got.split(":", 1)[0] == "127.0.0.1" or got.startswith("[::1]")

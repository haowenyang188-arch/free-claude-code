"""Unit tests for the thin DSH Typert transport probe (no Desktop required).

Fixtures use the exact response bodies observed against DSH Desktop v2.0.3
(127.0.0.1:43120) during S0-R P2-b, so the classifications encode the REAL
server behaviour rather than an invented contract.
"""

from __future__ import annotations

import json

import pytest

from workbench.backend.agents.dsh_transport import (
    K_DSH_API,
    K_HTTP,
    K_NOT_FOUND,
    K_NOT_JSON,
    K_NOT_TYPERT,
    K_PROTOCOL_ERROR,
    K_RPCID_MISMATCH,
    K_UNAUTHORIZED,
    K_UNREACHABLE,
    ProbeUnreachable,
    build_rpc_request,
    discover_endpoint,
    is_dsh_api_endpoint,
    parse_rpc_response,
    probe_endpoint,
)

# --------------------------------------------------------------------------
# Observed real bodies (v2.0.3)
# --------------------------------------------------------------------------

OK_BODY = {
    "type": "server-response",
    "rpcId": "abc",
    "result": {"ok": True, "value": {"items": [{"sessionId": "session-x"}]}},
}
UNKNOWN_METHOD_404 = b"not found"
MISSING_TYPE_BODY = {
    "type": "server-response",
    "rpcId": "abc",
    "result": {
        "ok": False,
        "error": {
            "code": "bad-request",
            "message": "invalid client-request message",
            "details": {
                "issues": [
                    {
                        "code": "invalid_value",
                        "values": ["client-request"],
                        "path": ["type"],
                        "message": 'Invalid input: expected "client-request"',
                    }
                ]
            },
        },
    },
}
MISSING_RPCID_BODY = {
    "type": "server-response",
    "rpcId": "invalid-request",  # server placeholder when request rpcId missing
    "result": {
        "ok": False,
        "error": {
            "code": "bad-request",
            "message": "invalid client-request message",
            "details": {"issues": []},
        },
    },
}
PAYLOAD_WRONG_BODY = {
    "type": "server-response",
    "rpcId": "abc",
    "result": {
        "ok": False,
        "error": {
            "code": "bad-request",
            "message": "invalid payload for session.list",
            "details": {
                "issues": [
                    {
                        "expected": "object",
                        "code": "invalid_type",
                        "path": [],
                        "message": "Invalid input: expected object, received string",
                    }
                ]
            },
        },
    },
}
METHOD_MISMATCH_BODY = {
    "type": "server-response",
    "rpcId": "abc",
    "result": {
        "ok": False,
        "error": {
            "code": "bad-request",
            "message": 'method "host.describe" does not match path "session.list"',
            "details": {"issues": []},
        },
    },
}
WECHAT_BRIDGE_401_BODY = b'{"ok":false,"error":"unauthorized"}'
HTML_200_BODY = b"<html><body>not dsh</body></html>"


def fake_post(status: int, body: bytes):
    """Build an injectable HttpPost returning a fixed (status, body)."""

    def _post(url: str, _body: bytes) -> tuple[int, bytes]:
        return status, body

    return _post


def echo(result: dict, status: int = 200):
    """HttpPost that echoes the REQUEST's rpcId back in a server-response."""

    def _post(_url: str, body: bytes) -> tuple[int, bytes]:
        req = json.loads(body)
        resp = {
            "type": "server-response",
            "rpcId": req.get("rpcId", "invalid-request"),
            "result": result,
        }
        return status, json.dumps(resp).encode("utf-8")

    return _post


OK_RESULT = {"ok": True, "value": {"items": [{"sessionId": "session-x"}]}}
BAD_REQUEST_RESULT = {
    "ok": False,
    "error": {
        "code": "bad-request",
        "message": "invalid client-request message",
        "details": {"issues": []},
    },
}


# --------------------------------------------------------------------------
# build_rpc_request
# --------------------------------------------------------------------------

class TestBuildRpcRequest:
    def test_valid_defaults(self):
        req = build_rpc_request("session.list")
        assert req["type"] == "client-request"
        assert req["method"] == "session.list"
        assert req["payload"] == {"args": {}}
        assert req["rpcId"] and isinstance(req["rpcId"], str)

    def test_custom_rpc_id_and_payload(self):
        req = build_rpc_request("session.prompt", payload={"args": {"sessionId": "s1"}}, rpc_id="r1")
        assert req["rpcId"] == "r1"
        assert req["payload"] == {"args": {"sessionId": "s1"}}

    def test_invalid_method_rejected(self):
        with pytest.raises(ValueError):
            build_rpc_request("")
        with pytest.raises(ValueError):
            build_rpc_request("session/list")

    def test_unique_rpc_ids(self):
        assert build_rpc_request("session.list")["rpcId"] != build_rpc_request("session.list")["rpcId"]


# --------------------------------------------------------------------------
# parse_rpc_response
# --------------------------------------------------------------------------

class TestParseRpcResponse:
    def test_ok_true(self):
        parsed = parse_rpc_response(json.dumps(OK_BODY), expected_rpc_id="abc")
        assert parsed.kind == "ok"
        assert parsed.ok is True
        assert parsed.value == {"items": [{"sessionId": "session-x"}]}

    def test_rpcid_mismatch(self):
        body = dict(OK_BODY, rpcId="WRONG")
        parsed = parse_rpc_response(json.dumps(body), expected_rpc_id="abc")
        assert parsed.kind == K_RPCID_MISMATCH

    def test_missing_type(self):
        parsed = parse_rpc_response(json.dumps(MISSING_TYPE_BODY), expected_rpc_id="abc")
        assert parsed.kind == K_PROTOCOL_ERROR
        assert parsed.error_code == "bad-request"

    def test_server_placeholder_rpcid_rejected(self):
        # server answers "invalid-request" when the request had no rpcId
        parsed = parse_rpc_response(json.dumps(MISSING_RPCID_BODY), expected_rpc_id="abc")
        assert parsed.kind == K_RPCID_MISMATCH

    def test_payload_wrong(self):
        parsed = parse_rpc_response(json.dumps(PAYLOAD_WRONG_BODY), expected_rpc_id="abc")
        assert parsed.kind == K_PROTOCOL_ERROR
        assert parsed.error_code == "bad-request"
        assert "issues" in parsed.error_message or parsed.error_message

    def test_method_mismatch_message(self):
        parsed = parse_rpc_response(json.dumps(METHOD_MISMATCH_BODY), expected_rpc_id="abc")
        assert parsed.kind == K_PROTOCOL_ERROR
        assert "does not match path" in parsed.error_message

    def test_not_json_html(self):
        parsed = parse_rpc_response(b"<html>hello</html>", expected_rpc_id="abc")
        assert parsed.kind == K_NOT_JSON

    def test_not_typert_json(self):
        parsed = parse_rpc_response(b'{"ok":false,"error":"unauthorized"}', expected_rpc_id="abc")
        assert parsed.kind == K_NOT_TYPERT

    def test_non_dict_json(self):
        assert parse_rpc_response(b"[1,2,3]", expected_rpc_id="abc").kind == K_NOT_TYPERT


# --------------------------------------------------------------------------
# probe_endpoint classification
# --------------------------------------------------------------------------

class TestProbeEndpoint:
    def test_dsh_api_accepted(self):
        res = probe_endpoint("127.0.0.1:43120", http_post=echo(OK_RESULT))
        assert res.kind == K_DSH_API
        assert res.http_status == 200
        assert res.ok is True

    def test_401_rejected(self):
        res = probe_endpoint("127.0.0.1:50118", http_post=fake_post(401, WECHAT_BRIDGE_401_BODY))
        assert res.kind == K_UNAUTHORIZED

    def test_404_rejected(self):
        res = probe_endpoint("127.0.0.1:9999", http_post=fake_post(404, UNKNOWN_METHOD_404))
        assert res.kind == K_NOT_FOUND

    def test_html_200_rejected(self):
        res = probe_endpoint("127.0.0.1:9999", http_post=fake_post(200, HTML_200_BODY))
        assert res.kind == K_NOT_JSON

    def test_ok_false_rejected(self):
        res = probe_endpoint("127.0.0.1:43120", http_post=echo(BAD_REQUEST_RESULT))
        assert res.kind == K_PROTOCOL_ERROR
        assert res.ok is False
        assert res.error_code == "bad-request"

    def test_rpcid_mismatch_rejected(self):
        wrong = {
            "type": "server-response",
            "rpcId": "WRONG",
            "result": OK_RESULT,
        }
        res = probe_endpoint("127.0.0.1:43120", http_post=fake_post(200, json.dumps(wrong).encode("utf-8")))
        assert res.kind == K_RPCID_MISMATCH

    def test_unreachable(self):
        def boom(_url: str, _body: bytes):
            raise ProbeUnreachable("connection refused")

        res = probe_endpoint("127.0.0.1:1", http_post=boom)
        assert res.kind == K_UNREACHABLE

    def test_http_500(self):
        res = probe_endpoint("127.0.0.1:9999", http_post=fake_post(500, b"boom"))
        assert res.kind == K_HTTP
        assert res.http_status == 500


# --------------------------------------------------------------------------
# is_dsh_api_endpoint
# --------------------------------------------------------------------------

class TestIsDshApiEndpoint:
    def test_loopback_ok_true(self):
        assert is_dsh_api_endpoint("127.0.0.1:43120", http_post=echo(OK_RESULT)) is True

    def test_non_loopback_never_passes(self):
        # even when the probe would succeed, 0.0.0.0 listeners are excluded
        assert is_dsh_api_endpoint("0.0.0.0:3082", http_post=echo(OK_RESULT)) is False

    def test_401_false(self):
        assert is_dsh_api_endpoint("127.0.0.1:50118", http_post=fake_post(401, WECHAT_BRIDGE_401_BODY)) is False


# --------------------------------------------------------------------------
# discover_endpoint
# --------------------------------------------------------------------------

class TestDiscoverEndpoint:
    def test_picks_passing_loopback(self):
        def selective(url: str, body: bytes) -> tuple[int, bytes]:
            if "50118" in url:
                return 401, WECHAT_BRIDGE_401_BODY
            return echo(OK_RESULT)(url, body)

        got = discover_endpoint(["127.0.0.1:50118", "127.0.0.1:43120"], http_post=selective)
        assert got == "127.0.0.1:43120"

    def test_excludes_0_0_0_0_candidate(self):
        def selective(url: str, body: bytes) -> tuple[int, bytes]:
            if "43120" in url:
                return echo(OK_RESULT)(url, body)
            return 200, b"<html>x</html>"

        got = discover_endpoint(["0.0.0.0:3082", "127.0.0.1:43120"], http_post=selective)
        assert got == "127.0.0.1:43120"

    def test_none_when_all_fail(self):
        res = discover_endpoint(["127.0.0.1:50118"], http_post=fake_post(401, WECHAT_BRIDGE_401_BODY))
        assert res is None

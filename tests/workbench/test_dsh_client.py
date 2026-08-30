"""Unit tests for the P2-c DshClient (fake HTTP/discover — no Desktop needed)."""

from __future__ import annotations

import json

import pytest

from workbench.backend.agents.dsh_transport import (
    DshClient,
    DshTransportError,
    ProbeUnreachable,
    parse_rpc_response,
)

OK_VALUE = {"items": []}


def http_ok(value):
    """HttpPost returning a server-response echoing the request rpcId."""

    def _post(_url: str, body: bytes) -> tuple[int, bytes]:
        req = json.loads(body)
        resp = {
            "type": "server-response",
            "rpcId": req["rpcId"],
            "result": {"ok": True, "value": value},
        }
        return 200, json.dumps(resp).encode("utf-8")

    return _post


def http_error(code: str, message: str):
    def _post(_url: str, body: bytes) -> tuple[int, bytes]:
        req = json.loads(body)
        resp = {
            "type": "server-response",
            "rpcId": req["rpcId"],
            "result": {
                "ok": False,
                "error": {"code": code, "message": message, "details": {"issues": []}},
            },
        }
        return 200, json.dumps(resp).encode("utf-8")

    return _post


class TestCreateSession:
    def test_returns_external_id(self):
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_ok({"sessionId": "session-x", "agentPreset": "liangshen"}))
        assert client.create_session(cwd=r"C:\fixture", agent_preset="liangshen") == "session-x"

    def test_sends_cwd_arg(self):
        sent = {}

        def capture(_url: str, body: bytes) -> tuple[int, bytes]:
            sent["body"] = json.loads(body)
            return http_ok({"sessionId": "s1"})(_url, body)

        client = DshClient(endpoint="127.0.0.1:43120", http_post=capture)
        client.create_session(cwd=r"C:\tmp\fixture")
        assert sent["body"]["method"] == "session.create"
        # WIRE CONTRACT (verified vs running v2.0.3): payload = args directly,
        # NOT wrapped in payload.args.
        assert sent["body"]["payload"] == {"cwd": r"C:\tmp\fixture"}

    def test_workspace_id_and_cwd_conflict(self):
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_ok({"sessionId": "s1"}))
        with pytest.raises(ValueError):
            client.create_session(cwd="x", workspace_id="y")

    def test_protocol_error_raises_with_code(self):
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_error("bad-request", "invalid payload"))
        with pytest.raises(DshTransportError) as exc:
            client.create_session(cwd="x")
        assert exc.value.code == "bad-request"

    def test_invalid_response_value(self):
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_ok({"nope": 1}))
        with pytest.raises(DshTransportError) as exc:
            client.create_session(cwd="x")
        assert exc.value.code == "create-invalid"


class TestPromptCancelArchive:
    def test_prompt_sends_content(self):
        sent = {}

        def capture(_url: str, body: bytes) -> tuple[int, bytes]:
            sent["body"] = json.loads(body)
            return http_ok({"accepted": True})(_url, body)

        client = DshClient(endpoint="127.0.0.1:43120", http_post=capture)
        client.prompt("session-x", "hello")
        assert sent["body"]["method"] == "session.prompt"
        payload = sent["body"]["payload"]
        assert payload["sessionId"] == "session-x"
        assert payload["mode"] == "queue"
        assert payload["content"] == [{"type": "text", "text": "hello"}]

    def test_prompt_not_accepted(self):
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_ok({"accepted": False}))
        with pytest.raises(DshTransportError) as exc:
            client.prompt("session-x", "hi")
        assert exc.value.code == "prompt-not-accepted"

    def test_cancel(self):
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_ok({"accepted": True}))
        assert client.cancel("session-x") == {"accepted": True}

    def test_archive_session(self):
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_ok({"archivedSessionIds": ["session-x"]}))
        assert client.archive_session("session-x") == ["session-x"]


class TestListGetHistory:
    def test_list_sessions(self):
        value = {"items": [{"sessionId": "a", "running": False}, {"sessionId": "b", "running": True}]}
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_ok(value))
        rows = client.list_sessions()
        assert [r["sessionId"] for r in rows] == ["a", "b"]

    def test_get_session_filters(self):
        value = {"items": [{"sessionId": "a"}, {"sessionId": "b"}]}
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_ok(value))
        assert client.get_session("b")["sessionId"] == "b"
        assert client.get_session("zzz") is None

    def test_history(self):
        value = {"events": [{"event": {"type": "turn/start", "seq": 1}}], "hasMore": False}
        client = DshClient(endpoint="127.0.0.1:43120", http_post=http_ok(value))
        events = client.history("session-x")
        assert events[0]["event"]["type"] == "turn/start"


class TestWireContractRegression:
    """Wire-contract regression: RPC args are the payload itself (flattened).

    Empirically verified against the running DSH Desktop v2.0.3:
      * flattened prompt/cancel payload  -> ok:true accepted:true
      * payload.args wrapped payload     -> bad-request (fields undefined)
    A `payload.args` wrapper must never reappear for session methods.
    """

    def _capture_call(self, method: str, args: dict) -> dict:
        sent = {}

        def capture(_url: str, body: bytes) -> tuple[int, bytes]:
            sent["body"] = json.loads(body)
            return 200, json.dumps(
                {"type": "server-response", "rpcId": sent["body"]["rpcId"], "result": {"ok": True, "value": {}}}
            ).encode()

        DshClient(endpoint="127.0.0.1:43120", http_post=capture).call(method, args)
        return sent["body"]

    def test_prompt_payload_flattened(self):
        env = self._capture_call("session.prompt", {"sessionId": "s1", "mode": "queue", "content": [{"type": "text", "text": "x"}]})
        assert env["payload"] == {"sessionId": "s1", "mode": "queue", "content": [{"type": "text", "text": "x"}]}
        assert "args" not in env["payload"], "payload.args wrapper must not reappear"

    def test_cancel_payload_flattened(self):
        env = self._capture_call("session.cancel", {"sessionId": "s1"})
        assert env["payload"] == {"sessionId": "s1"}
        assert "args" not in env["payload"]

    def test_create_payload_flattened(self):
        env = self._capture_call("session.create", {"cwd": r"C:\fix", "agentPreset": "liangshen"})
        assert env["payload"] == {"cwd": r"C:\fix", "agentPreset": "liangshen"}
        assert "args" not in env["payload"]

    def test_list_payload_empty(self):
        env = self._capture_call("session.list", {})
        assert env["payload"] == {}
        assert "args" not in env["payload"]


class TestStaleRediscover:
    def test_unreachable_then_rediscover_then_ok(self):
        calls: list[str] = []

        def responder(url: str, body: bytes) -> tuple[int, bytes]:
            calls.append(url)
            if "43120" in url:
                raise ProbeUnreachable("connection refused")
            return http_ok({"items": []})(url, body)

        def discover() -> str:
            return "127.0.0.1:43999"

        client = DshClient(endpoint="127.0.0.1:43120", discover=discover, http_post=responder)
        assert client.list_sessions() == []
        # first attempt hit the stale endpoint, then rediscovered and retried
        assert any("43120" in c for c in calls)
        assert any("43999" in c for c in calls)
        assert client.endpoint == "127.0.0.1:43999"

    def test_discover_returns_none_raises(self):
        def responder(_url: str, _body: bytes):
            raise ProbeUnreachable("refused")

        def discover() -> str | None:
            return None

        client = DshClient(endpoint="127.0.0.1:43120", discover=discover, http_post=responder)
        with pytest.raises(DshTransportError) as exc:
            client.list_sessions()
        assert exc.value.code == "unreachable"

    def test_protocol_error_does_not_rediscover(self):
        discovered = []

        def discover() -> str:
            discovered.append(1)
            return "127.0.0.1:43120"

        client = DshClient(
            endpoint="127.0.0.1:43120",
            discover=discover,
            http_post=http_error("bad-request", "invalid payload"),
        )
        with pytest.raises(DshTransportError) as exc:
            client.list_sessions()
        assert exc.value.code == "bad-request"
        assert discovered == [], "protocol errors must not trigger rediscovery"

    def test_no_endpoint_no_discover(self):
        client = DshClient()
        with pytest.raises(DshTransportError) as exc:
            client.list_sessions()
        assert exc.value.code == "no-endpoint"


class TestStandardizeFrame:
    def test_session_event(self):
        from workbench.backend.agents.dsh_transport import DshClient as C

        frame = {
            "type": "session/event",
            "sessionId": "session-x",
            "event": {"type": "turn/end", "seq": 7, "time": 1234.5, "data": {"reason": {"kind": "completed"}}},
        }
        out = C._standardize_frame(frame)
        assert out["raw_type"] == "session/event"
        assert out["session_id"] == "session-x"
        assert out["sequence"] == 7
        assert out["timestamp"] == 1234.5
        assert out["payload"] is frame

    def test_subscribed(self):
        from workbench.backend.agents.dsh_transport import DshClient as C

        out = C._standardize_frame({"type": "session/subscribed", "sessionId": "session-x", "lastSeq": -1})
        assert out["raw_type"] == "session/subscribed"
        assert out["sequence"] == -1

    def test_unknown_frame_passthrough(self):
        from workbench.backend.agents.dsh_transport import DshClient as C

        out = C._standardize_frame({"type": "something/new", "sessionId": "s"})
        assert out["raw_type"] == "something/new"

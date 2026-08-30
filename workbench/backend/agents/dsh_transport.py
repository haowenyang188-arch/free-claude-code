"""Thin Typert RPC transport + endpoint probe for DSH Desktop v2.0.3.

FIRST_PARTY_INTERNAL_PROTOCOL (S0-R P1/P2/P2-c): DSH Desktop's packaged client
(``dsh-client-connection``) talks to its own loopback webserver via
``POST /api/<namespace>.<method>`` with a JSON Typert envelope and receives
``server-response`` envelopes; live events arrive on a downlink-only WebSocket
at ``/api/events.mux`` (frames wrapped in ``server-request`` envelopes).

Scope:
  * P2-b (frozen, do not extend): build_rpc_request / parse_rpc_response /
    probe_endpoint / discover_endpoint / is_dsh_api_endpoint.
  * P2-c: DshClient (endpoint management + stale-rediscover) and session
    lifecycle methods list_sessions / get_session / create_session / prompt /
    cancel / archive_session / history / subscribe_events.

Isolation rule: every Typert/WebSocket wire detail lives in THIS module.  The
SOP Engine and adapters only ever see endpoints, session ids and parsed values.

Empirical contract notes (v2.0.3, observed on the loopback webserver):
  * URL path is the dispatch authority; envelope.method must equal the path.
  * Protocol/schema errors are HTTP 200 with ``result.ok == false``
    (code "bad-request" + ``details.issues``); unknown URL paths are 404.
  * A missing ``rpcId`` is echoed back as the placeholder ``"invalid-request"``.
  * 43120 is an OBSERVED-STABLE port only — it is deliberately NOT referenced
    by any production discovery path (discovery scans the live process
    listeners; the port may change across restarts).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Iterable

# --------------------------------------------------------------------------
# Wire types (Typert envelope)
# --------------------------------------------------------------------------

REQUEST_TYPE = "client-request"
RESPONSE_TYPE = "server-response"

DEFAULT_PROBE_TIMEOUT = 5.0
DEFAULT_CALL_TIMEOUT = 30.0

# Kinds that mean "this endpoint is stale / not the DSH API" and therefore
# justify an auto-rediscover + retry (protocol errors do NOT: they mean the
# endpoint speaks Typert and we sent something invalid).
STALE_KINDS = frozenset(
    {
        "unreachable",
        "unauthorized",
        "not_found",
        "http_error",
        "not_json",
        "not_typert",
        "rpcid_mismatch",
    }
)


class ProbeUnreachable(RuntimeError):
    """The endpoint could not be contacted (refused, reset, or timeout)."""


class DshTransportError(RuntimeError):
    """A DSH API call failed at the transport/protocol level."""

    def __init__(self, code: str, message: str, endpoint: str | None = None):
        super().__init__(f"{code}: {message} (endpoint={endpoint})")
        self.code = code
        self.endpoint = endpoint


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedResponse:
    """Result of parsing + validating one server-response body."""

    kind: str  # "ok" | "protocol_error" | "not_json" | "not_typert" | "rpcid_mismatch"
    ok: bool | None = None
    value: Any = None
    error_code: str | None = None
    error_message: str | None = None
    rpc_id: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    """Classification of one endpoint probe (what ``probe_endpoint`` returns)."""

    endpoint: str
    kind: str
    http_status: int | None = None
    ok: bool | None = None
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


# Probe result kinds
K_DSH_API = "dsh_api"
K_PROTOCOL_ERROR = "dsh_protocol_error"
K_UNAUTHORIZED = "unauthorized"
K_NOT_FOUND = "not_found"
K_HTTP = "http_error"
K_NOT_JSON = "not_json"
K_NOT_TYPERT = "not_typert"
K_RPCID_MISMATCH = "rpcid_mismatch"
K_UNREACHABLE = "unreachable"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


# --------------------------------------------------------------------------
# Envelope construction / parsing (P2-b, frozen)
# --------------------------------------------------------------------------

def build_rpc_request(
    method: str,
    payload: dict[str, Any] | None = None,
    rpc_id: str | None = None,
) -> dict[str, Any]:
    """Build a Typert client-request envelope for ``method``."""
    if not isinstance(method, str) or not method or "/" in method:
        raise ValueError(f"method must be a non-empty 'ns.method' string, got {method!r}")
    return {
        "type": REQUEST_TYPE,
        "rpcId": rpc_id or uuid.uuid4().hex,
        "method": method,
        "payload": payload if payload is not None else {"args": {}},
    }


def parse_rpc_response(body: bytes | str, expected_rpc_id: str) -> ParsedResponse:
    """Parse and validate one DSH server-response body."""
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = body
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ParsedResponse(kind=K_NOT_JSON)
    if not isinstance(data, dict):
        return ParsedResponse(kind=K_NOT_TYPERT)
    if data.get("type") != RESPONSE_TYPE:
        return ParsedResponse(kind=K_NOT_TYPERT)
    rpc_id = data.get("rpcId")
    if rpc_id != expected_rpc_id:
        return ParsedResponse(kind=K_RPCID_MISMATCH, rpc_id=rpc_id)
    result = data.get("result")
    if not isinstance(result, dict) or "ok" not in result:
        return ParsedResponse(kind=K_NOT_TYPERT, rpc_id=rpc_id)
    if result["ok"] is True:
        return ParsedResponse(kind="ok", ok=True, value=result.get("value"), rpc_id=rpc_id)
    error = result.get("error")
    if isinstance(error, dict):
        return ParsedResponse(
            kind=K_PROTOCOL_ERROR,
            ok=False,
            error_code=error.get("code"),
            error_message=error.get("message"),
            rpc_id=rpc_id,
        )
    return ParsedResponse(kind=K_PROTOCOL_ERROR, ok=False, rpc_id=rpc_id)


# --------------------------------------------------------------------------
# HTTP carrier (injectable so unit tests never touch the network)
# --------------------------------------------------------------------------

HttpPost = Callable[[str, bytes], tuple[int, bytes]]


def _default_http_post(url: str, body: bytes, timeout: float = DEFAULT_CALL_TIMEOUT) -> tuple[int, bytes]:
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise ProbeUnreachable(f"{url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProbeUnreachable(f"{url}: timeout") from exc
    except OSError as exc:
        raise ProbeUnreachable(f"{url}: {exc}") from exc


# --------------------------------------------------------------------------
# Probe / discovery (P2-b, frozen)
# --------------------------------------------------------------------------

def normalize_endpoint(endpoint: str) -> str:
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    return f"http://{endpoint}"


def endpoint_host(endpoint: str) -> str:
    url = normalize_endpoint(endpoint)
    host = url.split("://", 1)[1].split("/", 1)[0]
    if ":" in host and not host.startswith("["):
        host, _, _ = host.rpartition(":")
    return host.strip("[]").lower()


def is_loopback(endpoint: str) -> bool:
    return endpoint_host(endpoint) in LOOPBACK_HOSTS


def probe_endpoint(endpoint: str, *, http_post: HttpPost | None = None) -> ProbeResult:
    """Probe one endpoint with a read-only ``session.list`` client-request."""
    post = http_post or _default_http_post
    envelope = build_rpc_request("session.list")
    url = f"{normalize_endpoint(endpoint)}/api/session.list"
    try:
        status, body = post(url, json.dumps(envelope).encode("utf-8"))
    except ProbeUnreachable as exc:
        return ProbeResult(endpoint=endpoint, kind=K_UNREACHABLE, error_message=str(exc))
    parsed = parse_rpc_response(body, expected_rpc_id=envelope["rpcId"])
    if parsed.kind == "ok":
        return ProbeResult(
            endpoint=endpoint, kind=K_DSH_API, http_status=status,
            ok=True, details={"value_is_object": isinstance(parsed.value, dict)},
        )
    if status == 401:
        return ProbeResult(endpoint=endpoint, kind=K_UNAUTHORIZED, http_status=status)
    if status == 404:
        return ProbeResult(endpoint=endpoint, kind=K_NOT_FOUND, http_status=status)
    if status != 200:
        return ProbeResult(endpoint=endpoint, kind=K_HTTP, http_status=status)
    if parsed.kind == K_PROTOCOL_ERROR:
        return ProbeResult(
            endpoint=endpoint, kind=K_PROTOCOL_ERROR, http_status=status,
            ok=False, error_code=parsed.error_code, error_message=parsed.error_message,
        )
    if parsed.kind == K_RPCID_MISMATCH:
        return ProbeResult(endpoint=endpoint, kind=K_RPCID_MISMATCH, http_status=status)
    if parsed.kind == K_NOT_TYPERT:
        return ProbeResult(endpoint=endpoint, kind=K_NOT_TYPERT, http_status=status)
    if parsed.kind == K_NOT_JSON:
        return ProbeResult(endpoint=endpoint, kind=K_NOT_JSON, http_status=status)
    return ProbeResult(endpoint=endpoint, kind=K_NOT_TYPERT, http_status=status)


def is_dsh_api_endpoint(endpoint: str, *, http_post: HttpPost | None = None) -> bool:
    """PROTOCOL-identity predicate (loopback + 200 + server-response + rpcId + ok:true)."""
    if not is_loopback(endpoint):
        return False
    return probe_endpoint(endpoint, http_post=http_post).kind == K_DSH_API


def discover_endpoint(
    candidates: Iterable[str],
    *,
    http_post: HttpPost | None = None,
) -> str | None:
    """Pick the first loopback candidate that passes ``is_dsh_api_endpoint``.

    Never selects non-loopback candidates (e.g. ``0.0.0.0:3082``).  Candidates
    are expected to be derived from the current DSH Desktop process listeners
    (process identity), NOT guessed from a fixed port.
    """
    ordered = sorted(set(candidates), key=lambda e: (endpoint_host(e) != "127.0.0.1", e))
    for endpoint in ordered:
        if is_dsh_api_endpoint(endpoint, http_post=http_post):
            return endpoint
    return None


def discover_dsh_desktop_endpoint() -> str | None:
    """PROCESS-identity discovery: find the live DSH Desktop API endpoint.

    Scans the current ``DSH Desktop.exe`` process tree's TCP listeners
    (Windows interop via netstat/tasklist), keeps only loopback-bound
    listeners (0.0.0.0 listeners such as the dsh-bridge proxy are excluded at
    the source), probes each with ``session.list``, and returns the first
    passing endpoint.  Returns None when the Desktop is not running or not
    reachable.  No fixed port is assumed anywhere.
    """
    import re
    import subprocess

    netstat = "/mnt/c/Windows/System32/netstat.exe"
    tasklist = "/mnt/c/Windows/System32/tasklist.exe"
    try:
        task = subprocess.run(
            [tasklist, "/FI", "IMAGENAME eq DSH Desktop.exe"],
            capture_output=True, timeout=10,
        )
        pids = set(re.findall(r"\b(\d{3,6})\b", task.stdout.decode("utf-8", errors="replace")))
        if not pids:
            return None
        net = subprocess.run([netstat, "-ano"], capture_output=True, timeout=10)
        listeners: list[str] = []
        for line in net.stdout.decode("utf-8", errors="replace").splitlines():
            m = re.match(r"\s*TCP\s+(\S+:\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", line)
            if m and m.group(2) in pids:
                addr, port = m.group(1).rsplit(":", 1)
                if addr in ("127.0.0.1", "[::1]"):
                    listeners.append(f"{addr}:{port}")
        return discover_endpoint(listeners)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None


# --------------------------------------------------------------------------
# P2-c: DshClient — endpoint management + session lifecycle
# --------------------------------------------------------------------------

class DshClient:
    """A small DSH API client bound to one live Desktop instance.

    ``discover`` (callable returning an endpoint string or None) enables
    stale-endpoint auto-recovery: when a call fails because the recorded
    endpoint is no longer the DSH API (unreachable / 401 / 404 / HTML / ...),
    the client rediscover once and retries the call.  Protocol errors
    (bad-request etc.) never trigger rediscovery — they mean the endpoint
    speaks Typert and the request itself was invalid.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        discover: Callable[[], str | None] | None = None,
        http_post: HttpPost | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._discover = discover
        self._http_post = http_post or _default_http_post

    # -- endpoint management -------------------------------------------------

    @property
    def endpoint(self) -> str | None:
        return self._endpoint

    def rediscover(self) -> str | None:
        """Re-run discovery and update the bound endpoint. Returns it or None."""
        if self._discover is None:
            raise DshTransportError("no-discover", "no discover callable configured", self._endpoint)
        self._endpoint = self._discover()
        return self._endpoint

    def _resolve_endpoint(self) -> str:
        if self._endpoint is None:
            if self._discover is None:
                raise DshTransportError("no-endpoint", "no endpoint and no discover callable")
            self._endpoint = self._discover()
        if self._endpoint is None:
            raise DshTransportError("discover-failed", "no DSH API endpoint found")
        return self._endpoint

    # -- core call -----------------------------------------------------------

    def call(self, method: str, args: dict[str, Any] | None = None) -> Any:
        """One Typert unary call; returns the parsed ``value`` on success.

        Wire contract (verified against the running v2.0.3): the RPC args are
        the PAYLOAD ITSELF (flattened) — ``payload = args``.  The server
        validates the payload directly against the domain schema
        (``sessions.schema.js`` payload schemas describe the args object), and
        the first-party client's ``callUnary`` sends ``payload`` as-is.  A
        ``payload.args`` wrapper is REJECTED for methods with required fields
        (session.prompt/cancel: bad-request, fields undefined at top level).
        """
        attempts = 0
        while attempts < 2:
            attempts += 1
            endpoint = self._resolve_endpoint()
            envelope = build_rpc_request(method, args if args is not None else {})
            url = f"{normalize_endpoint(endpoint)}/api/{method}"
            try:
                status, body = self._http_post(url, json.dumps(envelope).encode("utf-8"))
            except ProbeUnreachable as exc:
                if attempts < 2 and self._try_recover():
                    continue
                raise DshTransportError("unreachable", str(exc), endpoint) from exc
            parsed = parse_rpc_response(body, expected_rpc_id=envelope["rpcId"])
            if parsed.kind == "ok":
                return parsed.value
            if parsed.kind in STALE_KINDS and attempts < 2 and self._try_recover():
                continue
            raise DshTransportError(
                parsed.error_code or parsed.kind,
                parsed.error_message or "",
                endpoint,
            )

    def _try_recover(self) -> bool:
        if self._discover is None:
            return False
        new = self.rediscover()
        return new is not None

    # -- session operations (wire schema from sessions.schema.js, v2.0.3) ----

    def list_sessions(self) -> list[dict[str, Any]]:
        value = self.call("session.list", {})
        items = value.get("items") if isinstance(value, dict) else None
        return list(items) if isinstance(items, list) else []

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        for row in self.list_sessions():
            if row.get("sessionId") == session_id:
                return row
        return None

    def create_session(
        self,
        *,
        cwd: str | None = None,
        workspace_id: str | None = None,
        agent_preset: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Create a session; returns the real DSH external session id.

        ``workspace_id`` and ``cwd`` are mutually exclusive (server schema).
        """
        if cwd is not None and workspace_id is not None:
            raise ValueError("session.create accepts workspaceId or cwd, not both")
        args: dict[str, Any] = {}
        if cwd is not None:
            args["cwd"] = cwd
        if workspace_id is not None:
            args["workspaceId"] = workspace_id
        if agent_preset is not None:
            args["agentPreset"] = agent_preset
        if session_id is not None:
            args["sessionId"] = session_id
        value = self.call("session.create", args)
        if not isinstance(value, dict) or not value.get("sessionId"):
            raise DshTransportError("create-invalid", f"session.create returned {value!r}", self._endpoint)
        return value["sessionId"]

    def prompt(self, session_id: str, text: str, *, mode: str = "queue") -> dict[str, Any]:
        """Queue a text prompt on an existing session; returns the value dict."""
        value = self.call(
            "session.prompt",
            {
                "sessionId": session_id,
                "mode": mode,
                "content": [{"type": "text", "text": text}],
            },
        )
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise DshTransportError("prompt-not-accepted", f"session.prompt returned {value!r}", self._endpoint)
        return value

    def cancel(self, session_id: str) -> dict[str, Any]:
        value = self.call("session.cancel", {"sessionId": session_id})
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise DshTransportError("cancel-not-accepted", f"session.cancel returned {value!r}", self._endpoint)
        return value

    def archive_session(self, session_id: str) -> list[str]:
        """Official archival cleanup (workspace.archiveSession)."""
        value = self.call("workspace.archiveSession", {"sessionId": session_id})
        if not isinstance(value, dict) or not isinstance(value.get("archivedSessionIds"), list):
            raise DshTransportError("archive-invalid", f"workspace.archiveSession returned {value!r}", self._endpoint)
        return value["archivedSessionIds"]

    def history(self, session_id: str, *, before_seq: int | None = None, max_messages: int | None = None) -> list[dict[str, Any]]:
        """Fetch session.history (events over HTTP — useful for verification)."""
        args: dict[str, Any] = {"sessionId": session_id}
        if before_seq is not None:
            args["beforeSeq"] = before_seq
        if max_messages is not None:
            args["maxMessages"] = max_messages
        value = self.call("session.history", args)
        events = value.get("events") if isinstance(value, dict) else None
        return list(events) if isinstance(events, list) else []

    # -- events.mux (downlink-only WebSocket) -------------------------------

    async def subscribe_events(
        self,
        session_id: str | None = None,
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield standardized event frames from ``/api/events.mux``.

        Each yielded dict: ``{"raw_type", "session_id", "sequence",
        "timestamp", "payload"}``.  The stream is downlink-only (no
        subscription message is sent; the server pushes a ``session/subscribed``
        baseline frame then live ``session/event`` frames).  Optional
        ``session_id`` filters frames client-side.
        """
        import websockets  # available in the repo test venv (15.0.1)

        endpoint = self._resolve_endpoint()
        ws_url = normalize_endpoint(endpoint).replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        ws_url = f"{ws_url}/api/events.mux"
        try:
            async with websockets.connect(ws_url, open_timeout=max(timeout or DEFAULT_CALL_TIMEOUT, 5.0)) as ws:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(msg, dict) or msg.get("type") != "server-request":
                        continue
                    frame = msg.get("payload")
                    if not isinstance(frame, dict):
                        continue
                    standardized = self._standardize_frame(frame)
                    if standardized is None:
                        continue
                    if session_id is not None and standardized["session_id"] != session_id:
                        continue
                    yield standardized
        except OSError as exc:
            raise DshTransportError("ws-unreachable", str(exc), endpoint) from exc

    @staticmethod
    def _standardize_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
        ftype = frame.get("type")
        if ftype == "session/event":
            event = frame.get("event")
            if not isinstance(event, dict):
                return None
            return {
                "raw_type": ftype,
                "session_id": frame.get("sessionId"),
                "sequence": event.get("seq"),
                "timestamp": event.get("time"),
                "payload": frame,
            }
        if ftype in ("session/subscribed", "approval/requested", "approval/resolved"):
            return {
                "raw_type": ftype,
                "session_id": frame.get("sessionId"),
                "sequence": frame.get("lastSeq"),
                "timestamp": None,
                "payload": frame,
            }
        return {
            "raw_type": ftype,
            "session_id": frame.get("sessionId"),
            "sequence": None,
            "timestamp": None,
            "payload": frame,
        }

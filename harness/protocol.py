"""Strict newline-delimited JSON-RPC 2.0 framing for DeepSeek Harness.

The official runtime speaks one JSON-RPC frame per UTF-8 line over stdio.  The
functions here validate that boundary before a future process bridge handles
the method-specific payloads.  No plugin loading, subprocess, or network code
is included.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

DEFAULT_MAX_FRAME_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_NESTING_DEPTH = 128
MAX_MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_MAX_NESTING_DEPTH = 512

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonRpcId = str | int | float


class JsonRpcProtocolError(ValueError):
    """Base error for invalid JSON-RPC input or output."""


class MalformedFrameError(JsonRpcProtocolError):
    """Raised when a line cannot be decoded as one JSON frame."""


class FrameTooLargeError(JsonRpcProtocolError):
    """Raised before parsing when a UTF-8 frame exceeds the configured limit."""


class InvalidMessageError(JsonRpcProtocolError):
    """Raised when valid JSON is not a valid JSON-RPC 2.0 message."""


def _valid_id(value: object) -> bool:
    return (
        isinstance(value, str)
        or (isinstance(value, (int, float)) and not isinstance(value, bool))
    ) and (not isinstance(value, float) or math.isfinite(value))


def _validate_method(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidMessageError("method must be a non-empty string")
    if any(ord(char) < 0x20 for char in value):
        raise InvalidMessageError("method must not contain control characters")
    return value


def _validate_value(value: object, *, depth: int, max_depth: int) -> None:
    if depth > max_depth:
        raise InvalidMessageError(
            f"JSON value is nested deeper than the {max_depth}-level limit"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidMessageError("JSON numbers must be finite")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidMessageError("JSON object keys must be strings")
            _validate_value(item, depth=depth + 1, max_depth=max_depth)
    elif isinstance(value, list):
        for item in value:
            _validate_value(item, depth=depth + 1, max_depth=max_depth)
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        raise InvalidMessageError("JSON values must use JSON-compatible types")


def _validate_limit(value: int, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    if value > maximum:
        raise ValueError(f"{field} must not exceed {maximum}")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


@dataclass(frozen=True, slots=True)
class JsonRpcError:
    """JSON-RPC error object carried by an error response."""

    code: int
    message: str
    data: JsonValue | None = None

    def __post_init__(self) -> None:
        if isinstance(self.code, bool) or not isinstance(self.code, int):
            raise InvalidMessageError("error.code must be an integer")
        if not isinstance(self.message, str):
            raise InvalidMessageError("error.message must be a string")
        _validate_value(self.data, depth=0, max_depth=DEFAULT_MAX_NESTING_DEPTH)

    def to_mapping(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "code": self.code,
            "message": self.message,
        }
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    """A client-to-server JSON-RPC request with an id and method."""

    request_id: JsonRpcId
    method: str
    params: JsonValue | None = None

    @property
    def kind(self) -> Literal["request"]:
        return "request"

    def __post_init__(self) -> None:
        if not _valid_id(self.request_id):
            raise InvalidMessageError("request id must be a string or finite number")
        _validate_method(self.method)
        _validate_value(self.params, depth=0, max_depth=DEFAULT_MAX_NESTING_DEPTH)

    def to_mapping(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": self.method,
        }
        if self.params is not None:
            result["params"] = self.params
        return result


@dataclass(frozen=True, slots=True)
class JsonRpcNotification:
    """A server notification with a method and no request id."""

    method: str
    params: JsonValue | None = None

    @property
    def kind(self) -> Literal["notification"]:
        return "notification"

    def __post_init__(self) -> None:
        _validate_method(self.method)
        _validate_value(self.params, depth=0, max_depth=DEFAULT_MAX_NESTING_DEPTH)

    def to_mapping(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"jsonrpc": "2.0", "method": self.method}
        if self.params is not None:
            result["params"] = self.params
        return result


@dataclass(frozen=True, slots=True)
class JsonRpcResponse:
    """A JSON-RPC response carrying exactly one result or error on the wire."""

    request_id: JsonRpcId
    result: JsonValue | None = None
    error: JsonRpcError | None = None

    @property
    def kind(self) -> Literal["response"]:
        return "response"

    def __post_init__(self) -> None:
        if not _valid_id(self.request_id):
            raise InvalidMessageError("response id must be a string or finite number")
        if self.result is not None and self.error is not None:
            raise InvalidMessageError("response cannot contain both result and error")
        if self.error is not None and not isinstance(self.error, JsonRpcError):
            raise InvalidMessageError("response.error must be a JsonRpcError")
        _validate_value(self.result, depth=0, max_depth=DEFAULT_MAX_NESTING_DEPTH)

    def to_mapping(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"jsonrpc": "2.0", "id": self.request_id}
        if self.error is not None:
            result["error"] = self.error.to_mapping()
        else:
            result["result"] = self.result
        return result


type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse


def _message_from_mapping(
    value: Mapping[str, object], *, max_depth: int
) -> JsonRpcMessage:
    if value.get("jsonrpc") != "2.0":
        raise InvalidMessageError("jsonrpc must be exactly '2.0'")
    _validate_value(value, depth=0, max_depth=max_depth)

    has_id = "id" in value
    has_method = "method" in value
    has_result = "result" in value
    has_error = "error" in value

    if has_method:
        method = _validate_method(value["method"])
        if has_result or has_error:
            raise InvalidMessageError(
                "a request or notification cannot contain result/error"
            )
        if has_id:
            request_id = value["id"]
            if not _valid_id(request_id):
                raise InvalidMessageError(
                    "request id must be a string or finite number"
                )
            return JsonRpcRequest(
                request_id=cast(JsonRpcId, request_id),
                method=method,
                params=cast(JsonValue | None, value.get("params")),
            )
        return JsonRpcNotification(
            method=method,
            params=cast(JsonValue | None, value.get("params")),
        )

    if not has_id:
        raise InvalidMessageError("message must contain method or id")
    if has_result == has_error:
        raise InvalidMessageError("response must contain exactly one result or error")
    response_id = value["id"]
    if not _valid_id(response_id):
        raise InvalidMessageError("response id must be a string or finite number")
    if has_error:
        error_value = value["error"]
        if not isinstance(error_value, Mapping):
            raise InvalidMessageError("response.error must be an object")
        error_mapping = cast(Mapping[str, object], error_value)
        code = error_mapping.get("code")
        message = error_mapping.get("message")
        if isinstance(code, bool) or not isinstance(code, int):
            raise InvalidMessageError("error.code must be an integer")
        if not isinstance(message, str):
            raise InvalidMessageError("error.message must be a string")
        data = cast(JsonValue | None, error_mapping.get("data"))
        return JsonRpcResponse(
            request_id=cast(JsonRpcId, response_id),
            error=JsonRpcError(code=code, message=message, data=data),
        )
    return JsonRpcResponse(
        request_id=cast(JsonRpcId, response_id),
        result=cast(JsonValue, value["result"]),
    )


def decode_message(
    line: str | bytes,
    *,
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    max_depth: int = DEFAULT_MAX_NESTING_DEPTH,
) -> JsonRpcMessage:
    """Decode and validate one newline-delimited JSON-RPC frame.

    A single trailing ``\n`` or ``\r\n`` is accepted.  Blank lines, embedded
    newlines, malformed JSON, duplicate keys, non-standard numbers, and frames
    larger than ``max_bytes`` are rejected.
    """

    max_bytes = _validate_limit(max_bytes, "max_bytes", MAX_MAX_FRAME_BYTES)
    max_depth = _validate_limit(max_depth, "max_depth", MAX_MAX_NESTING_DEPTH)
    if isinstance(line, bytes):
        raw = line
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedFrameError("frame is not valid UTF-8") from exc
    elif isinstance(line, str):
        text = line
        raw = text.encode("utf-8")
    else:
        raise MalformedFrameError("frame must be str or bytes")

    if len(raw) > max_bytes:
        raise FrameTooLargeError(
            f"frame is {len(raw)} bytes; maximum is {max_bytes} bytes"
        )
    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    if "\n" in text or "\r" in text:
        raise MalformedFrameError("JSON-RPC frame must contain exactly one single line")
    if not text.strip():
        raise MalformedFrameError("JSON-RPC frame must not be blank")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise MalformedFrameError(f"invalid JSON frame: {exc}") from exc
    if not isinstance(value, Mapping):
        raise InvalidMessageError("JSON-RPC frame must be an object")
    try:
        return _message_from_mapping(value, max_depth=max_depth)
    except RecursionError as exc:
        raise InvalidMessageError("JSON-RPC frame is too deeply nested") from exc


def encode_message(
    message: JsonRpcMessage | Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    max_depth: int = DEFAULT_MAX_NESTING_DEPTH,
) -> str:
    """Validate and encode one message as compact UTF-8 JSON plus ``\n``."""

    max_bytes = _validate_limit(max_bytes, "max_bytes", MAX_MAX_FRAME_BYTES)
    max_depth = _validate_limit(max_depth, "max_depth", MAX_MAX_NESTING_DEPTH)
    if isinstance(message, (JsonRpcRequest, JsonRpcNotification, JsonRpcResponse)):
        value: Mapping[str, object] = message.to_mapping()
    elif isinstance(message, Mapping):
        value = message
    else:
        raise InvalidMessageError("message must be a JSON-RPC dataclass or object")

    # Re-run the same envelope validation used by decode so callers cannot
    # bypass the boundary by passing a raw mapping to the encoder.
    normalized = _message_from_mapping(value, max_depth=max_depth)
    if isinstance(normalized, (JsonRpcRequest, JsonRpcNotification, JsonRpcResponse)):
        value = normalized.to_mapping()
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidMessageError(f"message is not JSON serializable: {exc}") from exc
    encoded = f"{text}\n"
    size = len(encoded.encode("utf-8"))
    if size > max_bytes:
        raise FrameTooLargeError(f"frame is {size} bytes; maximum is {max_bytes} bytes")
    return encoded


# Friendly aliases for bridge code that names the wire operation explicitly.
decode_line = decode_message
encode_line = encode_message


__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_MAX_NESTING_DEPTH",
    "FrameTooLargeError",
    "InvalidMessageError",
    "JsonRpcError",
    "JsonRpcId",
    "JsonRpcMessage",
    "JsonRpcNotification",
    "JsonRpcProtocolError",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonValue",
    "MalformedFrameError",
    "decode_line",
    "decode_message",
    "encode_line",
    "encode_message",
]

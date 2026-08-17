"""Unit tests for the newline-delimited DeepSeek Harness JSON-RPC contract."""

from __future__ import annotations

import json

import pytest

from harness.protocol import (
    DEFAULT_MAX_FRAME_BYTES,
    FrameTooLargeError,
    InvalidMessageError,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    MalformedFrameError,
    decode_message,
    encode_message,
)


def test_request_round_trip_preserves_object_params() -> None:
    message = JsonRpcRequest(
        request_id="req-1",
        method="initialize",
        params={"cwd": "/tmp/workspace", "maxTokens": 32},
    )

    encoded = encode_message(message)
    decoded = decode_message(encoded)

    assert encoded.endswith("\n")
    assert isinstance(decoded, JsonRpcRequest)
    assert decoded.request_id == "req-1"
    assert decoded.method == "initialize"
    assert decoded.params == {"cwd": "/tmp/workspace", "maxTokens": 32}


def test_notification_round_trip_has_no_id() -> None:
    message = JsonRpcNotification(method="session.event", params={"sessionId": "s1"})

    decoded = decode_message(encode_message(message))

    assert isinstance(decoded, JsonRpcNotification)
    assert decoded.method == "session.event"
    assert decoded.params == {"sessionId": "s1"}


def test_response_round_trip_supports_null_result() -> None:
    message = JsonRpcResponse(request_id=7, result=None)

    decoded = decode_message(encode_message(message))

    assert isinstance(decoded, JsonRpcResponse)
    assert decoded.request_id == 7
    assert decoded.result is None
    assert decoded.error is None


def test_error_response_round_trip_preserves_code_message_and_data() -> None:
    message = JsonRpcResponse(
        request_id="req-2",
        error=JsonRpcError(
            code=-32601, message="method not found", data={"method": "x"}
        ),
    )

    decoded = decode_message(encode_message(message))

    assert isinstance(decoded, JsonRpcResponse)
    assert decoded.error == message.error
    assert decoded.result is None


@pytest.mark.parametrize(
    "line",
    [
        "",
        "not json\n",
        "[]\n",
        '{"jsonrpc":"1.0","id":1,"result":null}\n',
        '{"jsonrpc":"2.0","id":1}\n',
        '{"jsonrpc":"2.0","method":1}\n',
        '{"jsonrpc":"2.0","id":true,"method":"x"}\n',
        '{"jsonrpc":"2.0","id":null,"method":"x"}\n',
        '{"jsonrpc":"2.0","method":"x","result":1}\n',
        '{"jsonrpc":"2.0","id":1,"method":"x","result":1}\n',
        '{"jsonrpc":"2.0","id":1,"result":1,"error":{"code":-1,"message":"x"}}\n',
        '{"jsonrpc":"2.0","id":1,"error":{"code":-1}}\n',
    ],
)
def test_malformed_or_invalid_frames_are_rejected(line: str) -> None:
    with pytest.raises((MalformedFrameError, InvalidMessageError)):
        decode_message(line)


def test_duplicate_json_keys_are_rejected() -> None:
    line = '{"jsonrpc":"2.0","id":1,"method":"x","method":"y"}\n'

    with pytest.raises(MalformedFrameError, match="duplicate"):
        decode_message(line)


def test_non_finite_numbers_are_rejected() -> None:
    line = '{"jsonrpc":"2.0","id":1,"result":NaN}\n'

    with pytest.raises(MalformedFrameError, match="constant"):
        decode_message(line)


def test_oversized_input_is_rejected_before_json_parsing() -> None:
    line = (
        '{"jsonrpc":"2.0","method":"x","params":"'
        + ("a" * DEFAULT_MAX_FRAME_BYTES)
        + '"}\n'
    )

    with pytest.raises(FrameTooLargeError):
        decode_message(line)


def test_multibyte_size_is_measured_in_utf8_bytes() -> None:
    line = '{"jsonrpc":"2.0","method":"x","params":"' + "中" * 100 + '"}\n'

    with pytest.raises(FrameTooLargeError):
        decode_message(line, max_bytes=100)


def test_embedded_newline_is_rejected_but_one_trailing_newline_is_allowed() -> None:
    valid = '{"jsonrpc":"2.0","method":"x"}\n'
    assert isinstance(decode_message(valid), JsonRpcNotification)

    with pytest.raises(MalformedFrameError, match="single line"):
        decode_message(valid + valid)


def test_deeply_nested_json_is_rejected() -> None:
    nested: object = "leaf"
    for _ in range(20):
        nested = [nested]
    line = json.dumps({"jsonrpc": "2.0", "method": "x", "params": nested}) + "\n"

    with pytest.raises(InvalidMessageError, match="nested"):
        decode_message(line, max_depth=8)


def test_encode_rejects_invalid_message_and_control_method_names() -> None:
    with pytest.raises(InvalidMessageError):
        encode_message({"jsonrpc": "2.0"})

    with pytest.raises(InvalidMessageError, match="method"):
        encode_message(JsonRpcNotification(method="bad\nmethod"))


def test_decode_accepts_bytes_and_carriage_return_newline() -> None:
    decoded = decode_message(b'{"jsonrpc":"2.0","method":"x"}\r\n')

    assert isinstance(decoded, JsonRpcNotification)

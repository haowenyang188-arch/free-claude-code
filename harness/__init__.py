"""Optional DeepSeek Harness integration contracts.

The package deliberately contains only standard-library contracts.  Runtime
process management and FastAPI integration live in sibling modules so native
mode can import the application without starting (or requiring) Node.
"""

from .config import HarnessConfig, HarnessConfigError
from .protocol import (
    FrameTooLargeError,
    InvalidMessageError,
    JsonRpcError,
    JsonRpcMessage,
    JsonRpcNotification,
    JsonRpcProtocolError,
    JsonRpcRequest,
    JsonRpcResponse,
    MalformedFrameError,
    decode_message,
    encode_message,
)

__all__ = [
    "FrameTooLargeError",
    "HarnessConfig",
    "HarnessConfigError",
    "InvalidMessageError",
    "JsonRpcError",
    "JsonRpcMessage",
    "JsonRpcNotification",
    "JsonRpcProtocolError",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "MalformedFrameError",
    "decode_message",
    "encode_message",
]

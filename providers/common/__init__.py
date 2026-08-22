"""Shared provider utilities."""

from .context_compression import (
    MessageCompressor,
    MessageDeduplicator,
    TokenCounter,
    optimize_messages,
)
from .error_mapping import append_request_id, get_user_facing_error_message, map_error
from .heuristic_tool_parser import HeuristicToolParser
from .message_converter import (
    AnthropicToOpenAIConverter,
    build_base_request_body,
    get_block_attr,
    get_block_type,
)
from .performance import PerformanceMonitor, SimpleCache, async_memoize, memoize
from .sse_builder import ContentBlockManager, SSEBuilder, map_stop_reason
from .structured_output import (
    ResponseFormatter,
    StructuredOutputBuilder,
    StructuredOutputParser,
    create_tool_for_schema,
    enforce_structured_output,
)
from .think_parser import ContentChunk, ContentType, ThinkTagParser
from .utils import set_if_not_none

__all__ = [
    "AnthropicToOpenAIConverter",
    "ContentBlockManager",
    "ContentChunk",
    "ContentType",
    "HeuristicToolParser",
    "MessageCompressor",
    "MessageDeduplicator",
    "PerformanceMonitor",
    "ResponseFormatter",
    "SSEBuilder",
    "SimpleCache",
    "StructuredOutputBuilder",
    "StructuredOutputParser",
    "ThinkTagParser",
    "TokenCounter",
    "append_request_id",
    "async_memoize",
    "build_base_request_body",
    "create_tool_for_schema",
    "enforce_structured_output",
    "get_block_attr",
    "get_block_type",
    "get_user_facing_error_message",
    "map_error",
    "map_stop_reason",
    "memoize",
    "optimize_messages",
    "set_if_not_none",
]

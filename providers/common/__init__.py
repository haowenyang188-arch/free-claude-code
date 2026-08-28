"""Shared provider utilities."""

from .agent_planning import (
    DependencyAnalyzer,
    ExecutionPlan,
    ExecutionScheduler,
    Task,
    TaskDecomposer,
    TaskPriority,
    TaskStatus,
    create_plan_from_description,
)
from .context_compression import (
    MessageCompressor,
    MessageDeduplicator,
    TokenCounter,
    optimize_messages,
)
from .error_mapping import append_request_id, get_user_facing_error_message, map_error
from .heuristic_tool_parser import HeuristicToolParser
from .identity import IDENTITY_FIELDS, RuntimeIdentity, normalize_identity
from .memory_integration import (
    ConversationMemory,
    MemoryClient,
    extract_entities_from_messages,
    inject_memory_into_system,
)
from .message_converter import (
    AnthropicToOpenAIConverter,
    build_base_request_body,
    get_block_attr,
    get_block_type,
)
from .performance import PerformanceMonitor, SimpleCache, async_memoize, memoize
from .rag_engine import GitNexusRAG, build_knowledge_summary, inject_rag_context
from .self_improvement import (
    AutoOptimizer,
    CodeQualityAnalyzer,
    SelfImprovementLoop,
    analyze_performance_bottlenecks,
)
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
    "IDENTITY_FIELDS",
    "AnthropicToOpenAIConverter",
    "AutoOptimizer",
    "CodeQualityAnalyzer",
    "ContentBlockManager",
    "ContentChunk",
    "ContentType",
    "ConversationMemory",
    "DependencyAnalyzer",
    "ExecutionPlan",
    "ExecutionScheduler",
    "GitNexusRAG",
    "HeuristicToolParser",
    "MemoryClient",
    "MessageCompressor",
    "MessageDeduplicator",
    "PerformanceMonitor",
    "ResponseFormatter",
    "RuntimeIdentity",
    "SSEBuilder",
    "SelfImprovementLoop",
    "SimpleCache",
    "StructuredOutputBuilder",
    "StructuredOutputParser",
    "Task",
    "TaskDecomposer",
    "TaskPriority",
    "TaskStatus",
    "ThinkTagParser",
    "TokenCounter",
    "analyze_performance_bottlenecks",
    "append_request_id",
    "async_memoize",
    "build_base_request_body",
    "build_knowledge_summary",
    "create_plan_from_description",
    "create_tool_for_schema",
    "enforce_structured_output",
    "extract_entities_from_messages",
    "get_block_attr",
    "get_block_type",
    "get_user_facing_error_message",
    "inject_memory_into_system",
    "inject_rag_context",
    "map_error",
    "map_stop_reason",
    "memoize",
    "normalize_identity",
    "optimize_messages",
    "set_if_not_none",
]

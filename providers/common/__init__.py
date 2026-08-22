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
    # Message conversion
    "AnthropicToOpenAIConverter",
    "build_base_request_body",
    "get_block_attr",
    "get_block_type",
    # Context compression
    "MessageCompressor",
    "MessageDeduplicator",
    "TokenCounter",
    "optimize_messages",
    # Structured output
    "ResponseFormatter",
    "StructuredOutputBuilder",
    "StructuredOutputParser",
    "create_tool_for_schema",
    "enforce_structured_output",
    # Performance
    "PerformanceMonitor",
    "SimpleCache",
    "async_memoize",
    "memoize",
    # Agent planning
    "DependencyAnalyzer",
    "ExecutionPlan",
    "ExecutionScheduler",
    "Task",
    "TaskDecomposer",
    "TaskPriority",
    "TaskStatus",
    "create_plan_from_description",
    # Memory integration
    "ConversationMemory",
    "MemoryClient",
    "extract_entities_from_messages",
    "inject_memory_into_system",
    # RAG engine
    "GitNexusRAG",
    "build_knowledge_summary",
    "inject_rag_context",
    # Self improvement
    "AutoOptimizer",
    "CodeQualityAnalyzer",
    "SelfImprovementLoop",
    "analyze_performance_bottlenecks",
    # Error handling
    "append_request_id",
    "get_user_facing_error_message",
    "map_error",
    # SSE building
    "ContentBlockManager",
    "SSEBuilder",
    "map_stop_reason",
    # Parsing
    "HeuristicToolParser",
    "ThinkTagParser",
    "ContentChunk",
    "ContentType",
    # Utils
    "set_if_not_none",
]

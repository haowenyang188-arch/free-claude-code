"""Context compression utilities for token optimization.

Provides intelligent message compression, deduplication, and summarization
to reduce token usage while preserving semantic meaning.
"""

import json
from typing import Any

try:
    import tiktoken
except ImportError:
    tiktoken = None


class TokenCounter:
    """Count tokens in text using tiktoken or fallback estimation."""

    def __init__(self, model: str = "gpt-4"):
        """Initialize token counter.

        Args:
            model: Model name for tiktoken encoding (default: gpt-4)
        """
        self.encoding = None
        if tiktoken:
            try:
                self.encoding = tiktoken.encoding_for_model(model)
            except KeyError:
                # Fallback to cl100k_base for unknown models
                self.encoding = tiktoken.get_encoding("cl100k_base")

    def count(self, text: str) -> int:
        """Count tokens in text.

        Args:
            text: Text to count tokens for

        Returns:
            Token count
        """
        if self.encoding:
            return len(self.encoding.encode(text))
        # Fallback: estimate 1 token ≈ 4 characters
        return len(text) // 4

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        """Count tokens in a list of messages.

        Args:
            messages: List of message dictionaries

        Returns:
            Total token count
        """
        total = 0
        for msg in messages:
            # Count role
            total += 4  # Role overhead
            # Count content
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            total += self.count(item.get("text", ""))
                        elif item.get("type") == "image_url":
                            # Image tokens (rough estimate)
                            total += 85  # Base image token cost
            # Count tool_calls
            if "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    total += self.count(json.dumps(tc))
        return total


class MessageDeduplicator:
    """Remove duplicate or redundant messages while preserving order."""

    @staticmethod
    def deduplicate(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove consecutive duplicate messages.

        Args:
            messages: List of messages

        Returns:
            Deduplicated message list
        """
        if not messages:
            return []

        result = [messages[0]]
        for msg in messages[1:]:
            # Only deduplicate if role and content are identical
            if msg.get("role") != result[-1].get("role") or msg.get(
                "content"
            ) != result[-1].get("content"):
                result.append(msg)

        return result


class MessageCompressor:
    """Compress messages to reduce token usage."""

    def __init__(self, max_tokens: int = 100000):
        """Initialize message compressor.

        Args:
            max_tokens: Maximum allowed tokens (default: 100k)
        """
        self.max_tokens = max_tokens
        self.counter = TokenCounter()

    def compress(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compress messages if they exceed max_tokens.

        Strategy:
        1. Remove duplicates
        2. Truncate long text blocks
        3. Summarize old messages if needed

        Args:
            messages: List of messages

        Returns:
            Compressed message list
        """
        # Step 1: Remove duplicates
        deduped = MessageDeduplicator.deduplicate(messages)

        # Step 2: Check token count
        token_count = self.counter.count_messages(deduped)
        if token_count <= self.max_tokens:
            return deduped

        # Step 3: Truncate from the beginning (keep recent messages)
        # Always keep system message and last N messages
        if deduped and deduped[0].get("role") == "system":
            system_msg = [deduped[0]]
            other_msgs = deduped[1:]
        else:
            system_msg = []
            other_msgs = deduped

        # Keep last messages that fit within budget
        result = system_msg.copy()
        current_tokens = self.counter.count_messages(system_msg)

        # Add messages from end, respecting token budget
        for msg in reversed(other_msgs):
            msg_tokens = self.counter.count_messages([msg])
            if current_tokens + msg_tokens <= self.max_tokens:
                result.insert(len(system_msg), msg)
                current_tokens += msg_tokens
            else:
                # Add truncation notice
                if system_msg:
                    notice = {
                        "role": "system",
                        "content": f"[Earlier messages truncated to fit {self.max_tokens} token limit]",
                    }
                    result.insert(len(system_msg), notice)
                break

        return result


class ContentSummarizer:
    """Summarize long content blocks."""

    @staticmethod
    def should_summarize(text: str, threshold: int = 2000) -> bool:
        """Check if text should be summarized.

        Args:
            text: Text to check
            threshold: Character threshold (default: 2000)

        Returns:
            True if text should be summarized
        """
        return len(text) > threshold

    @staticmethod
    def truncate(text: str, max_length: int = 1000) -> str:
        """Truncate text to max_length.

        Args:
            text: Text to truncate
            max_length: Maximum length

        Returns:
            Truncated text with ellipsis
        """
        if len(text) <= max_length:
            return text
        # Keep beginning and end
        keep = max_length // 2 - 10
        return f"{text[:keep]}\n\n[... {len(text) - 2 * keep} characters truncated ...]\n\n{text[-keep:]}"


def optimize_messages(
    messages: list[dict[str, Any]],
    max_tokens: int = 100000,
    deduplicate: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Optimize messages for token efficiency.

    Args:
        messages: List of messages
        max_tokens: Maximum token limit
        deduplicate: Whether to remove duplicates

    Returns:
        Tuple of (optimized_messages, stats)
    """
    original_count = len(messages)
    counter = TokenCounter()
    original_tokens = counter.count_messages(messages)

    # Apply optimizations
    result = messages
    if deduplicate:
        result = MessageDeduplicator.deduplicate(result)

    compressor = MessageCompressor(max_tokens=max_tokens)
    result = compressor.compress(result)

    # Calculate stats
    final_count = len(result)
    final_tokens = counter.count_messages(result)

    stats = {
        "original_messages": original_count,
        "final_messages": final_count,
        "messages_removed": original_count - final_count,
        "original_tokens": original_tokens,
        "final_tokens": final_tokens,
        "tokens_saved": original_tokens - final_tokens,
        "compression_ratio": (
            1 - final_tokens / original_tokens if original_tokens > 0 else 0
        ),
    }

    return result, stats

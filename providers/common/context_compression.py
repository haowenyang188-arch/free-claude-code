"""Context compression utilities for token optimization.

Provides intelligent message compression, deduplication, and summarization
to reduce token usage while preserving semantic meaning.
"""

import json
from typing import Any

tiktoken: Any = None
try:
    import tiktoken as _tiktoken

    tiktoken = _tiktoken
except ImportError:
    pass


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
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise ValueError("max_tokens must be a non-negative integer")
        if max_tokens < 0:
            raise ValueError("max_tokens must be a non-negative integer")
        self.max_tokens = max_tokens
        self.counter = TokenCounter()

    def _fit_message(
        self, message: dict[str, Any], token_budget: int
    ) -> dict[str, Any] | None:
        """Return a message that fits, truncating text content when possible."""
        if token_budget <= 0:
            return None
        if self.counter.count_messages([message]) <= token_budget:
            return message

        content = message.get("content")
        if not isinstance(content, str):
            return None

        candidate = dict(message)
        candidate["content"] = ""
        if self.counter.count_messages([candidate]) > token_budget:
            return None

        low = 0
        high = len(content)
        best = dict(candidate)
        while low <= high:
            middle = (low + high) // 2
            candidate["content"] = content[:middle]
            if self.counter.count_messages([candidate]) <= token_budget:
                best = dict(candidate)
                low = middle + 1
            else:
                high = middle - 1
        return best

    def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        deduplicate: bool = True,
    ) -> list[dict[str, Any]]:
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
        # Step 1: Remove duplicates when requested.
        deduped = (
            MessageDeduplicator.deduplicate(messages) if deduplicate else list(messages)
        )

        if self.max_tokens == 0:
            return []

        # Step 2: Check token count
        token_count = self.counter.count_messages(deduped)
        if token_count <= self.max_tokens:
            return deduped

        # Step 3: Keep the system prompt and most recent messages. Text content
        # is shortened to the remaining budget; non-text messages are omitted
        # when they cannot fit.
        result: list[dict[str, Any]] = []
        current_tokens = 0
        other_msgs = deduped

        if deduped and deduped[0].get("role") == "system":
            system = self._fit_message(deduped[0], self.max_tokens)
            if system is not None:
                result.append(system)
                current_tokens = self.counter.count_messages([system])
            other_msgs = deduped[1:]

        selected: list[dict[str, Any]] = []
        for message in reversed(other_msgs):
            fitted = self._fit_message(message, self.max_tokens - current_tokens)
            if fitted is not None:
                selected.append(fitted)
                current_tokens += self.counter.count_messages([fitted])

        result.extend(reversed(selected))
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

    # Apply optimizations while preserving the caller's deduplication choice.
    compressor = MessageCompressor(max_tokens=max_tokens)
    result = compressor.compress(messages, deduplicate=deduplicate)

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

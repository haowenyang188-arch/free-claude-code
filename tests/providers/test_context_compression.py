"""Tests for context compression utilities."""

import pytest

from providers.common.context_compression import (
    ContentSummarizer,
    MessageCompressor,
    MessageDeduplicator,
    TokenCounter,
    optimize_messages,
)


class TestTokenCounter:
    """Test token counting functionality."""

    def test_count_simple_text(self):
        counter = TokenCounter()
        text = "Hello, world!"
        count = counter.count(text)
        # Should be non-zero
        assert count > 0
        # Should be roughly proportional to length
        assert count < len(text)

    def test_count_empty_text(self):
        counter = TokenCounter()
        assert counter.count("") == 0

    def test_count_messages_simple(self):
        counter = TokenCounter()
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        count = counter.count_messages(messages)
        assert count > 0

    def test_count_messages_with_tool_calls(self):
        counter = TokenCounter()
        messages = [
            {
                "role": "assistant",
                "content": "Let me search",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"q": "test"}'},
                    }
                ],
            }
        ]
        count = counter.count_messages(messages)
        assert count > 0

    def test_count_messages_with_image(self):
        counter = TokenCounter()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abc123"},
                    },
                ],
            }
        ]
        count = counter.count_messages(messages)
        # Should include image token cost
        assert count > 85  # Base image cost


class TestMessageDeduplicator:
    """Test message deduplication."""

    def test_deduplicate_no_duplicates(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = MessageDeduplicator.deduplicate(messages)
        assert result == messages

    def test_deduplicate_consecutive_duplicates(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = MessageDeduplicator.deduplicate(messages)
        assert len(result) == 2
        assert result[0]["content"] == "Hello"
        assert result[1]["content"] == "Hi"

    def test_deduplicate_empty_list(self):
        result = MessageDeduplicator.deduplicate([])
        assert result == []

    def test_deduplicate_preserves_order(self):
        messages = [
            {"role": "user", "content": "A"},
            {"role": "user", "content": "B"},
            {"role": "user", "content": "A"},
        ]
        result = MessageDeduplicator.deduplicate(messages)
        assert len(result) == 3  # Different content, so kept


class TestMessageCompressor:
    """Test message compression."""

    def test_compress_under_limit(self):
        compressor = MessageCompressor(max_tokens=10000)
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = compressor.compress(messages)
        assert len(result) == len(messages)

    def test_compress_over_limit(self):
        compressor = MessageCompressor(max_tokens=50)
        # Use longer messages to actually exceed the token limit
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello, can you help me with this?"},
            {"role": "assistant", "content": "Of course! I'd be happy to help."},
            {"role": "user", "content": "How are you doing today?"},
            {"role": "assistant", "content": "I'm doing great, thank you for asking!"},
            {"role": "user", "content": "That's wonderful to hear!"},
        ]
        result = compressor.compress(messages)
        # Should keep system message and recent messages
        assert result[0]["role"] == "system"
        assert len(result) < len(messages) or any(
            "truncated" in str(msg.get("content", "")).lower() for msg in result
        )

    def test_compress_preserves_system_message(self):
        compressor = MessageCompressor(max_tokens=50)
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "A" * 1000},
        ]
        result = compressor.compress(messages)
        # System message should always be kept
        assert any(msg["role"] == "system" for msg in result)

    def test_compress_truncates_an_oversized_system_message_to_budget(self):
        compressor = MessageCompressor(max_tokens=20)
        messages = [
            {"role": "system", "content": "system instructions " * 100},
            {"role": "user", "content": "recent question"},
        ]

        result = compressor.compress(messages)

        assert compressor.counter.count_messages(result) <= 20
        assert result[0]["role"] == "system"

    def test_compress_truncates_a_single_oversized_message(self):
        compressor = MessageCompressor(max_tokens=20)
        messages = [{"role": "user", "content": "long message " * 100}]

        result = compressor.compress(messages)

        assert compressor.counter.count_messages(result) <= 20
        assert len(result) == 1

    def test_compress_zero_budget_returns_no_messages(self):
        compressor = MessageCompressor(max_tokens=0)

        assert compressor.compress([{"role": "user", "content": "hello"}]) == []


class TestContentSummarizer:
    """Test content summarization."""

    def test_should_summarize_short_text(self):
        assert not ContentSummarizer.should_summarize("Short text")

    def test_should_summarize_long_text(self):
        long_text = "A" * 3000
        assert ContentSummarizer.should_summarize(long_text)

    def test_truncate_short_text(self):
        text = "Short"
        result = ContentSummarizer.truncate(text, max_length=100)
        assert result == text

    def test_truncate_long_text(self):
        text = "A" * 2000
        result = ContentSummarizer.truncate(text, max_length=1000)
        assert len(result) <= 1100  # Some overhead for truncation message
        assert "truncated" in result


class TestOptimizeMessages:
    """Test end-to-end message optimization."""

    def test_optimize_simple_messages(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result, stats = optimize_messages(messages)
        assert len(result) == len(messages)
        assert stats["original_messages"] == 2
        assert stats["final_messages"] == 2

    def test_optimize_with_duplicates(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result, stats = optimize_messages(messages, deduplicate=True)
        assert len(result) < len(messages)
        assert stats["messages_removed"] > 0

    def test_optimize_can_preserve_duplicates_when_requested(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "Hello"},
        ]

        result, stats = optimize_messages(messages, deduplicate=False)

        assert result == messages
        assert stats["messages_removed"] == 0

    def test_optimize_zero_budget_obeys_token_limit(self):
        result, stats = optimize_messages(
            [{"role": "system", "content": "system"}], max_tokens=0
        )

        assert result == []
        assert stats["final_tokens"] == 0

    def test_optimize_with_token_limit(self):
        messages = [
            {"role": "user", "content": "A" * 1000},
            {"role": "assistant", "content": "B" * 1000},
            {"role": "user", "content": "C" * 1000},
        ]
        _result, stats = optimize_messages(messages, max_tokens=100)
        assert stats["final_tokens"] <= 100
        assert stats["tokens_saved"] > 0

    def test_optimize_calculates_compression_ratio(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "Hello"},
        ]
        _result, stats = optimize_messages(messages)
        assert "compression_ratio" in stats
        assert 0 <= stats["compression_ratio"] <= 1


@pytest.mark.parametrize(
    "messages,max_tokens,expected_min_messages",
    [
        ([{"role": "user", "content": "Hi"}], 1000, 1),
        (
            [
                {"role": "system", "content": "System"},
                {"role": "user", "content": "Hi"},
            ],
            1000,
            2,
        ),
        (
            [{"role": "user", "content": "A" * 100}] * 10,
            100,
            1,
        ),  # Should truncate heavily
    ],
)
def test_optimize_messages_parametrized(messages, max_tokens, expected_min_messages):
    """Parametrized test for message optimization."""
    result, stats = optimize_messages(messages, max_tokens=max_tokens)
    assert len(result) >= expected_min_messages
    assert stats["final_tokens"] <= max_tokens or len(result) == expected_min_messages

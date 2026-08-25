"""Test that tool results are correctly displayed after the handler fix."""

import pytest
from messaging.transcript import TranscriptBuffer


def test_transcript_buffer_shows_tool_results_when_enabled():
    """Verify that TranscriptBuffer includes tool results when show_tool_results=True."""
    transcript = TranscriptBuffer(show_tool_results=True)

    # Add tool_use event
    transcript.apply({
        "type": "tool_use",
        "id": "test_tool_1",
        "name": "bash",
        "input": {"command": "echo test"},
    })

    # Add tool_result event
    transcript.apply({
        "type": "tool_result",
        "tool_use_id": "test_tool_1",
        "content": "test output",
        "is_error": False,
    })

    # Verify segments are stored (access private attribute for testing)
    segments = transcript._segments

    # Should have 2 segments: ToolUseSegment and ToolResultSegment
    assert len(segments) == 2, f"Expected 2 segments, got {len(segments)}"

    # Verify first segment is tool_use
    assert segments[0].__class__.__name__ == "ToolCallSegment"

    # Verify second segment is tool_result
    assert segments[1].__class__.__name__ == "ToolResultSegment"


def test_transcript_buffer_hides_tool_results_when_disabled():
    """Verify that TranscriptBuffer excludes tool results when show_tool_results=False."""
    transcript = TranscriptBuffer(show_tool_results=False)

    # Add tool_use event
    transcript.apply({
        "type": "tool_use",
        "id": "test_tool_1",
        "name": "bash",
        "input": {"command": "echo test"},
    })

    # Add tool_result event (should be ignored)
    transcript.apply({
        "type": "tool_result",
        "tool_use_id": "test_tool_1",
        "content": "test output",
        "is_error": False,
    })

    # Verify segments are stored (access private attribute for testing)
    segments = transcript._segments

    # Should only have 1 segment: ToolCallSegment (tool_result is hidden)
    assert len(segments) == 1, f"Expected 1 segment, got {len(segments)}"
    assert segments[0].__class__.__name__ == "ToolCallSegment"


def test_handler_fix_verification():
    """Verify the handler.py line 391 fix is in place."""
    # Read the handler file and verify show_tool_results=True
    with open("messaging/handler.py", "r") as f:
        content = f.read()

    # Verify the fix: TranscriptBuffer(show_tool_results=True)
    assert "TranscriptBuffer(show_tool_results=True)" in content, (
        "handler.py should create TranscriptBuffer with show_tool_results=True"
    )

    # Verify the old buggy code is gone
    assert "TranscriptBuffer(show_tool_results=False)" not in content, (
        "handler.py should not have TranscriptBuffer with show_tool_results=False"
    )


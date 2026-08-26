"""Adapters package for runtime-specific task execution."""

from workbench.backend.workflow.adapters.claude_code import ClaudeCodeAdapter

__all__ = ["ClaudeCodeAdapter"]

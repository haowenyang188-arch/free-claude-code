"""Test Claude Code runtime adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from workbench.backend.domain.models import (
    Artifact,
    ArtifactType,
    ContextPackage,
    RuntimeKind,
    SubagentAssignment,
    Task,
)
from workbench.backend.workflow.adapters.claude_code import ClaudeCodeAdapter


@pytest.mark.asyncio
async def test_adapter_supports_claude_code_runtime() -> None:
    """ClaudeCodeAdapter should claim support for CLAUDE_CODE runtime."""
    adapter = ClaudeCodeAdapter()
    assert adapter.supports(RuntimeKind.CLAUDE_CODE) is True
    assert adapter.supports(RuntimeKind.CODEX) is False


@pytest.mark.asyncio
async def test_adapter_constructs_prompt_with_goal_and_instructions() -> None:
    """Adapter should build prompt from context.goal_summary and context.instructions."""
    adapter = ClaudeCodeAdapter()

    task = Task(
        id="task-1",
        step_run_id="step-run-1",
        role_id="researcher",
        title="Research authentication patterns",
    )
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="researcher",
        agent_instance_id="agent-1",
        runtime_id="claude-code-1",
    )
    context = ContextPackage(
        id="context-1",
        task_id=task.id,
        goal_summary="Implement OAuth2 login",
        instructions="Research best practices for OAuth2 implementation in Python web apps",
    )

    with patch.object(adapter, "_invoke_claude_code", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "Research complete: Use authlib library"

        artifact = await adapter.execute_step(task=task, assignment=assignment, context=context)

        # Verify prompt construction
        call_args = mock_invoke.call_args
        prompt = call_args.kwargs["prompt"]
        assert "Implement OAuth2 login" in prompt
        assert "Research best practices for OAuth2 implementation in Python web apps" in prompt


@pytest.mark.asyncio
async def test_adapter_includes_constraints_in_prompt() -> None:
    """Adapter should include context.constraints in the prompt."""
    adapter = ClaudeCodeAdapter()

    task = Task(id="task-1", step_run_id="step-run-1", role_id="researcher")
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="researcher",
        agent_instance_id="agent-1",
        runtime_id="claude-code-1",
    )
    context = ContextPackage(
        id="context-1",
        task_id=task.id,
        goal_summary="Research topic",
        instructions="Do research",
        constraints=["max 500 tokens", "use only public APIs", "no paid services"],
    )

    with patch.object(adapter, "_invoke_claude_code", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "Done"

        await adapter.execute_step(task=task, assignment=assignment, context=context)

        prompt = mock_invoke.call_args.kwargs["prompt"]
        assert "max 500 tokens" in prompt
        assert "use only public APIs" in prompt
        assert "no paid services" in prompt


@pytest.mark.asyncio
async def test_adapter_includes_artifact_references_in_context() -> None:
    """Adapter should load and include upstream artifacts in the prompt."""
    adapter = ClaudeCodeAdapter()

    # Mock artifact store
    artifact_store_mock = Mock()
    artifact_store_mock.get.return_value = (
        b"Previous research findings: Use bcrypt for password hashing"
    )
    adapter._artifact_store = artifact_store_mock

    task = Task(id="task-1", step_run_id="step-run-1", role_id="implementer")
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="implementer",
        agent_instance_id="agent-1",
        runtime_id="claude-code-1",
    )
    context = ContextPackage(
        id="context-1",
        task_id=task.id,
        goal_summary="Implement authentication",
        instructions="Follow research recommendations",
        artifact_ids=["artifact-upstream-1"],
    )

    with patch.object(adapter, "_invoke_claude_code", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "Implementation complete"

        await adapter.execute_step(task=task, assignment=assignment, context=context)

        prompt = mock_invoke.call_args.kwargs["prompt"]
        assert "artifact-upstream-1" in prompt
        assert "bcrypt" in prompt


@pytest.mark.asyncio
async def test_adapter_returns_artifact_with_claude_code_response() -> None:
    """Adapter should wrap Claude Code's response in an Artifact."""
    adapter = ClaudeCodeAdapter()

    task = Task(id="task-1", step_run_id="step-run-1", role_id="researcher")
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="researcher",
        agent_instance_id="agent-1",
        runtime_id="claude-code-1",
    )
    context = ContextPackage(
        id="context-1",
        task_id=task.id,
        goal_summary="Research authentication",
        instructions="Find best practices",
    )

    with patch.object(adapter, "_invoke_claude_code", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "Recommendations:\n1. Use OAuth2\n2. Implement PKCE\n3. Store tokens securely"

        artifact = await adapter.execute_step(task=task, assignment=assignment, context=context)

        assert artifact.task_id == "task-1"
        assert artifact.type == ArtifactType.TEXT
        assert "OAuth2" in artifact.content
        assert "PKCE" in artifact.content
        assert artifact.summary is not None


@pytest.mark.asyncio
async def test_adapter_respects_workspace_scope() -> None:
    """Adapter should pass workspace_scope to Claude Code invocation."""
    adapter = ClaudeCodeAdapter()

    task = Task(id="task-1", step_run_id="step-run-1", role_id="implementer")
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="implementer",
        agent_instance_id="agent-1",
        runtime_id="claude-code-1",
    )
    context = ContextPackage(
        id="context-1",
        task_id=task.id,
        goal_summary="Implement feature",
        instructions="Add login endpoint",
        workspace_scope="/home/user/project/backend/auth",
    )

    with patch.object(adapter, "_invoke_claude_code", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "Done"

        await adapter.execute_step(task=task, assignment=assignment, context=context)

        call_args = mock_invoke.call_args
        assert call_args.kwargs.get("cwd") == "/home/user/project/backend/auth"


@pytest.mark.asyncio
async def test_adapter_includes_allowed_tools_in_prompt() -> None:
    """Adapter should list allowed tools in the prompt when specified."""
    adapter = ClaudeCodeAdapter()

    task = Task(id="task-1", step_run_id="step-run-1", role_id="researcher")
    assignment = SubagentAssignment(
        id="assignment-1",
        task_id=task.id,
        role_id="researcher",
        agent_instance_id="agent-1",
        runtime_id="claude-code-1",
    )
    context = ContextPackage(
        id="context-1",
        task_id=task.id,
        goal_summary="Research codebase",
        instructions="Find authentication logic",
        allowed_tools=["Read", "Grep", "Glob"],
    )

    with patch.object(adapter, "_invoke_claude_code", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "Found in auth.py"

        await adapter.execute_step(task=task, assignment=assignment, context=context)

        prompt = mock_invoke.call_args.kwargs["prompt"]
        assert "Read" in prompt
        assert "Grep" in prompt
        assert "Glob" in prompt

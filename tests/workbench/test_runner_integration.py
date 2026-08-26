"""Integration test: RuntimeNeutralRunner with ClaudeCodeAdapter."""

from __future__ import annotations

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
from workbench.backend.workflow.adapters import ClaudeCodeAdapter
from workbench.backend.workflow.runners import RuntimeNeutralRunner


@pytest.mark.asyncio
async def test_runner_with_claude_code_adapter_end_to_end() -> None:
    """Integration test: Runner routes to ClaudeCodeAdapter and returns artifact."""
    # Setup adapter with mocked Claude Code invocation
    adapter = ClaudeCodeAdapter()

    with patch.object(adapter, "_invoke_claude_code", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "Authentication research complete:\n1. Use OAuth2 with PKCE\n2. Store refresh tokens in httpOnly cookies\n3. Implement token rotation"

        # Setup runner
        runner = RuntimeNeutralRunner(adapters=[adapter])
        runner.register_runtime("claude-code-main", RuntimeKind.CLAUDE_CODE)

        # Create task with full context
        task = Task(
            id="task-research-1",
            step_run_id="step-run-1",
            role_id="security_researcher",
            title="Research OAuth2 best practices",
        )
        assignment = SubagentAssignment(
            id="assignment-1",
            task_id=task.id,
            role_id="security_researcher",
            agent_instance_id="agent-001",
            runtime_id="claude-code-main",
        )
        context = ContextPackage(
            id="context-1",
            task_id=task.id,
            goal_summary="Implement secure OAuth2 authentication for web app",
            instructions="Research current best practices for OAuth2 implementation in 2026, focusing on security considerations",
            constraints=["Must support PKCE", "Token storage must be secure"],
            allowed_tools=["Read", "WebSearch"],
        )

        # Execute
        artifact = await runner.execute(task=task, assignment=assignment, context=context)

        # Verify artifact
        assert artifact.task_id == "task-research-1"
        assert artifact.type == ArtifactType.TEXT
        assert "OAuth2" in artifact.content
        assert "PKCE" in artifact.content
        assert "cookies" in artifact.content
        assert artifact.summary is not None

        # Verify Claude Code was invoked with correct prompt structure
        mock_invoke.assert_called_once()
        call_kwargs = mock_invoke.call_args.kwargs
        prompt = call_kwargs["prompt"]

        assert "Implement secure OAuth2 authentication for web app" in prompt
        assert "Research current best practices" in prompt
        assert "Must support PKCE" in prompt
        assert "Token storage must be secure" in prompt
        assert "Read" in prompt
        assert "WebSearch" in prompt


@pytest.mark.asyncio
async def test_runner_with_claude_code_passes_workspace_scope() -> None:
    """Integration test: Workspace scope flows through to Claude Code cwd."""
    adapter = ClaudeCodeAdapter()

    with patch.object(adapter, "_invoke_claude_code", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "Implementation complete"

        runner = RuntimeNeutralRunner(adapters=[adapter])
        runner.register_runtime("claude-code-main", RuntimeKind.CLAUDE_CODE)

        task = Task(
            id="task-impl-1",
            step_run_id="step-run-2",
            role_id="backend_engineer",
        )
        assignment = SubagentAssignment(
            id="assignment-2",
            task_id=task.id,
            role_id="backend_engineer",
            agent_instance_id="agent-002",
            runtime_id="claude-code-main",
        )
        context = ContextPackage(
            id="context-2",
            task_id=task.id,
            goal_summary="Add OAuth2 endpoints",
            instructions="Implement /oauth/authorize and /oauth/token endpoints",
            workspace_scope="/home/user/myapp/backend/auth",
        )

        await runner.execute(task=task, assignment=assignment, context=context)

        # Verify cwd was passed
        call_kwargs = mock_invoke.call_args.kwargs
        assert call_kwargs["cwd"] == "/home/user/myapp/backend/auth"


@pytest.mark.asyncio
async def test_runner_with_claude_code_loads_upstream_artifacts() -> None:
    """Integration test: Upstream artifacts are loaded and included in prompt."""
    # Mock artifact store
    artifact_store = Mock()
    artifact_store.get.return_value = b"Research findings:\n- Use authlib library\n- Implement state parameter validation\n- Use secure random for state generation"

    adapter = ClaudeCodeAdapter(artifact_store=artifact_store)

    with patch.object(adapter, "_invoke_claude_code", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = "Implementation follows research recommendations"

        runner = RuntimeNeutralRunner(adapters=[adapter])
        runner.register_runtime("claude-code-main", RuntimeKind.CLAUDE_CODE)

        task = Task(
            id="task-impl-2",
            step_run_id="step-run-3",
            role_id="backend_engineer",
        )
        assignment = SubagentAssignment(
            id="assignment-3",
            task_id=task.id,
            role_id="backend_engineer",
            agent_instance_id="agent-003",
            runtime_id="claude-code-main",
        )
        context = ContextPackage(
            id="context-3",
            task_id=task.id,
            goal_summary="Implement OAuth2",
            instructions="Follow research recommendations",
            artifact_ids=["artifact-research-1"],
        )

        await runner.execute(task=task, assignment=assignment, context=context)

        # Verify artifact was loaded
        artifact_store.get.assert_called_once()

        # Verify artifact content appeared in prompt
        call_kwargs = mock_invoke.call_args.kwargs
        prompt = call_kwargs["prompt"]
        assert "artifact-research-1" in prompt
        assert "authlib" in prompt
        assert "state parameter validation" in prompt

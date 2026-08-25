"""Tests for ClaudeCodeAdapter CLI integration."""

import asyncio
import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from workbench.backend.workflow.adapters.claude_code import ClaudeCodeAdapter
from workbench.backend.workflow.runners import RunnerError
from workbench.backend.domain.models import (
    Task,
    SubagentAssignment,
    ContextPackage,
    Artifact,
)


@pytest.fixture
def adapter():
    """Create ClaudeCodeAdapter instance."""
    return ClaudeCodeAdapter()


@pytest.fixture
def sample_task():
    """Create sample task."""
    return Task(
        id="task-123",
        step_run_id="step-run-456",
        title="Research OAuth2 security",
        role_id="security_researcher",
        status="pending",
    )


@pytest.fixture
def sample_assignment():
    """Create sample assignment."""
    return SubagentAssignment(
        id="assign-789",
        task_id="task-123",
        role_id="security_researcher",
        agent_instance_id="agent-001",
        runtime_id="claude_code",
    )


@pytest.fixture
def sample_context():
    """Create sample context package."""
    return ContextPackage(
        id="ctx-abc",
        task_id="task-123",
        goal_summary="Research OAuth2 best practices",
        instructions="Focus on PKCE and token storage",
        constraints=["max 1000 tokens"],
        artifact_ids=[],
        decisions=[],
        allowed_tools=["Read", "WebSearch"],
        workspace_scope="/project/auth",
    )


@pytest.mark.asyncio
class TestClaudeCodeCLIIntegration:
    """Test ClaudeCodeAdapter CLI subprocess execution."""

    async def test_invoke_creates_temp_file_and_calls_claude_cli(
        self, adapter, sample_task, sample_assignment, sample_context
    ):
        """Should create temp file with prompt and call claude CLI."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            return_value=(b"OAuth2 recommendations: Use PKCE...", b"")
        )
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with patch("tempfile.NamedTemporaryFile") as mock_temp:
                # Mock temp file
                mock_file = MagicMock()
                mock_file.name = "/tmp/prompt-xyz.txt"
                mock_file.__enter__ = Mock(return_value=mock_file)
                mock_file.__exit__ = Mock(return_value=None)
                mock_temp.return_value = mock_file

                artifact = await adapter.execute_step(
                    task=sample_task,
                    assignment=sample_assignment,
                    context=sample_context,
                )

                # Verify temp file was created with prompt content
                mock_temp.assert_called_once()
                assert mock_temp.call_args[1]["mode"] == "w"
                assert mock_temp.call_args[1]["suffix"] == ".txt"
                assert mock_temp.call_args[1]["delete"] is False

                # Verify prompt was written to file
                mock_file.write.assert_called_once()
                written_prompt = mock_file.write.call_args[0][0]
                assert "Research OAuth2 best practices" in written_prompt
                assert "Focus on PKCE and token storage" in written_prompt

                # Verify claude CLI was called with --print flag (not --cwd)
                mock_exec.assert_called_once()
                call_args = mock_exec.call_args[0]
                assert call_args[0] == "claude"
                assert call_args[1] == "--print"
                assert call_args[2] == "/tmp/prompt-xyz.txt"

                # Verify cwd was passed to subprocess, not as CLI flag
                call_kwargs = mock_exec.call_args[1]
                assert call_kwargs["cwd"] == "/project/auth"

                # Verify artifact was created with response
                assert artifact.task_id == "task-123"
                assert "OAuth2 recommendations" in artifact.content

    async def test_invoke_passes_cwd_to_subprocess(
        self, adapter, sample_task, sample_assignment, sample_context
    ):
        """Should pass workspace_scope as cwd to subprocess."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"Response", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with patch("tempfile.NamedTemporaryFile"):
                with patch("os.unlink"):
                    await adapter.execute_step(
                        task=sample_task,
                        assignment=sample_assignment,
                        context=sample_context,
                    )

                    # Verify cwd was passed to subprocess
                    call_kwargs = mock_exec.call_args[1]
                    assert call_kwargs["cwd"] == "/project/auth"

    async def test_invoke_handles_nonzero_exit_code(
        self, adapter, sample_task, sample_assignment, sample_context
    ):
        """Should raise RunnerError if claude CLI exits with non-zero code."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            return_value=(b"", b"Error: Permission denied")
        )
        mock_process.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch("tempfile.NamedTemporaryFile"):
                with patch("os.unlink"):
                    with pytest.raises(RunnerError, match="exited with code 1"):
                        await adapter.execute_step(
                            task=sample_task,
                            assignment=sample_assignment,
                            context=sample_context,
                        )

    async def test_invoke_handles_timeout(
        self, adapter, sample_task, sample_assignment, sample_context
    ):
        """Should raise RunnerError if claude CLI times out."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            side_effect=TimeoutError("Timeout")
        )
        mock_process.kill = AsyncMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch("tempfile.NamedTemporaryFile"):
                with patch("os.unlink"):
                    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                        with pytest.raises(RunnerError, match="timeout after 5 minutes"):
                            await adapter.execute_step(
                                task=sample_task,
                                assignment=sample_assignment,
                                context=sample_context,
                            )

                        # Verify process was killed
                        mock_process.kill.assert_called_once()

    async def test_invoke_handles_empty_response(
        self, adapter, sample_task, sample_assignment, sample_context
    ):
        """Should raise RunnerError if claude CLI returns empty response."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch("tempfile.NamedTemporaryFile"):
                with patch("os.unlink"):
                    with pytest.raises(RunnerError, match="empty response"):
                        await adapter.execute_step(
                            task=sample_task,
                            assignment=sample_assignment,
                            context=sample_context,
                        )

    async def test_invoke_cleans_up_temp_file(
        self, adapter, sample_task, sample_assignment, sample_context
    ):
        """Should clean up temp file even if execution fails."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"Response", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch("tempfile.NamedTemporaryFile") as mock_temp:
                mock_file = MagicMock()
                mock_file.name = "/tmp/prompt-xyz.txt"
                mock_file.__enter__ = Mock(return_value=mock_file)
                mock_file.__exit__ = Mock(return_value=None)
                mock_temp.return_value = mock_file

                with patch("os.unlink") as mock_unlink:
                    await adapter.execute_step(
                        task=sample_task,
                        assignment=sample_assignment,
                        context=sample_context,
                    )

                    # Verify temp file was cleaned up
                    mock_unlink.assert_called_once_with("/tmp/prompt-xyz.txt")

    async def test_invoke_without_workspace_scope(
        self, adapter, sample_task, sample_assignment
    ):
        """Should work without workspace_scope (cwd=None)."""
        context = ContextPackage(
            id="ctx-abc",
            task_id="task-123",
            goal_summary="Research OAuth2",
            instructions="",
            workspace_scope=None,  # No workspace scope
        )

        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"Response", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            with patch("tempfile.NamedTemporaryFile"):
                with patch("os.unlink"):
                    await adapter.execute_step(
                        task=sample_task,
                        assignment=sample_assignment,
                        context=context,
                    )

                    # Verify claude was called with --print but no cwd in command
                    call_args = mock_exec.call_args[0]
                    assert call_args[0] == "claude"
                    assert call_args[1] == "--print"
                    assert len(call_args) == 3  # claude, --print, prompt_file

                    # Verify cwd=None was passed to subprocess
                    call_kwargs = mock_exec.call_args[1]
                    assert call_kwargs["cwd"] is None

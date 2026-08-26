from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCodexSession:
    def test_build_command_defaults_to_read_only_and_never_uses_shell(self) -> None:
        from cli.codex_session import CodexSession

        session = CodexSession("/tmp/project")

        command = session.build_command("inspect the project")

        assert command[:3] == ["codex", "exec", "--json"]
        assert "--cd" in command
        assert "/tmp/project" in command
        assert "--sandbox" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[-1] == "inspect the project"
        assert "--ignore-user-config" in command
        assert "--ignore-rules" in command
        assert "--strict-config" in command
        assert session.shell is False

    def test_build_command_can_resume_or_fork_a_session(self) -> None:
        from cli.codex_session import CodexSession

        session = CodexSession("/tmp/project", sandbox_mode="workspace-write")

        resumed = session.build_command("continue", session_id="thread-1")
        forked = session.build_command(
            "branch", session_id="thread-1", fork_session=True
        )

        assert resumed[:3] == ["codex", "exec", "resume"]
        assert resumed[3] == "thread-1"
        assert resumed[-1] == "continue"
        assert forked[:3] == ["codex", "exec", "fork"]
        assert forked[3] == "thread-1"
        assert "--sandbox" not in resumed
        assert "--sandbox" not in forked

    def test_inherit_isolation_omits_safe_profile_flags(self) -> None:
        from cli.codex_session import CodexSession

        command = CodexSession("/tmp/project", isolation_mode="inherit").build_command(
            "inspect"
        )

        assert "--ignore-user-config" not in command
        assert "--ignore-rules" not in command
        assert "--strict-config" not in command

    def test_build_command_projects_approval_hooks_when_enabled(self, tmp_path) -> None:
        from cli.approval import ApprovalPolicy
        from cli.codex_session import CodexSession

        policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
        command = CodexSession(str(tmp_path), approval_policy=policy).build_command(
            "inspect"
        )

        assert command.count("--config") == 2
        assert any("hooks.PreToolUse" in value for value in command)
        assert any("hooks.PermissionRequest" in value for value in command)

    @pytest.mark.asyncio
    async def test_enabled_policy_projects_hook_environment(self, tmp_path) -> None:
        from cli.approval import ApprovalPolicy
        from cli.codex_session import CodexSession

        process = MagicMock()
        process.pid = 123
        process.stdout.readline = AsyncMock(side_effect=[b""])
        process.wait = AsyncMock(return_value=0)
        policy = ApprovalPolicy.low_risk(enabled=True, allowed_workspaces=[tmp_path])
        session = CodexSession(str(tmp_path), approval_policy=policy)

        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
            patch("cli.codex_session.register_process"),
            patch("cli.codex_session.unregister_process"),
        ):
            spawn.return_value = process
            [event async for event in session.start_task("inspect")]

        call = spawn.await_args
        assert call is not None
        assert call.kwargs["env"]["FCC_APPROVAL_ENABLED"] == "true"

    @pytest.mark.asyncio
    async def test_streams_thread_message_and_completion_events(self) -> None:
        from cli.codex_session import CodexSession

        process = AsyncMock()
        process.stdout.readline.side_effect = [
            b'{"type":"thread.started","thread_id":"thread-1"}\n',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"ready"}}\n',
            b'{"type":"turn.completed","usage":{"total_tokens":12}}\n',
            b"",
        ]
        process.stderr.read.return_value = b""
        process.wait.return_value = 0

        session = CodexSession("/tmp/project")
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            spawn.return_value = process
            events = [event async for event in session.start_task("inspect")]

        assert events[0] == {"type": "session_info", "session_id": "thread-1"}
        assert events[1]["type"] == "assistant"
        assert events[1]["message"]["content"][0]["text"] == "ready"
        assert events[-1] == {"type": "exit", "code": 0, "stderr": None}
        assert session.current_session_id == "thread-1"
        spawn.assert_awaited_once()
        call = spawn.await_args
        assert call is not None
        assert call.args[0:3] == ("codex", "exec", "--json")
        assert call.kwargs.get("shell", False) is False

    @pytest.mark.asyncio
    async def test_generation_rotates_for_each_started_process(self) -> None:
        from cli.codex_session import CodexSession

        def make_process():
            process = MagicMock()
            process.pid = 123
            process.stdout.readline = AsyncMock(side_effect=[b""])
            process.stderr.read = AsyncMock(return_value=b"")
            process.wait = AsyncMock(return_value=0)
            return process

        registered: list[str] = []

        def register(_pid, *, generation):
            registered.append(generation)

        session = CodexSession("/tmp/project")
        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
            patch("cli.codex_session.register_process", side_effect=register),
            patch("cli.codex_session.unregister_process"),
        ):
            spawn.side_effect = [make_process(), make_process()]
            [event async for event in session.start_task("first")]
            [event async for event in session.start_task("second")]

        assert len(registered) == 2
        assert registered[0] != registered[1]
        assert session.generation == registered[-1]

    @pytest.mark.asyncio
    async def test_start_task_can_use_caller_owned_generation(self) -> None:
        from cli.codex_session import CodexSession

        process = MagicMock()
        process.pid = 123
        process.stdout.readline = AsyncMock(side_effect=[b""])
        process.stderr.read = AsyncMock(return_value=b"")
        process.wait = AsyncMock(return_value=0)

        session = CodexSession("/tmp/project")
        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
            patch("cli.codex_session.register_process") as register,
            patch("cli.codex_session.unregister_process"),
        ):
            spawn.return_value = process
            [
                event
                async for event in session.start_task(
                    "inspect", generation="owned-generation"
                )
            ]

        assert session.generation == "owned-generation"
        register.assert_called_once_with(123, generation="owned-generation")

    @pytest.mark.asyncio
    async def test_preflight_failure_does_not_spawn_codex(self, monkeypatch) -> None:
        from cli.codex_session import CodexSession
        from cli.runtime_registry import RuntimeBackend, RuntimeRegistry

        async def runner(_argv, _timeout):
            return 1, b"", b"hidden diagnostic"

        monkeypatch.setattr("cli.runtime_registry.shutil.which", lambda _: "/bin/codex")
        session = CodexSession(
            "/tmp/project",
            runtime_registry=RuntimeRegistry(
                executables={RuntimeBackend.CODEX: "codex"},
                runner=runner,
            ),
            preflight_runtime=True,
        )
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            events = [event async for event in session.start_task("inspect")]

        assert events == [
            {
                "type": "error",
                "error": {
                    "message": "Codex CLI preflight failed: version_command_failed"
                },
            },
            {"type": "exit", "code": 127, "stderr": "version_command_failed"},
        ]
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_codex_child_environment_drops_inherited_provider_and_proxy_keys(
        self, monkeypatch
    ) -> None:
        from cli.codex_session import CodexSession

        monkeypatch.setenv("OPENAI_API_KEY", "parent-secret")
        monkeypatch.setenv("HTTP_PROXY", "http://stale-proxy")
        process = MagicMock()
        process.pid = 123
        process.stdout.readline = AsyncMock(side_effect=[b""])
        process.stderr.read = AsyncMock(return_value=b"")
        process.wait = AsyncMock(return_value=0)

        session = CodexSession("/tmp/project")
        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
            patch("cli.codex_session.register_process"),
            patch("cli.codex_session.unregister_process"),
        ):
            spawn.return_value = process
            [event async for event in session.start_task("inspect")]

        call = spawn.await_args
        assert call is not None
        child_env = call.kwargs["env"]
        assert "OPENAI_API_KEY" not in child_env
        assert "HTTP_PROXY" not in child_env
        assert child_env["TERM"] == "dumb"

    @pytest.mark.asyncio
    async def test_successful_exit_discards_stderr(self) -> None:
        from cli.codex_session import CodexSession

        process = MagicMock()
        process.pid = 123
        process.stdout.readline = AsyncMock(side_effect=[b""])
        process.stderr.read = AsyncMock(return_value=b"warning: informational")
        process.wait = AsyncMock(return_value=0)

        session = CodexSession("/tmp/project")
        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
            patch("cli.codex_session.register_process"),
            patch("cli.codex_session.unregister_process"),
            patch("cli.codex_session.logger.debug") as debug_log,
        ):
            spawn.return_value = process
            events = [event async for event in session.start_task("inspect")]

        assert not any(event["type"] == "error" for event in events)
        assert events[-1] == {
            "type": "exit",
            "code": 0,
            "stderr": None,
        }
        call = spawn.await_args
        assert call is not None
        assert call.kwargs["stderr"] is asyncio.subprocess.DEVNULL
        process.stderr.read.assert_not_awaited()
        logged = " ".join(
            " ".join(str(value) for value in call.args)
            for call in debug_log.call_args_list
        )
        assert "warning: informational" not in logged

    @pytest.mark.asyncio
    async def test_failed_exit_hides_provider_stderr(self) -> None:
        from cli.codex_session import CodexSession

        process = MagicMock()
        process.pid = 123
        process.stdout.readline = AsyncMock(side_effect=[b""])
        process.stderr.read = AsyncMock(return_value=b"fatal: failed")
        process.wait = AsyncMock(return_value=1)

        session = CodexSession("/tmp/project")
        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
            patch("cli.codex_session.register_process"),
            patch("cli.codex_session.unregister_process"),
        ):
            spawn.return_value = process
            events = [event async for event in session.start_task("inspect")]

        errors = [event for event in events if event["type"] == "error"]
        assert errors == [
            {"type": "error", "error": {"message": "Codex CLI exited with code 1"}}
        ]
        assert events[-1] == {"type": "exit", "code": 1, "stderr": None}
        call = spawn.await_args
        assert call is not None
        assert call.kwargs["stderr"] is asyncio.subprocess.DEVNULL
        process.stderr.read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_large_stderr_does_not_block_session(self, tmp_path) -> None:
        from cli.codex_session import CodexSession

        script = tmp_path / "fake-codex"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stderr.write('x' * (1024 * 1024))\n"
            "sys.stderr.flush()\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        session = CodexSession(str(tmp_path), codex_bin=str(script))

        async def collect_events() -> list[dict]:
            return [event async for event in session.start_task("probe")]

        events = await asyncio.wait_for(collect_events(), timeout=2)
        assert events[-1] == {"type": "exit", "code": 0, "stderr": None}

    @pytest.mark.asyncio
    async def test_cancel_terminates_running_process(self) -> None:
        from cli.codex_session import CodexSession

        process = MagicMock()
        process.returncode = None
        process.wait = AsyncMock(return_value=0)
        session = CodexSession("/tmp/project")
        session.process = process

        assert await session.stop() is True
        process.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_approval_mode_stages_changes_until_approved(self, tmp_path):
        from cli.codex_session import CodexSession

        workspace = tmp_path / "project"
        workspace.mkdir()
        (workspace / "before.txt").write_text("before\n", encoding="utf-8")

        process = MagicMock()
        process.stdout.readline = AsyncMock(
            side_effect=[
                b'{"type":"thread.started","thread_id":"thread-approval"}\n',
                b"",
            ]
        )
        process.stderr.read = AsyncMock(return_value=b"")
        process.wait = AsyncMock(return_value=0)

        async def spawn(*_args, **kwargs):
            Path(kwargs["cwd"]).joinpath("before.txt").write_text(
                "after\n", encoding="utf-8"
            )
            return process

        session = CodexSession(
            workspace,
            sandbox_mode="workspace-write",
            approval_mode=True,
        )
        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            events = [event async for event in session.start_task("edit")]

        assert any(event["type"] == "approval_required" for event in events)
        assert events[-1]["awaiting_approval"] is True
        assert (workspace / "before.txt").read_text(encoding="utf-8") == "before\n"

        result = await session.approve()
        assert result["changed_paths"] == ["before.txt"]
        assert (workspace / "before.txt").read_text(encoding="utf-8") == "after\n"

    @pytest.mark.asyncio
    async def test_approval_state_is_set_before_first_approval_yield(self, tmp_path):
        from cli.codex_session import CodexSession

        workspace = tmp_path / "project"
        workspace.mkdir()
        (workspace / "before.txt").write_text("before\n", encoding="utf-8")
        process = MagicMock()
        process.pid = 123
        process.stdout.readline = AsyncMock(side_effect=[b""])
        process.stderr.read = AsyncMock(return_value=b"")
        process.wait = AsyncMock(return_value=0)

        async def spawn(*_args, **kwargs):
            Path(kwargs["cwd"]).joinpath("before.txt").write_text(
                "after\n", encoding="utf-8"
            )
            return process

        session = CodexSession(
            workspace,
            sandbox_mode="workspace-write",
            approval_mode=True,
        )
        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            task = session.start_task("edit")
            first = await anext(task)
            assert first["type"] == "approval_required"
            assert session._staged_workspace is not None
            staged_path = session._staged_workspace.path
            assert staged_path.exists()
            await task.aclose()

        assert session._staged_workspace is not None
        assert session._staged_workspace.path == staged_path
        session.reject()
        assert session._staged_workspace is None
        assert not staged_path.exists()

    @pytest.mark.asyncio
    async def test_approval_mode_stages_resumed_threads_in_the_copy(self, tmp_path):
        from cli.codex_session import CodexSession

        workspace = tmp_path / "project"
        workspace.mkdir()
        (workspace / "before.txt").write_text("before\n", encoding="utf-8")
        process = MagicMock()
        process.stdout.readline = AsyncMock(side_effect=[b""])
        process.stderr.read = AsyncMock(return_value=b"")
        process.wait = AsyncMock(return_value=0)

        async def spawn(*_args, **kwargs):
            assert Path(kwargs["cwd"]) != workspace
            return process

        session = CodexSession(
            workspace, sandbox_mode="workspace-write", approval_mode=True
        )

        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            [event async for event in session.start_task("edit", session_id="thread-1")]

    def test_staged_approval_rejects_full_access_sandbox(self):
        from cli.codex_session import CodexSession

        with pytest.raises(ValueError, match="danger-full-access"):
            CodexSession(
                "/tmp/project",
                sandbox_mode="danger-full-access",
                approval_mode=True,
            )

    @pytest.mark.asyncio
    async def test_failed_staged_run_is_not_offered_for_approval(self, tmp_path):
        from cli.codex_session import CodexSession

        workspace = tmp_path / "project"
        workspace.mkdir()
        (workspace / "before.txt").write_text("before\n", encoding="utf-8")
        process = MagicMock()
        process.stdout.readline = AsyncMock(side_effect=[b""])
        process.stderr.read = AsyncMock(return_value=b"failed")
        process.wait = AsyncMock(return_value=1)

        async def spawn(*_args, **kwargs):
            Path(kwargs["cwd"]).joinpath("before.txt").write_text(
                "failed change\n", encoding="utf-8"
            )
            return process

        session = CodexSession(
            workspace, sandbox_mode="workspace-write", approval_mode=True
        )
        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            events = [event async for event in session.start_task("edit")]

        assert not any(event["type"] == "approval_required" for event in events)
        assert events[-1]["type"] == "exit"
        assert (
            not workspace.joinpath("before.txt")
            .read_text(encoding="utf-8")
            .startswith("failed")
        )

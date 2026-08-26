from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_successful_stderr_is_discarded_and_not_exposed() -> None:
    from cli.session import CLISession

    session = CLISession("/tmp", "http://localhost:8082/v1")
    process = AsyncMock()
    process.stdout.read.side_effect = [b""]
    process.stderr.read.return_value = b"provider detail that must stay private"
    process.wait.return_value = 0

    with (
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
        patch("cli.session.logger.debug") as debug_log,
    ):
        spawn.return_value = process
        events = [event async for event in session.start_task("read the project")]

    assert events[-1] == {
        "type": "exit",
        "code": 0,
        "stderr": None,
    }
    spawn.assert_awaited_once()
    call = spawn.await_args
    assert call is not None
    assert call.kwargs["stderr"] is asyncio.subprocess.DEVNULL
    process.stderr.read.assert_not_awaited()
    logged = " ".join(
        " ".join(str(value) for value in call.args) for call in debug_log.call_args_list
    )
    assert "provider detail that must stay private" not in logged


@pytest.mark.asyncio
async def test_failed_stderr_is_replaced_with_stable_error() -> None:
    from cli.session import CLISession

    session = CLISession("/tmp", "http://localhost:8082/v1")
    process = AsyncMock()
    process.stdout.read.side_effect = [b""]
    process.stderr.read.return_value = b"provider secret detail"
    process.wait.return_value = 1

    with (
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn,
        patch("cli.session.logger.error") as error_log,
    ):
        spawn.return_value = process
        events = [event async for event in session.start_task("read the project")]

    assert events[0] == {
        "type": "error",
        "error": {"message": "Claude CLI exited with code 1"},
    }
    assert events[1] == {"type": "exit", "code": 1, "stderr": None}
    call = spawn.await_args
    assert call is not None
    assert call.kwargs["stderr"] is asyncio.subprocess.DEVNULL
    process.stderr.read.assert_not_awaited()
    logged = " ".join(
        " ".join(str(value) for value in call.args) for call in error_log.call_args_list
    )
    assert "provider secret detail" not in logged


@pytest.mark.asyncio
async def test_large_stderr_does_not_block_session(tmp_path) -> None:
    from cli.session import CLISession

    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('x' * (1024 * 1024))\n"
        "sys.stderr.flush()\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    session = CLISession(
        str(tmp_path),
        "http://localhost:8082/v1",
        claude_bin=str(script),
        use_proxy=False,
    )

    async def collect_events() -> list[dict]:
        return [event async for event in session.start_task("probe")]

    events = await asyncio.wait_for(collect_events(), timeout=2)
    assert events[-1] == {"type": "exit", "code": 0, "stderr": None}

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_successful_stderr_is_not_written_to_debug_logs() -> None:
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
        "stderr": "provider detail that must stay private",
    }
    logged = " ".join(
        " ".join(str(value) for value in call.args) for call in debug_log.call_args_list
    )
    assert "provider detail that must stay private" not in logged


@pytest.mark.asyncio
async def test_failed_stderr_is_not_written_to_error_logs() -> None:
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
        "error": {"message": "provider secret detail"},
    }
    logged = " ".join(
        " ".join(str(value) for value in call.args) for call in error_log.call_args_list
    )
    assert "provider secret detail" not in logged

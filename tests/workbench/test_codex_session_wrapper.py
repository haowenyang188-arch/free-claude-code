from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_wrapper_hides_provider_stderr() -> None:
    from workbench.backend.agents.codex_session_wrapper import CodexSessionWrapper

    process = MagicMock()
    process.stdout.readline = AsyncMock(side_effect=[b""])
    process.stderr.read = AsyncMock(return_value=b"provider secret detail")
    process.wait = AsyncMock(return_value=1)

    wrapper = CodexSessionWrapper("/tmp/project")
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        events = [event async for event in wrapper.start_task("inspect")]

    assert events == [
        {"type": "error", "error": {"message": "Codex CLI exited with code 1"}},
        {"type": "exit", "code": 1, "stderr": None},
    ]
    call = spawn.await_args
    assert call is not None
    assert call.kwargs["stderr"] is asyncio.subprocess.DEVNULL
    process.stderr.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrapper_large_stderr_does_not_block(tmp_path) -> None:
    from workbench.backend.agents.codex_session_wrapper import CodexSessionWrapper

    script = tmp_path / "fake-codex"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stderr.write('x' * (1024 * 1024))\n"
        "sys.stderr.flush()\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    wrapper = CodexSessionWrapper(str(tmp_path), codex_bin=str(script))

    async def collect_events() -> list[dict]:
        return [event async for event in wrapper.start_task("probe")]

    events = await asyncio.wait_for(collect_events(), timeout=2)
    assert events[-1] == {"type": "exit", "code": 0, "stderr": None}

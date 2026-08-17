"""Fake-stdio integration tests for the DSH transport."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from harness.config import HarnessConfig
from harness.process import HarnessProcess, HarnessRuntimeClosedError


def _runtime_script(tmp_path: Path, body: str) -> tuple[str, ...]:
    script = tmp_path / "fake_runtime.py"
    script.write_text(body, encoding="utf-8")
    return (sys.executable, str(script))


@pytest.mark.asyncio
async def test_process_correlates_response_and_fans_out_notifications(
    tmp_path: Path,
) -> None:
    command = _runtime_script(
        tmp_path,
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        print(json.dumps({"jsonrpc":"2.0","method":"session.status","params":{"sessionId":"s","status":"running"}}), flush=True)
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{"serverInfo":{"name":"fake"}}}), flush=True)
    elif request.get("method") == "shutdown":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{}}), flush=True)
        break
""".strip(),
    )
    config = HarnessConfig(
        enabled=True,
        runtime_command=command,
        workspace_root=tmp_path,
    )
    process = HarnessProcess(config)
    await process.start()
    queue = process.subscribe()
    try:
        result = await process.request("initialize", {"cwd": str(tmp_path)})
        assert result == {"serverInfo": {"name": "fake"}}
        notification = await queue.get()
        assert notification.method == "session.status"
        assert notification.params == {"sessionId": "s", "status": "running"}
    finally:
        await process.close()

    assert process.is_running is False


@pytest.mark.asyncio
async def test_process_notifies_waiters_when_runtime_exits(tmp_path: Path) -> None:
    command = _runtime_script(
        tmp_path,
        """
import sys
sys.exit(0)
""".strip(),
    )
    config = HarnessConfig(enabled=True, runtime_command=command)
    process = HarnessProcess(config)
    await process.start()
    queue = process.subscribe()
    item = await queue.get()
    assert isinstance(item, HarnessRuntimeClosedError)
    await process.close()


@pytest.mark.asyncio
async def test_process_does_not_forward_unrelated_host_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FCC_HOST_SECRET", "should-not-reach-runtime")
    command = _runtime_script(
        tmp_path,
        """
import json
import os
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        result = {"secret": os.environ.get("FCC_HOST_SECRET", "<missing>")}
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}), flush=True)
    elif request.get("method") == "shutdown":
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":{}}), flush=True)
        break
""".strip(),
    )
    process = HarnessProcess(
        HarnessConfig(enabled=True, runtime_command=command, workspace_root=tmp_path),
    )
    await process.start()
    try:
        result = await process.request("initialize")
        assert result == {"secret": "<missing>"}
    finally:
        await process.close()


@pytest.mark.asyncio
async def test_process_uses_a_separate_process_group_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX process groups are not available on this platform")

    command = _runtime_script(
        tmp_path,
        """
import sys
for line in sys.stdin:
    if 'shutdown' in line:
        break
""".strip(),
    )
    process = HarnessProcess(
        HarnessConfig(enabled=True, runtime_command=command, workspace_root=tmp_path),
    )
    await process.start()
    try:
        assert process.process is not None
        assert os.getpgid(process.process.pid) == process.process.pid
    finally:
        await process.close()

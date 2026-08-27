from __future__ import annotations

import asyncio
import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.asyncio
async def test_one_shot_approval_binds_the_full_command_identity(
    tmp_path: Path,
) -> None:
    from workbench.backend.runtime.approval import (
        ApprovalIntegrityError,
        ApprovalManager,
        ApprovalState,
        CommandIntent,
        CommandRisk,
    )

    intent = CommandIntent.create(
        session_id="session-1",
        call_id="call-1",
        command="python tool.py --mode check",
        cwd=tmp_path,
        requested_permission="project_write",
    )
    assert intent.risk is CommandRisk.LEVEL_B

    approvals = ApprovalManager()
    pending = await approvals.request(intent)
    assert pending.status is ApprovalState.PENDING
    assert pending.command_hash == intent.command_hash
    assert not hasattr(pending, "pid")

    approved = await approvals.approve(
        session_id=intent.session_id,
        call_id=intent.call_id,
        command_hash=intent.command_hash,
    )
    assert approved.status is ApprovalState.APPROVED

    changed = CommandIntent.create(
        session_id="session-1",
        call_id="call-1",
        command="python tool.py --mode write",
        cwd=tmp_path,
        requested_permission="project_write",
    )
    with pytest.raises(ApprovalIntegrityError, match="approval_integrity_mismatch"):
        await approvals.consume(changed)

    consumed = await approvals.consume(intent)
    assert consumed.status is ApprovalState.CONSUMED
    with pytest.raises(ApprovalIntegrityError, match="approval_unavailable"):
        await approvals.consume(intent)


@pytest.mark.asyncio
async def test_approval_timeout_never_creates_a_process(tmp_path: Path) -> None:
    from workbench.backend.runtime.approval import (
        ApprovalManager,
        ApprovalState,
        CommandIntent,
    )

    intent = CommandIntent.create(
        session_id="session-timeout",
        call_id="call-timeout",
        command="python tool.py",
        cwd=tmp_path,
        requested_permission="project_write",
    )
    approvals = ApprovalManager()
    record = await approvals.request(intent, approval_timeout_seconds=0)

    assert record.status is ApprovalState.APPROVAL_TIMEOUT
    assert not hasattr(record, "pid")
    with pytest.raises(Exception, match="approval_timeout"):
        await approvals.approve(
            session_id=intent.session_id,
            call_id=intent.call_id,
            command_hash=intent.command_hash,
        )


@pytest.mark.asyncio
async def test_reject_and_cancel_after_expiry_preserve_approval_timeout(
    tmp_path: Path,
) -> None:
    from workbench.backend.runtime.approval import (
        ApprovalIntegrityError,
        ApprovalManager,
        CommandIntent,
    )

    intent = CommandIntent.create(
        session_id="session-expired-decision",
        call_id="call-expired-decision",
        command="python task.py",
        cwd=tmp_path,
        requested_permission="project_write",
    )
    approvals = ApprovalManager()
    await approvals.request(intent, approval_timeout_seconds=0.001)
    await asyncio.sleep(0.01)

    for decision in (approvals.reject, approvals.cancel):
        with pytest.raises(ApprovalIntegrityError, match="approval_timeout"):
            await decision(
                session_id=intent.session_id,
                call_id=intent.call_id,
                command_hash=intent.command_hash,
            )
        assert (
            await approvals.get(session_id=intent.session_id, call_id=intent.call_id)
        ).status.value == "approval_timeout"


@pytest.mark.asyncio
async def test_native_approval_waiter_is_released_by_matching_http_decision(
    tmp_path: Path,
) -> None:
    from workbench.backend.runtime.approval import (
        ApprovalManager,
        ApprovalState,
        CommandIntent,
    )

    intent = CommandIntent.create(
        session_id="native-session",
        call_id="native-call",
        command="python task.py",
        cwd=tmp_path,
        requested_permission="process_spawn",
    )
    approvals = ApprovalManager()
    pending = await approvals.request(intent)
    waiter = asyncio.create_task(approvals.wait_for_terminal(intent))
    await asyncio.sleep(0)

    decided = await approvals.approve(
        session_id=intent.session_id,
        call_id=intent.call_id,
        command_hash=intent.command_hash,
    )
    observed = await asyncio.wait_for(waiter, timeout=1)

    assert pending.status is ApprovalState.PENDING
    assert decided.status is ApprovalState.APPROVED
    assert observed.status is ApprovalState.APPROVED


@pytest.mark.asyncio
async def test_native_approval_waiter_expires_without_starting_a_process(
    tmp_path: Path,
) -> None:
    from workbench.backend.runtime.approval import (
        ApprovalManager,
        ApprovalState,
        CommandIntent,
    )

    intent = CommandIntent.create(
        session_id="native-timeout-session",
        call_id="native-timeout-call",
        command="python task.py",
        cwd=tmp_path,
        requested_permission="process_spawn",
    )
    approvals = ApprovalManager()
    await approvals.request(intent, approval_timeout_seconds=10)

    observed = await approvals.wait_for_terminal(intent, timeout_seconds=0.01)

    assert observed.status is ApprovalState.APPROVAL_TIMEOUT


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", "level_a"),
        ("git -C /tmp/outside status", "level_b"),
        ("git diff --no-index /tmp/a /tmp/b", "level_b"),
        ("git diff ../outside", "level_b"),
        ("python tokenizer.py", "level_b"),
        ("cat token.txt", "level_c"),
        ("git show HEAD:.env", "level_c"),
        ("/mnt/c/Windows/System32/cmd.exe /c echo ok", "level_b"),
        ("powershell -NoProfile -Command Get-Process", "level_b"),
        ("powershell -NoProfile -Command Remove-Item build", "level_c"),
        ("Remove-Item -Recurse -Force C:\\\\build", "level_c"),
        ("Set-ExecutionPolicy Bypass", "level_c"),
        ("wsl --unregister Ubuntu", "level_c"),
        ("reg query HKLM\\\\Software\\\\Acme", "level_b"),
        ("sc query AcmeService", "level_b"),
        ("systemctl status ssh", "level_b"),
        ("chmod +x scripts/check.sh", "level_b"),
        ("curl -sf http://127.0.0.1:8000/health", "level_a"),
        ("curl -X POST http://127.0.0.1:8000/health", "level_b"),
        ("pytest tests/unit", "level_a"),
        ("npm install package", "level_b"),
        ("rm -rf /", "level_c"),
    ],
)
def test_command_risk_classification(
    command: str, expected: str, tmp_path: Path
) -> None:
    from workbench.backend.runtime.approval import CommandIntent

    intent = CommandIntent.create(
        session_id="session-risk",
        call_id=f"call-{expected}",
        command=command,
        cwd=tmp_path,
        requested_permission="process_spawn",
    )

    assert intent.risk.value == expected


@pytest.mark.parametrize(
    "command",
    [
        "npm test && rm -rf build",
        "git status; rm -rf build",
        "git status | cat",
        "echo safe & rm -rf build",
        "(rm -rf build)",
        "$(whoami)",
        "echo $(whoami)",
    ],
)
def test_command_intent_rejects_shell_composition(command: str, tmp_path: Path) -> None:
    from workbench.backend.runtime.approval import CommandIntent, CommandSyntaxError

    with pytest.raises(CommandSyntaxError):
        CommandIntent.create(
            session_id="session-parser",
            call_id="call-parser",
            command=command,
            cwd=tmp_path,
            requested_permission="process_spawn",
        )


@pytest.mark.asyncio
async def test_background_job_is_not_killed_when_readiness_wait_ends(
    tmp_path: Path,
) -> None:
    from workbench.backend.runtime.approval import (
        ApprovalManager,
        CommandIntent,
    )
    from workbench.backend.runtime.jobs import ApprovalExecutor, JobRuntime, JobState

    port = _free_port()
    server = tmp_path / "server.py"
    server.write_text(
        "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "    def log_message(self, *_args):\n"
        "        return\n"
        "HTTPServer(('127.0.0.1', int(__import__('sys').argv[1])), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    intent = CommandIntent.create(
        session_id="session-job",
        call_id="call-job",
        argv=[sys.executable, str(server), str(port)],
        cwd=tmp_path,
        requested_permission="background_service",
    )
    approvals = ApprovalManager()
    await approvals.request(intent)
    await approvals.approve(
        session_id=intent.session_id,
        call_id=intent.call_id,
        command_hash=intent.command_hash,
    )
    jobs = JobRuntime()
    executor = ApprovalExecutor(approvals, jobs)

    job = await executor.execute(
        intent,
        background=True,
        port=port,
        health_url=f"http://127.0.0.1:{port}/health",
        job_wait_timeout_seconds=0.2,
    )

    assert job.status is JobState.JOB_RUNNING
    assert job.job_id
    assert job.pid is not None
    assert job.port == port
    assert await jobs.is_process_alive(job.job_id) is True
    try:
        await asyncio.sleep(0.05)
        assert await jobs.is_process_alive(job.job_id) is True
    finally:
        await jobs.stop(job.job_id)


@pytest.mark.asyncio
async def test_executor_recomputes_identity_before_spawn(tmp_path: Path) -> None:
    from workbench.backend.runtime.approval import (
        ApprovalIntegrityError,
        ApprovalManager,
        CommandIntent,
    )
    from workbench.backend.runtime.jobs import ApprovalExecutor, JobRuntime

    approved_intent = CommandIntent.create(
        session_id="session-integrity",
        call_id="call-integrity",
        command="pwd",
        cwd=tmp_path,
        requested_permission="process_spawn",
    )
    approvals = ApprovalManager()
    await approvals.request(approved_intent)
    jobs = JobRuntime()
    executor = ApprovalExecutor(approvals, jobs)

    # A caller cannot mutate the frozen intent, but a forged object with the
    # old hash must still be rejected before JobRuntime can spawn anything.
    forged = replace(approved_intent, argv=("sh", "-c", "echo compromised"))
    with pytest.raises(ApprovalIntegrityError, match="approval_integrity_mismatch"):
        await executor.execute(forged, background=False)


@pytest.mark.asyncio
async def test_job_runtime_rejects_direct_unconsumed_approval(tmp_path: Path) -> None:
    from workbench.backend.runtime.approval import ApprovalManager, CommandIntent
    from workbench.backend.runtime.jobs import JobRuntime

    intent = CommandIntent.create(
        session_id="session-direct",
        call_id="call-direct",
        command="pwd",
        cwd=tmp_path,
        requested_permission="process_spawn",
    )
    approvals = ApprovalManager()
    pending = await approvals.request(intent)
    jobs = JobRuntime()

    with pytest.raises(PermissionError, match="executor-only"):
        await jobs.start(pending, background=False)


@pytest.mark.asyncio
async def test_missing_executable_reports_process_failed_without_a_job(
    tmp_path: Path,
) -> None:
    from workbench.backend.runtime.approval import ApprovalManager, CommandIntent
    from workbench.backend.runtime.jobs import (
        ApprovalExecutor,
        JobRuntime,
        JobRuntimeError,
    )

    intent = CommandIntent.create(
        session_id="session-process-failed",
        call_id="call-process-failed",
        argv=[str(tmp_path / "does-not-exist")],
        cwd=tmp_path,
        requested_permission="process_spawn",
    )
    approvals = ApprovalManager()
    await approvals.request(intent)
    await approvals.approve(
        session_id=intent.session_id,
        call_id=intent.call_id,
        command_hash=intent.command_hash,
    )

    with pytest.raises(JobRuntimeError, match="process_failed"):
        await ApprovalExecutor(approvals, JobRuntime()).execute(
            intent, background=False
        )


@pytest.mark.asyncio
async def test_invalid_readiness_options_do_not_consume_approval(
    tmp_path: Path,
) -> None:
    from workbench.backend.runtime.approval import (
        ApprovalManager,
        CommandIntent,
    )
    from workbench.backend.runtime.jobs import ApprovalExecutor, JobRuntime

    intent = CommandIntent.create(
        session_id="session-invalid-options",
        call_id="call-invalid-options",
        command="pwd",
        cwd=tmp_path,
        requested_permission="process_spawn",
    )
    approvals = ApprovalManager()
    await approvals.request(intent)
    jobs = JobRuntime()
    executor = ApprovalExecutor(approvals, jobs)

    with pytest.raises(ValueError, match="loopback"):
        await executor.execute(
            intent,
            background=True,
            health_url="http://example.invalid/health",
        )

    with pytest.raises(ValueError, match="foreground"):
        await executor.execute(
            intent,
            background=True,
            process_timeout_seconds=1,
        )

    with pytest.raises(ValueError, match="readiness"):
        await executor.execute(intent, background=True)

    assert (
        await approvals.get(session_id=intent.session_id, call_id=intent.call_id)
    ).status.value == "approved"


@pytest.mark.asyncio
async def test_job_runtime_rejects_approved_record_without_executor_consumption(
    tmp_path: Path,
) -> None:
    from workbench.backend.runtime.approval import ApprovalManager, CommandIntent
    from workbench.backend.runtime.jobs import JobRuntime

    intent = CommandIntent.create(
        session_id="session-direct-approved",
        call_id="call-direct-approved",
        command="pwd",
        cwd=tmp_path,
        requested_permission="process_spawn",
    )
    approvals = ApprovalManager()
    approved = await approvals.request(intent)
    jobs = JobRuntime()

    with pytest.raises(PermissionError, match="approval_unavailable"):
        await jobs.start(
            approved,
            background=False,
            _executor_token=jobs._executor_token,
        )


@pytest.mark.asyncio
async def test_foreground_process_timeout_terminates_only_owned_process(
    tmp_path: Path,
) -> None:
    from workbench.backend.runtime.approval import ApprovalManager, CommandIntent
    from workbench.backend.runtime.jobs import ApprovalExecutor, JobRuntime, JobState

    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    intent = CommandIntent.create(
        session_id="session-process-timeout",
        call_id="call-process-timeout",
        argv=[sys.executable, str(script)],
        cwd=tmp_path,
        requested_permission="foreground_task",
    )
    approvals = ApprovalManager()
    await approvals.request(intent)
    await approvals.approve(
        session_id=intent.session_id,
        call_id=intent.call_id,
        command_hash=intent.command_hash,
    )
    jobs = JobRuntime()

    job = await ApprovalExecutor(approvals, jobs).execute(
        intent,
        background=False,
        process_timeout_seconds=0.1,
    )

    assert job.status is JobState.PROCESS_TIMEOUT
    assert await jobs.is_process_alive(job.job_id) is False


@pytest.mark.asyncio
async def test_workbench_approval_api_enforces_one_shot_and_integrity(
    tmp_path: Path,
) -> None:
    from httpx import ASGITransport, AsyncClient

    from workbench.backend import main as main_module
    from workbench.backend.main import WorkbenchService
    from workbench.backend.runtime.auth import WorkbenchAuth

    script = tmp_path / "task.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    service = WorkbenchService(
        workspace_root=tmp_path,
        event_log_path=tmp_path / "events.jsonl",
        state_path=tmp_path / "state.json",
    )
    previous_service = main_module.service
    previous_auth = main_module.auth
    main_module.service = service
    main_module.auth = WorkbenchAuth("test-token")
    headers = {"Authorization": "Bearer test-token"}
    request_body = {
        "session_id": "api-session",
        "call_id": "api-call",
        "argv": [sys.executable, str(script)],
        "cwd": str(tmp_path),
        "requested_permission": "project_write",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=main_module.app), base_url="http://test"
        ) as client:
            unauthorized = await client.post("/api/approvals", json=request_body)
            requested = await client.post(
                "/api/approvals", json=request_body, headers=headers
            )
            approval = requested.json()
            approved = await client.post(
                "/api/approvals/api-session/api-call/approve",
                json={"command_hash": approval["command_hash"]},
                headers=headers,
            )
            executed = await client.post(
                "/api/approvals/api-session/api-call/execute",
                json={
                    **request_body,
                    "background": False,
                    "process_timeout_seconds": 2,
                },
                headers=headers,
            )
            second_execute = await client.post(
                "/api/approvals/api-session/api-call/execute",
                json={**request_body, "background": False},
                headers=headers,
            )
    finally:
        main_module.service = previous_service
        main_module.auth = previous_auth

    assert unauthorized.status_code == 401
    assert requested.status_code == 200
    assert approval["status"] == "pending"
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert executed.status_code == 200
    assert executed.json()["status"] == "job_completed"
    assert second_execute.status_code == 409
    assert second_execute.json()["error"]["code"] == "approval_unavailable"


@pytest.mark.asyncio
async def test_workbench_approval_api_blocks_integrity_mismatch_and_timeout(
    tmp_path: Path,
) -> None:
    from httpx import ASGITransport, AsyncClient

    from workbench.backend import main as main_module
    from workbench.backend.main import WorkbenchService
    from workbench.backend.runtime.auth import WorkbenchAuth

    script = tmp_path / "task.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    service = WorkbenchService(
        workspace_root=tmp_path,
        event_log_path=tmp_path / "events.jsonl",
        state_path=tmp_path / "state.json",
    )
    previous_service = main_module.service
    previous_auth = main_module.auth
    main_module.service = service
    main_module.auth = WorkbenchAuth("test-token")
    headers = {"Authorization": "Bearer test-token"}

    def body(call_id: str) -> dict[str, object]:
        return {
            "session_id": "api-session",
            "call_id": call_id,
            "argv": [sys.executable, str(script)],
            "cwd": str(tmp_path),
            "requested_permission": "project_write",
        }

    try:
        async with AsyncClient(
            transport=ASGITransport(app=main_module.app), base_url="http://test"
        ) as client:
            timeout_request = await client.post(
                "/api/approvals",
                json={**body("timeout-call"), "approval_timeout_seconds": 0},
                headers=headers,
            )
            timeout_execute = await client.post(
                "/api/approvals/api-session/timeout-call/execute",
                json=body("timeout-call"),
                headers=headers,
            )
            mismatch_request = await client.post(
                "/api/approvals", json=body("mismatch-call"), headers=headers
            )
            mismatch_hash = mismatch_request.json()["command_hash"]
            await client.post(
                "/api/approvals/api-session/mismatch-call/approve",
                json={"command_hash": mismatch_hash},
                headers=headers,
            )
            mismatch_execute = await client.post(
                "/api/approvals/api-session/mismatch-call/execute",
                json={
                    **body("mismatch-call"),
                    "argv": [sys.executable, str(tmp_path / "other.py")],
                },
                headers=headers,
            )
            dangerous = await client.post(
                "/api/approvals",
                json={
                    "session_id": "api-session",
                    "call_id": "danger-call",
                    "command": "rm -rf ./build",
                    "cwd": str(tmp_path),
                    "requested_permission": "project_write",
                },
                headers=headers,
            )
    finally:
        main_module.service = previous_service
        main_module.auth = previous_auth

    assert timeout_request.json()["status"] == "approval_timeout"
    assert timeout_execute.status_code == 409
    assert timeout_execute.json()["error"]["code"] == "approval_timeout"
    assert mismatch_execute.status_code == 409
    assert mismatch_execute.json()["error"]["code"] == "approval_integrity_mismatch"
    assert dangerous.status_code == 200
    assert dangerous.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_workbench_job_api_keeps_background_service_running_until_stopped(
    tmp_path: Path,
) -> None:
    from httpx import ASGITransport, AsyncClient

    from workbench.backend import main as main_module
    from workbench.backend.main import WorkbenchService
    from workbench.backend.runtime.auth import WorkbenchAuth

    port = _free_port()
    server = tmp_path / "api_server.py"
    server.write_text(
        "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "    def log_message(self, *_args):\n"
        "        return\n"
        "HTTPServer(('127.0.0.1', int(__import__('sys').argv[1])), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    service = WorkbenchService(
        workspace_root=tmp_path,
        event_log_path=tmp_path / "events.jsonl",
        state_path=tmp_path / "state.json",
    )
    previous_service = main_module.service
    previous_auth = main_module.auth
    main_module.service = service
    main_module.auth = WorkbenchAuth("test-token")
    headers = {"Authorization": "Bearer test-token"}
    body = {
        "session_id": "api-job-session",
        "call_id": "api-job-call",
        "argv": [sys.executable, str(server), str(port)],
        "cwd": str(tmp_path),
        "requested_permission": "background_service",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=main_module.app), base_url="http://test"
        ) as client:
            requested = await client.post("/api/approvals", json=body, headers=headers)
            approval = requested.json()
            await client.post(
                "/api/approvals/api-job-session/api-job-call/approve",
                json={"command_hash": approval["command_hash"]},
                headers=headers,
            )
            executed = await client.post(
                "/api/approvals/api-job-session/api-job-call/execute",
                json={
                    **body,
                    "background": True,
                    "port": port,
                    "health_url": f"http://127.0.0.1:{port}/health",
                    "job_wait_timeout_seconds": 2,
                },
                headers=headers,
            )
            job = executed.json()
            queried = await client.get(f"/api/jobs/{job['job_id']}", headers=headers)
            stopped = await client.post(
                f"/api/jobs/{job['job_id']}/stop", headers=headers
            )
    finally:
        await service.cleanup()
        main_module.service = previous_service
        main_module.auth = previous_auth

    assert requested.status_code == 200
    assert executed.status_code == 200
    assert job["status"] == "job_running"
    assert job["pid"] > 0
    assert job["port"] == port
    assert job["health_url"] == f"http://127.0.0.1:{port}/health"
    assert job["ready"] is True
    assert queried.status_code == 200
    assert queried.json()["status"] == "job_running"
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "job_cancelled"

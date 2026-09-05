from __future__ import annotations

import asyncio

import pytest

from runner.service import (
    CHANNELS,
    RunnerManager,
    RunRecord,
    StartRequest,
    UnsupportedFieldError,
    _run_process,
    build_scraper_argv,
    validate_start_payload,
)


def test_build_scraper_argv_uses_fixed_channel_mapping() -> None:
    argv = build_scraper_argv("xhs", limit=20, restart=False, smoke=False)

    assert argv[:2] == ["python3", "/home/gnen/scraper/main.py"]
    assert argv[2:] == [
        "--mode",
        "search",
        "--platform",
        "xhs",
        "--port",
        "9222",
        "--keyword-group",
        "kaka_camera",
        "--limit",
        "20",
        "--slow-mode",
        "true",
    ]


def test_build_scraper_argv_smoke_is_valid_for_scraper_cli() -> None:
    argv = build_scraper_argv("douyin", limit=30, restart=True, smoke=True)

    assert argv[0:2] == ["python3", "/home/gnen/scraper/main.py"]
    assert "--keyword" in argv
    assert argv[argv.index("--keyword") + 1] == "AI写真"
    assert "--smoke-test" in argv
    assert "--restart" in argv
    assert argv[argv.index("--platform") + 1] == "douyin"
    assert argv[argv.index("--port") + 1] == "9224"


def test_build_scraper_argv_smoke_keyword_is_not_client_configurable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("RUNNER_SMOKE_KEYWORD", "; whoami")

    argv = build_scraper_argv("xhs", limit=1, restart=False, smoke=True)

    assert argv[argv.index("--keyword") + 1] == "AI写真"


def test_validate_start_payload_rejects_unknown_fields() -> None:
    with pytest.raises(UnsupportedFieldError) as caught:
        validate_start_payload({"channel": "xhs", "command": "rm -rf /"})

    assert caught.value.fields == ["command"]


def test_validate_start_payload_applies_safe_defaults_and_limits() -> None:
    request = validate_start_payload({"channel": "xhs", "limit": 999})

    assert request.channel == "xhs"
    assert request.limit == 100
    assert request.boot is True
    assert request.restart is False
    assert request.smoke is False
    assert request.kill_chrome is False


def test_channel_registry_has_separate_cdp_ports() -> None:
    assert CHANNELS["xhs"].cdp_port == 9222
    assert CHANNELS["douyin"].cdp_port == 9224


@pytest.mark.asyncio
async def test_manager_captures_real_output_and_imports_json(
    tmp_path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    output = data_dir / "search_xhs_test.json"
    script = tmp_path / "main.py"
    script.write_text(
        "import json, pathlib\n"
        f"p = pathlib.Path({str(output)!r})\n"
        "p.write_text(json.dumps({'data': [{'id': 1}, {'id': 2}]}), encoding='utf-8')\n"
        "print('采集开始', flush=True)\n"
        "print('输出: ' + str(p), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("runner.service.cdp_healthy", lambda _port: True)
    manager = RunnerManager(scraper_root=tmp_path, ledger_path=tmp_path / "ledger.json")

    started = await manager.start(StartRequest(channel="xhs", boot=False, limit=2))
    run_id = started["run_id"]
    for _ in range(50):
        current = manager.get(run_id)
        if current.status == "completed":
            break
        await asyncio.sleep(0.01)

    assert current.status == "completed"
    assert current.exit_code == 0
    assert manager.logs(run_id)["lines"][:1] == ["采集开始"]
    imported = await manager.import_output(run_id)
    assert imported["record_count"] == 2
    assert len(imported["sha256"]) == 64
    registered = await manager.register(run_id)
    assert registered["registered"] is True


@pytest.mark.asyncio
async def test_run_process_preserves_negative_exit_code(monkeypatch) -> None:
    class FakeProcess:
        returncode = -15

        async def communicate(self):
            return b"terminated\n", None

        async def wait(self):
            return self.returncode

        def kill(self):
            raise AssertionError("process should not be killed")

    async def fake_create(*_argv, **_kwargs):
        return FakeProcess()

    monkeypatch.setattr("runner.service.asyncio.create_subprocess_exec", fake_create)

    code, output = await _run_process(["fake"], timeout=1)

    assert code == -15
    assert output == "terminated\n"


@pytest.mark.asyncio
async def test_watcher_accepts_text_lines_and_marks_nonzero_exit_failed() -> None:
    class FakeStdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, "done", False):
                raise StopAsyncIteration
            self.done = True
            return "采集失败\n"

    class FakeProcess:
        pid = 4321
        stdout = FakeStdout()

        async def wait(self):
            return 2

    record = RunRecord(
        run_id="failed01",
        channel="xhs",
        argv=[],
        limit=1,
        restart=False,
        smoke=False,
        kill_chrome=False,
        status="running",
        pid=4321,
        process=FakeProcess(),
    )

    await RunnerManager()._watch(record)

    assert record.status == "failed"
    assert record.exit_code == 2
    assert record.lines == ["采集失败"]


def test_read_ledger_discards_non_object_entries(tmp_path) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text('[{"run_id": "ok"}, 1, "bad", null]', encoding="utf-8")

    entries = RunnerManager(ledger_path=ledger)._read_ledger()

    assert entries == [{"run_id": "ok"}]

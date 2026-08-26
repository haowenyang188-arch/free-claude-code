from __future__ import annotations

import asyncio

import pytest

from cli.runtime_registry import RuntimeBackend, RuntimeRegistry


@pytest.mark.asyncio
async def test_probe_parses_version_and_caches_result(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def runner(argv, _timeout):
        calls.append(tuple(argv))
        return 0, b"codex-cli 0.149.1\n", b""

    monkeypatch.setattr("cli.runtime_registry.shutil.which", lambda _: "/usr/bin/codex")
    registry = RuntimeRegistry(
        executables={RuntimeBackend.CODEX: "codex"},
        runner=runner,
    )

    first = await registry.probe("codex")
    second = await registry.probe(RuntimeBackend.CODEX)

    assert first is second
    assert first.available is True
    assert first.version == "0.149.1"
    assert "sandbox" in first.capabilities
    assert calls == [("codex", "--version")]


@pytest.mark.asyncio
async def test_probe_missing_executable_is_safe_and_does_not_run(monkeypatch) -> None:
    called = False

    async def runner(_argv, _timeout):
        nonlocal called
        called = True
        return 0, b"claude 2.1.220", b""

    monkeypatch.setattr("cli.runtime_registry.shutil.which", lambda _: None)
    registry = RuntimeRegistry(runner=runner)

    result = await registry.probe("claude")

    assert result.available is False
    assert result.reason == "executable_not_found"
    assert result.version is None
    assert called is False
    assert result.to_mapping()["executable"] == "claude"


@pytest.mark.asyncio
async def test_probe_timeout_is_redacted(monkeypatch) -> None:
    async def runner(_argv, _timeout):
        await asyncio.sleep(0.05)
        return 0, b"claude 2.1.220", b"secret-token"

    monkeypatch.setattr("cli.runtime_registry.shutil.which", lambda _: "/usr/bin/claude")
    registry = RuntimeRegistry(timeout_seconds=0.001, runner=runner)

    result = await registry.probe("claude")

    assert result.available is False
    assert result.reason == "probe_timeout"
    assert "secret" not in repr(result.to_mapping()).lower()


@pytest.mark.asyncio
async def test_probe_all_isolates_backend_failures(monkeypatch) -> None:
    async def runner(argv, _timeout):
        if argv[0] == "claude":
            raise OSError("missing binary")
        return 0, b"codex 0.149.1", b""

    monkeypatch.setattr("cli.runtime_registry.shutil.which", lambda _: "/bin/tool")
    registry = RuntimeRegistry(runner=runner)

    results = await registry.probe_all()

    assert results["claude"].available is False
    assert results["claude"].reason == "probe_failed"
    assert results["codex"].available is True


def test_registry_rejects_unknown_backends_and_bad_limits() -> None:
    with pytest.raises(ValueError, match="unsupported runtime backend"):
        RuntimeRegistry(executables={"shell": "sh"})
    with pytest.raises(ValueError, match="timeout_seconds"):
        RuntimeRegistry(timeout_seconds=0)
    with pytest.raises(ValueError, match="cache_ttl_seconds"):
        RuntimeRegistry(cache_ttl_seconds=-1)

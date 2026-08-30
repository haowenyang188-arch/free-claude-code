from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def test_prepare_codex_home_links_native_config_and_auth(tmp_path: Path) -> None:
    from workbench.backend.runtime.codex_home import prepare_codex_home

    source = tmp_path / "source"
    target = tmp_path / "runtime" / "codex-home"
    source.mkdir()
    (source / "config.toml").write_text('model_provider = "custom"\n', encoding="utf-8")
    (source / "auth.json").write_text('{"OPENAI_API_KEY":"secret"}\n', encoding="utf-8")

    prepared = prepare_codex_home(target, source=source)

    assert prepared == target.resolve()
    assert (prepared / "config.toml").is_symlink()
    assert (prepared / "auth.json").is_symlink()
    assert (prepared / "config.toml").resolve() == (source / "config.toml").resolve()
    assert (prepared / "auth.json").resolve() == (source / "auth.json").resolve()
    assert not (prepared / "config.toml").samefile(prepared / "auth.json")


def test_prepare_codex_home_rejects_source_as_target(tmp_path: Path) -> None:
    from workbench.backend.runtime.codex_home import CodexHomeError, prepare_codex_home

    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text("", encoding="utf-8")

    with pytest.raises(CodexHomeError, match="separate"):
        prepare_codex_home(source, source=source)


@pytest.mark.asyncio
async def test_codex_app_server_projects_workbench_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workbench.backend.agents import codex_app_server as module
    from workbench.backend.agents.codex_app_server import CodexAppServerSession

    source = tmp_path / "source"
    target = tmp_path / "runtime" / "codex-home"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    (source / "config.toml").write_text("model = 'gpt-5.6-sol'\n", encoding="utf-8")
    (source / "auth.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(source))

    process = SimpleNamespace(pid=0, returncode=None)
    monkeypatch.setattr(module, "register_process", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "unregister_process", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "build_cli_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(CodexAppServerSession, "_initialize", AsyncMock())

    session = CodexAppServerSession(workspace, codex_home=target)
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
        spawn.return_value = process
        await session._ensure_started()

    assert spawn.await_args is not None
    environment = spawn.await_args.kwargs["env"]
    assert environment["CODEX_HOME"] == str(target.resolve())
    assert (target / "config.toml").is_symlink()
    assert (target / "auth.json").is_symlink()

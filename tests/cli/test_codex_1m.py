"""Tests for the managed Codex 1M default configuration."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from cli.codex_1m import (
    AUTO_COMPACT_TOKEN_LIMIT,
    CATALOG_OWNER_CONTENT,
    CATALOG_OWNER_RELATIVE_PATH,
    CATALOG_RELATIVE_PATH,
    CONFIG_MARKER_BEGIN,
    CONFIG_MARKER_END,
    DESIRED_CONTEXT_WINDOW,
    MODEL_SLUG,
    CodexTarget,
    CommandResult,
    ConfigurationBlocked,
    ConfigurationOwnershipError,
    _prepare_catalog_content,
    _runtime_path,
    configure_target,
    inspect_target,
    main,
    resolve_target,
)


def _catalog(max_context_window: int = 272_000) -> dict[str, Any]:
    return {
        "models": [
            {
                "slug": MODEL_SLUG,
                "display_name": "GPT-5.6-Sol",
                "context_window": max_context_window,
                "max_context_window": max_context_window,
                "effective_context_window_percent": 95,
                "supports_parallel_tool_calls": True,
            },
            {
                "slug": "gpt-5.6-terra",
                "display_name": "GPT-5.6-Terra",
                "context_window": 272_000,
                "max_context_window": 272_000,
                "effective_context_window_percent": 95,
            },
        ]
    }


class FakeCodexRunner:
    """Return deterministic diagnostics and honor the configured catalog path."""

    def __init__(
        self,
        *,
        bundled_max_context_window: int = 272_000,
        ignore_custom_catalog: bool = False,
        include_target_model: bool = True,
    ) -> None:
        self.bundled_max_context_window = bundled_max_context_window
        self.ignore_custom_catalog = ignore_custom_catalog
        self.include_target_model = include_target_model

    def _bundled_catalog(self) -> dict[str, object]:
        payload = _catalog(self.bundled_max_context_window)
        if not self.include_target_model:
            payload["models"] = [
                model for model in payload["models"] if model["slug"] != MODEL_SLUG
            ]
        return payload

    def __call__(
        self,
        args: Sequence[str],
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        del timeout
        command = list(args[1:])
        if command == ["--version"]:
            return CommandResult(0, "codex-cli 0.147.0\n", "")
        if command == ["login", "status"]:
            return CommandResult(0, "Logged in using an API key - sk-test-secret\n", "")
        if command == ["debug", "models", "--bundled"]:
            return CommandResult(0, json.dumps(self._bundled_catalog()), "")
        if command == ["debug", "models"]:
            payload = self._bundled_catalog()
            config_path = Path(env["CODEX_HOME"]) / "config.toml"
            if config_path.exists() and not self.ignore_custom_catalog:
                config = tomllib.loads(config_path.read_text("utf-8"))
                catalog_path = config.get("model_catalog_json")
                if catalog_path is not None:
                    payload = json.loads(Path(catalog_path).read_text("utf-8"))
            return CommandResult(0, json.dumps(payload), "")
        raise AssertionError(f"Unexpected command: {command}")


class ConfigErrorRunner(FakeCodexRunner):
    """Simulate a parser error that echoes credential-like config fields."""

    def __call__(
        self,
        args: Sequence[str],
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        if list(args[1:]) == ["debug", "models"]:
            return CommandResult(
                1,
                "",
                'config.toml: api_key = "super-secret" token=abc123 sk-live-456',
            )
        return super().__call__(args, env, timeout)


class StderrAuthRunner(FakeCodexRunner):
    """Match Codex 0.147, which reports successful auth status on stderr."""

    def __call__(
        self,
        args: Sequence[str],
        env: dict[str, str],
        timeout: float,
    ) -> CommandResult:
        if list(args[1:]) == ["login", "status"]:
            return CommandResult(0, "", "Logged in using an API key - sk-hidden")
        return super().__call__(args, env, timeout)


def _target(tmp_path: Path) -> CodexTarget:
    return CodexTarget(kind="wsl", executable="codex", home=tmp_path / ".codex")


def test_status_reports_current_272k_catalog_before_configuration(
    tmp_path: Path,
) -> None:
    status = inspect_target(_target(tmp_path), runner=FakeCodexRunner())

    assert status.catalog_max_context_window == 272_000
    assert status.effective_context_window == 258_400
    assert status.eligible is True
    assert status.ready is False
    assert any("default 1M configuration" in item for item in status.readiness_blockers)
    assert "sk-test-secret" not in json.dumps(status.to_dict())


def test_configure_installs_default_config_and_managed_catalog(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    target.home.mkdir(parents=True)
    original_config = 'approval_policy = "on-request"\n\n[features]\ngoals = true\n'
    (target.home / "config.toml").write_text(original_config, encoding="utf-8")

    status = configure_target(target, runner=FakeCodexRunner())

    config_content = (target.home / "config.toml").read_text("utf-8")
    config = tomllib.loads(config_content)
    assert config_content.count(CONFIG_MARKER_BEGIN) == 1
    assert config_content.count(CONFIG_MARKER_END) == 1
    assert original_config in config_content
    assert config["model"] == MODEL_SLUG
    assert config["model_context_window"] == DESIRED_CONTEXT_WINDOW
    assert config["model_auto_compact_token_limit"] == AUTO_COMPACT_TOKEN_LIMIT
    assert config["model_catalog_json"] == str(target.home / CATALOG_RELATIVE_PATH)

    catalog = json.loads((target.home / CATALOG_RELATIVE_PATH).read_text("utf-8"))
    model = next(item for item in catalog["models"] if item["slug"] == MODEL_SLUG)
    terra = next(item for item in catalog["models"] if item["slug"] == "gpt-5.6-terra")
    assert model["context_window"] == DESIRED_CONTEXT_WINDOW
    assert model["max_context_window"] == DESIRED_CONTEXT_WINDOW
    assert model["supports_parallel_tool_calls"] is False
    assert terra == _catalog()["models"][1]
    assert (target.home / CATALOG_OWNER_RELATIVE_PATH).read_text(
        "utf-8"
    ) == CATALOG_OWNER_CONTENT
    assert status.ready is True
    assert status.effective_context_window == 950_000


def test_configure_is_idempotent_for_managed_files(tmp_path: Path) -> None:
    target = _target(tmp_path)
    runner = FakeCodexRunner()

    configure_target(target, runner=runner)
    first_config = (target.home / "config.toml").read_bytes()
    first_catalog = (target.home / CATALOG_RELATIVE_PATH).read_bytes()
    status = configure_target(target, runner=runner)

    assert (target.home / "config.toml").read_bytes() == first_config
    assert (target.home / CATALOG_RELATIVE_PATH).read_bytes() == first_catalog
    assert status.ready is True


def test_prepare_catalog_disables_parallel_calls_on_duplicate_target_models() -> None:
    payload = _catalog()
    payload["models"].append(
        {
            "slug": MODEL_SLUG,
            "display_name": "GPT-5.6-Sol duplicate",
            "context_window": 272_000,
            "max_context_window": 272_000,
            "supports_parallel_tool_calls": True,
        }
    )

    content = _prepare_catalog_content(CommandResult(0, json.dumps(payload), ""))
    catalog = json.loads(content)

    assert [
        item["supports_parallel_tool_calls"]
        for item in catalog["models"]
        if item["slug"] == MODEL_SLUG
    ] == [False, False]


def test_configure_refuses_unmanaged_default_keys(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.home.mkdir(parents=True)
    config_path = target.home / "config.toml"
    original = "model_context_window = 500000\n"
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigurationOwnershipError, match="unmanaged"):
        configure_target(target, runner=FakeCodexRunner())

    assert config_path.read_text("utf-8") == original
    assert not (target.home / CATALOG_RELATIVE_PATH).exists()


def test_configure_refuses_malformed_managed_block(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.home.mkdir(parents=True)
    config_path = target.home / "config.toml"
    original = CONFIG_MARKER_BEGIN + f'model = "{MODEL_SLUG}"\n'
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigurationOwnershipError, match="marker"):
        configure_target(target, runner=FakeCodexRunner())

    assert config_path.read_text("utf-8") == original


def test_configure_refuses_unmanaged_catalog_file(tmp_path: Path) -> None:
    target = _target(tmp_path)
    catalog_path = target.home / CATALOG_RELATIVE_PATH
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(json.dumps(_catalog()), encoding="utf-8")

    with pytest.raises(ConfigurationOwnershipError, match="catalog"):
        configure_target(target, runner=FakeCodexRunner())

    assert not (target.home / "config.toml").exists()


def test_configure_rolls_back_all_files_when_runtime_ignores_catalog(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    target.home.mkdir(parents=True)
    config_path = target.home / "config.toml"
    original = 'approval_policy = "on-request"\n'
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ConfigurationBlocked, match="verification failed"):
        configure_target(
            target,
            runner=FakeCodexRunner(ignore_custom_catalog=True),
        )

    assert config_path.read_text("utf-8") == original
    assert not (target.home / CATALOG_RELATIVE_PATH).exists()
    assert not (target.home / CATALOG_OWNER_RELATIVE_PATH).exists()


def test_configure_rejects_bundled_catalog_without_target_model(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)

    with pytest.raises(ConfigurationBlocked, match=MODEL_SLUG):
        configure_target(
            target,
            runner=FakeCodexRunner(include_target_model=False),
        )

    assert not (target.home / "config.toml").exists()


def test_status_redacts_credentials_echoed_by_codex_errors(tmp_path: Path) -> None:
    status = inspect_target(_target(tmp_path), runner=ConfigErrorRunner())

    serialized = json.dumps(status.to_dict())
    assert "super-secret" not in serialized
    assert "abc123" not in serialized
    assert "sk-live-456" not in serialized


def test_status_reads_redacted_auth_mode_from_stderr(tmp_path: Path) -> None:
    status = inspect_target(_target(tmp_path), runner=StderrAuthRunner())

    assert status.auth_mode == "api_key"
    assert "sk-hidden" not in json.dumps(status.to_dict())


def test_resolve_target_honors_explicit_wsl_paths(tmp_path: Path) -> None:
    target = resolve_target(
        "wsl",
        codex_bin="/opt/codex/bin/codex",
        codex_home=tmp_path / "codex-home",
    )

    assert target == CodexTarget(
        kind="wsl",
        executable="/opt/codex/bin/codex",
        home=tmp_path / "codex-home",
    )


@pytest.mark.skipif(
    os.name == "nt", reason="WSL path conversion is not used on Windows"
)
def test_windows_catalog_path_is_written_in_native_form() -> None:
    target = CodexTarget(
        kind="windows",
        executable="codex.exe",
        home=Path("/mnt/c/Users/test/.codex"),
    )

    value = _runtime_path(target, target.home / CATALOG_RELATIVE_PATH)

    assert value == r"C:\Users\test\.codex\model-catalogs\gpt-5.6-sol-1m.json"


def test_status_command_emits_machine_readable_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "status",
            "--target",
            "wsl",
            "--codex-bin",
            "codex",
            "--codex-home",
            str(tmp_path / ".codex"),
            "--json",
        ],
        runner=FakeCodexRunner(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["eligible"] is True
    assert payload["ready"] is False
    assert payload["config_path"] == str(tmp_path / ".codex" / "config.toml")
    assert payload["catalog_path"] == str(tmp_path / ".codex" / CATALOG_RELATIVE_PATH)


def test_run_executes_plain_codex_after_default_is_ready(tmp_path: Path) -> None:
    target = _target(tmp_path)
    runner = FakeCodexRunner()
    configure_target(target, runner=runner)
    executed: list[tuple[str, list[str], dict[str, str]]] = []

    exit_code = main(
        [
            "run",
            "--target",
            "wsl",
            "--codex-bin",
            target.executable,
            "--codex-home",
            str(target.home),
            "--",
            "--no-alt-screen",
        ],
        runner=runner,
        exec_fn=lambda executable, args, env: executed.append((executable, args, env)),
    )

    assert exit_code == 0
    assert len(executed) == 1
    executable, args, env = executed[0]
    assert executable == "codex"
    assert args == ["codex", "--no-alt-screen"]
    assert env["CODEX_HOME"] == str(target.home)


@pytest.mark.skipif(os.name == "nt", reason="WSLENV is specific to WSL interop")
def test_windows_run_marks_codex_home_for_wsl_path_translation(
    tmp_path: Path,
) -> None:
    target = CodexTarget(
        kind="windows",
        executable="/mnt/c/Users/test/AppData/Local/OpenAI/Codex/bin/codex.exe",
        home=tmp_path / ".codex",
    )
    runner = FakeCodexRunner()
    configure_target(target, runner=runner)
    executed: list[dict[str, str]] = []

    exit_code = main(
        [
            "run",
            "--target",
            "windows",
            "--codex-bin",
            target.executable,
            "--codex-home",
            str(target.home),
        ],
        runner=runner,
        exec_fn=lambda executable, args, env: executed.append(env),
    )

    assert exit_code == 0
    assert "CODEX_HOME/p" in executed[0]["WSLENV"].split(":")

"""Unit tests for the optional DeepSeek Harness configuration contract."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, cast

import pytest

from harness.config import (
    DEFAULT_CORDIS_CONFIG,
    DEFAULT_CORDIS_PLUGINS,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_FRAME_BYTES,
    DEFAULT_MAX_STDERR_BYTES,
    DEFAULT_RUNTIME_COMMAND,
    REQUIRED_CORDIS_PLUGIN,
    HarnessConfig,
    HarnessConfigError,
)


def test_defaults_are_disabled_and_conservative() -> None:
    config = HarnessConfig()

    assert config.enabled is False
    assert config.runtime_command is None
    assert config.runtime_args == ()
    assert config.max_concurrency == DEFAULT_MAX_CONCURRENCY == 1
    assert config.request_timeout_seconds == 120.0
    assert config.shutdown_timeout_seconds == 5.0
    assert config.max_frame_bytes == DEFAULT_MAX_FRAME_BYTES
    assert config.max_stderr_bytes == DEFAULT_MAX_STDERR_BYTES


def test_enabled_config_requires_a_runtime_command() -> None:
    config = HarnessConfig(enabled=True)
    assert config.command_argv is None


def test_command_and_arguments_are_normalized_to_immutable_argv() -> None:
    config = HarnessConfig(
        enabled=True,
        runtime_command=["node"],
        runtime_args=("--config", "cordis.yml"),
    )

    assert config.runtime_command == ("node",)
    assert config.runtime_args == ("--config", "cordis.yml")
    assert config.command_argv == ("node", "--config", "cordis.yml")


@pytest.mark.parametrize(
    "command",
    ["node", (), ("",), ("node\x00",), ("node\n",), ("node", "")],
)
def test_invalid_runtime_command_is_rejected(command: object) -> None:
    with pytest.raises(HarnessConfigError):
        HarnessConfig(enabled=True, runtime_command=cast(Any, command))


def test_string_runtime_command_is_rejected_to_avoid_implicit_shell_parsing() -> None:
    with pytest.raises(HarnessConfigError, match="sequence"):
        HarnessConfig(enabled=True, runtime_command="node --version")


@pytest.mark.parametrize("value", [0, -1, math.nan, math.inf, -math.inf, True])
def test_request_timeout_must_be_finite_and_positive(value: object) -> None:
    with pytest.raises(HarnessConfigError, match="request_timeout_seconds"):
        HarnessConfig(request_timeout_seconds=cast(Any, value))


@pytest.mark.parametrize("value", [0, -1, 65, True])
def test_concurrency_is_bounded(value: object) -> None:
    with pytest.raises(HarnessConfigError, match="max_concurrency"):
        HarnessConfig(max_concurrency=cast(Any, value))


def test_frame_and_stderr_limits_are_bounded() -> None:
    with pytest.raises(HarnessConfigError, match="max_frame_bytes"):
        HarnessConfig(max_frame_bytes=0)
    with pytest.raises(HarnessConfigError, match="max_frame_bytes"):
        HarnessConfig(max_frame_bytes=16 * 1024 * 1024 + 1)
    with pytest.raises(HarnessConfigError, match="max_stderr_bytes"):
        HarnessConfig(max_stderr_bytes=0)


def test_workspace_root_is_resolved_and_must_be_a_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = HarnessConfig(workspace_root=workspace)

    assert config.workspace_root == workspace.resolve()
    assert (
        config.resolve_workspace_path("nested/file.txt")
        == (workspace / "nested" / "file.txt").resolve()
    )

    with pytest.raises(HarnessConfigError, match="workspace"):
        config.resolve_workspace_path(tmp_path / "outside.txt")


def test_workspace_root_rejects_missing_or_file_path(tmp_path: Path) -> None:
    with pytest.raises(HarnessConfigError, match="directory"):
        HarnessConfig(workspace_root=tmp_path / "missing")

    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(HarnessConfigError, match="directory"):
        HarnessConfig(workspace_root=file_path)


def test_plugin_allowlist_deduplicates_and_defaults_to_deny() -> None:
    config = HarnessConfig(
        plugin_allowlist=("plugin-a", "plugin-a", " plugin-b "),
    )

    assert config.plugin_allowlist == ("plugin-a", "plugin-b")
    assert config.is_plugin_allowed("plugin-a") is True
    assert config.is_plugin_allowed("plugin-c") is False
    assert HarnessConfig().is_plugin_allowed("plugin-a") is False


@pytest.mark.parametrize("allowlist", [("",), ("plugin\n",), ("plugin\x00",)])
def test_plugin_allowlist_rejects_empty_or_control_names(
    allowlist: tuple[str, ...],
) -> None:
    with pytest.raises(HarnessConfigError, match="plugin"):
        HarnessConfig(plugin_allowlist=allowlist)


def test_from_env_parses_shell_words_and_security_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {
        "DSH_ENABLED": "true",
        "DSH_RUNTIME_COMMAND": "node --no-warnings",
        "DSH_RUNTIME_ARGS": "--config 'cordis.yml'",
        "DSH_CORDIS_CONFIG": str(tmp_path / "cordis.yml"),
        "DSH_PLUGIN_ALLOWLIST": "plugin-a, plugin-b,plugin-a",
        "DSH_WORKSPACE_ROOT": str(workspace),
        "DSH_REQUEST_TIMEOUT": "12.5",
        "DSH_SHUTDOWN_TIMEOUT": "2",
        "DSH_MAX_CONCURRENCY": "2",
        "DSH_MAX_FRAME_BYTES": "2048",
        "DSH_MAX_STDERR_BYTES": "4096",
        "DSH_RUNTIME_VERSION": "0.1.0",
    }

    config = HarnessConfig.from_env(env)

    assert config.enabled is True
    assert config.command_argv == ("node", "--no-warnings", "--config", "cordis.yml")
    assert config.cordis_config == (tmp_path / "cordis.yml").resolve()
    assert config.plugin_allowlist == ("plugin-a", "plugin-b")
    assert config.workspace_root == workspace.resolve()
    assert config.request_timeout_seconds == 12.5
    assert config.shutdown_timeout_seconds == 2.0
    assert config.max_concurrency == 2
    assert config.max_frame_bytes == 2048
    assert config.max_stderr_bytes == 4096
    assert config.runtime_version == "0.1.0"


def test_from_env_empty_optional_values_keep_safe_defaults() -> None:
    config = HarnessConfig.from_env(
        {
            "DSH_ENABLED": "false",
            "DSH_RUNTIME_COMMAND": "",
            "DSH_RUNTIME_ARGS": "",
            "DSH_PLUGIN_ALLOWLIST": "",
        }
    )

    assert config.enabled is False
    assert config.runtime_command is None
    assert config.runtime_args == ()
    assert config.plugin_allowlist == ()


def test_from_env_enabled_uses_the_pinned_sandbox_runtime_command() -> None:
    config = HarnessConfig.from_env({"DSH_ENABLED": "true"})

    assert config.command_argv == DEFAULT_RUNTIME_COMMAND
    assert config.plugin_allowlist == DEFAULT_CORDIS_PLUGINS
    assert config.validate_cordis_config() is not None


def test_bundled_cordis_uses_sandbox_backends() -> None:
    plugins = set(DEFAULT_CORDIS_PLUGINS)

    assert "@deepseek-ai/dsh-sandbox-local" in plugins
    assert "@deepseek-ai/dsh-sandbox-policy" in plugins
    assert "@deepseek-ai/dsh-bash-sandbox" in plugins
    assert "@deepseek-ai/dsh-fs-sandbox" in plugins
    assert "@deepseek-ai/dsh-bash-local" not in plugins
    assert "@deepseek-ai/dsh-fs-local" not in plugins


def test_from_env_reads_dotenv_when_no_mapping_is_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        'DSH_ENABLED="true"\nDSH_RUNTIME_COMMAND="node --no-warnings"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DSH_ENABLED", raising=False)
    monkeypatch.delenv("DSH_RUNTIME_COMMAND", raising=False)

    config = HarnessConfig.from_env()

    assert config.enabled is True
    assert config.command_argv == ("node", "--no-warnings")


def test_bundled_cordis_config_is_allowed_with_a_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = HarnessConfig.from_env(
        {"DSH_ENABLED": "true", "DSH_WORKSPACE_ROOT": str(workspace)}
    )

    assert config.validate_cordis_config() == DEFAULT_CORDIS_CONFIG.resolve()


def test_relative_cordis_config_is_resolved_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = workspace / "cordis.json"
    config_path.write_text(
        json.dumps({"plugins": [REQUIRED_CORDIS_PLUGIN]}), encoding="utf-8"
    )

    config = HarnessConfig(
        cordis_config="cordis.json",
        plugin_allowlist=(REQUIRED_CORDIS_PLUGIN,),
        workspace_root=workspace,
    )

    assert config.cordis_config == config_path.resolve()
    assert config.validate_cordis_config() == config_path.resolve()


@pytest.mark.parametrize("value", ["maybe", "2", "enabled"])
def test_from_env_rejects_invalid_boolean(value: str) -> None:
    with pytest.raises(HarnessConfigError, match="DSH_ENABLED"):
        HarnessConfig.from_env({"DSH_ENABLED": value})


def test_cordis_config_must_not_be_a_directory(tmp_path: Path) -> None:
    with pytest.raises(HarnessConfigError, match="cordis_config"):
        HarnessConfig(cordis_config=tmp_path)


def test_custom_cordis_config_requires_server_plugin_and_allowlist(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "cordis.json"
    config_path.write_text(
        json.dumps(
            {
                "plugins": [
                    REQUIRED_CORDIS_PLUGIN,
                    "@example/extra-plugin",
                ]
            }
        ),
        encoding="utf-8",
    )
    config = HarnessConfig(
        cordis_config=config_path,
        plugin_allowlist=(REQUIRED_CORDIS_PLUGIN, "@example/extra-plugin"),
    )

    assert config.validate_cordis_config() == config_path.resolve()

    denied = HarnessConfig(
        cordis_config=config_path,
        plugin_allowlist=(REQUIRED_CORDIS_PLUGIN,),
    )
    with pytest.raises(HarnessConfigError, match="configured plugin"):
        denied.validate_cordis_config()


def test_custom_cordis_config_cannot_remove_required_server_plugin(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "cordis.json"
    config_path.write_text(
        json.dumps({"plugins": ["@example/extra-plugin"]}), encoding="utf-8"
    )
    config = HarnessConfig(
        cordis_config=config_path,
        plugin_allowlist=(REQUIRED_CORDIS_PLUGIN, "@example/extra-plugin"),
    )

    with pytest.raises(HarnessConfigError, match="must retain"):
        config.validate_cordis_config()

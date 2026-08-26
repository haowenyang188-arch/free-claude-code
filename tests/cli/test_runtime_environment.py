from __future__ import annotations

import json

import pytest


def test_build_environment_keeps_runtime_context_and_drops_inherited_leakage():
    from cli.runtime_environment import build_cli_environment

    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/tester",
        "TERM": "xterm-256color",
        "PYTHONIOENCODING": "utf-8",
        "LANG": "C.UTF-8",
        "LC_TIME": "en_US.UTF-8",
        "TMPDIR": "/tmp/tester",
        "SHELL": "/bin/zsh",
        "PWD": "/secret/workspace",
        "HTTP_PROXY": "http://user:password@example.test:8080",
        "MCP_SERVER_URL": "https://mcp.example.test",
        "DEEPSEEK_API_KEY": "deepseek-secret",
        "MINIMAX_API_KEY": "minimax-secret",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "OPENAI_API_KEY": "openai-secret",
    }

    result = build_cli_environment("claude", parent_env=parent)

    assert result == {
        "HOME": "/home/tester",
        "LANG": "C.UTF-8",
        "LC_TIME": "en_US.UTF-8",
        "PATH": "/usr/bin",
        "PYTHONIOENCODING": "utf-8",
        "TERM": "dumb",
        "TMPDIR": "/tmp/tester",
    }
    assert parent["ANTHROPIC_API_KEY"] == "anthropic-secret"


def test_defaults_are_stable_when_parent_omits_terminal_settings():
    from cli.runtime_environment import build_cli_environment

    result = build_cli_environment(
        "codex",
        parent_env={"PATH": "/usr/bin", "HOME": "/home/tester"},
    )

    assert result["TERM"] == "dumb"
    assert result["PYTHONIOENCODING"] == "utf-8"


def test_credentials_are_backend_specific_and_opt_in():
    from cli.runtime_environment import build_cli_environment

    parent = {
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "ANTHROPIC_AUTH_TOKEN": "anthropic-token",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
        "OPENAI_API_KEY": "openai-secret",
        "CODEX_API_KEY": "codex-secret",
        "PATH": "/usr/bin",
    }

    assert "ANTHROPIC_API_KEY" not in build_cli_environment("claude", parent_env=parent)
    assert "OPENAI_API_KEY" not in build_cli_environment("claude", parent_env=parent)

    claude = build_cli_environment("claude", parent_env=parent, allow_credentials=True)
    assert claude["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert claude["ANTHROPIC_AUTH_TOKEN"] == "anthropic-token"
    assert claude["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-secret"
    assert "OPENAI_API_KEY" not in claude

    codex = build_cli_environment("codex", parent_env=parent, allow_credentials=True)
    assert codex["OPENAI_API_KEY"] == "openai-secret"
    assert codex["CODEX_API_KEY"] == "codex-secret"
    assert "ANTHROPIC_API_KEY" not in codex


def test_routing_values_require_explicit_extra_environment():
    from cli.runtime_environment import build_cli_environment

    parent = {
        "PATH": "/usr/bin",
        "ANTHROPIC_BASE_URL": "https://stale.example.test",
        "ANTHROPIC_API_URL": "https://stale.example.test/v1",
    }

    inherited = build_cli_environment("claude", parent_env=parent)
    assert "ANTHROPIC_BASE_URL" not in inherited
    assert "ANTHROPIC_API_URL" not in inherited

    explicit = build_cli_environment(
        "claude",
        parent_env=parent,
        extra_env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8082",
            "ANTHROPIC_API_URL": "http://127.0.0.1:8082/v1",
        },
    )
    assert explicit["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8082"
    assert explicit["ANTHROPIC_API_URL"] == "http://127.0.0.1:8082/v1"


def test_extra_environment_can_override_safe_keys_and_rejects_unsafe_keys():
    from cli.runtime_environment import build_cli_environment

    result = build_cli_environment(
        "codex",
        parent_env={"PATH": "/usr/bin", "TERM": "dumb"},
        extra_env={"TERM": "xterm", "LC_ALL": "C.UTF-8", "TMP": "/tmp/cli"},
    )

    assert result["TERM"] == "xterm"
    assert result["LC_ALL"] == "C.UTF-8"
    assert result["TMP"] == "/tmp/cli"

    with pytest.raises(ValueError, match="allowlisted"):
        build_cli_environment(
            "codex", parent_env={}, extra_env={"LD_PRELOAD": "/tmp/evil.so"}
        )

    with pytest.raises(ValueError, match="allowlisted"):
        build_cli_environment("codex", parent_env={}, extra_env={"MCP_SERVER": "x"})

    with pytest.raises(ValueError, match="allow_credentials"):
        build_cli_environment(
            "claude", parent_env={}, extra_env={"ANTHROPIC_API_KEY": "secret"}
        )


def test_invalid_backends_mappings_and_values_fail_closed():
    from cli.runtime_environment import build_cli_environment

    with pytest.raises(ValueError, match="unsupported runtime backend"):
        build_cli_environment("shell", parent_env={})

    with pytest.raises(ValueError, match="environment key"):
        build_cli_environment("codex", parent_env={"BAD=KEY": "value"})

    with pytest.raises(ValueError, match="string"):
        build_cli_environment("codex", parent_env={"PATH": 123})

    with pytest.raises(ValueError, match="NUL"):
        build_cli_environment("codex", parent_env={"PATH": "/bin\x00evil"})

    with pytest.raises(ValueError, match="allow_credentials"):
        build_cli_environment("codex", parent_env={}, allow_credentials="yes")


def test_output_and_diagnostics_are_deterministic_and_never_contain_secret_values():
    from cli.runtime_environment import (
        build_cli_environment,
        describe_cli_environment,
    )

    secret = "sk-test-secret-value"
    first = build_cli_environment(
        "codex",
        parent_env={"HOME": "/home/tester", "PATH": "/bin", "LC_NUMERIC": "C"},
        extra_env={"OPENAI_BASE_URL": "https://api.example.test"},
    )
    second = build_cli_environment(
        "codex",
        parent_env={"LC_NUMERIC": "C", "PATH": "/bin", "HOME": "/home/tester"},
        extra_env={"OPENAI_BASE_URL": "https://api.example.test"},
    )
    assert list(first) == list(second)
    assert first == second

    diagnostics = describe_cli_environment(
        {**first, "OPENAI_API_KEY": secret}, backend="codex"
    )
    serialized = json.dumps(diagnostics, sort_keys=True)
    assert secret not in serialized
    assert "OPENAI_API_KEY" in diagnostics["credential_keys"]
    assert diagnostics["backend"] == "codex"
    assert diagnostics["key_count"] == len(first) + 1


def test_approval_policy_environment_is_allowlisted_without_credentials():
    from cli.runtime_environment import build_cli_environment

    environment = build_cli_environment(
        "codex",
        extra_env={
            "FCC_APPROVAL_ENABLED": "true",
            "FCC_APPROVAL_SCOPE": "once",
            "FCC_APPROVAL_COMMANDS_JSON": '["git status"]',
            "FCC_APPROVAL_WORKSPACES_JSON": '["/tmp/project"]',
            "FCC_APPROVAL_ALLOW_PERMANENT": "false",
        },
    )

    assert environment["FCC_APPROVAL_ENABLED"] == "true"
    assert "OPENAI_API_KEY" not in environment


def test_explicit_mcp_config_requires_an_existing_absolute_file(tmp_path):
    from cli.runtime_environment import resolve_explicit_mcp_config

    config = tmp_path / "windows-mcp.json"
    config.write_text('{"mcpServers":{"windows-mcp":{}}}', encoding="utf-8")

    assert resolve_explicit_mcp_config(str(config)) == str(config)
    assert resolve_explicit_mcp_config(None) is None
    with pytest.raises(ValueError, match="absolute path"):
        resolve_explicit_mcp_config("windows-mcp.json")
    with pytest.raises(ValueError, match="existing file"):
        resolve_explicit_mcp_config(str(tmp_path / "missing.json"))

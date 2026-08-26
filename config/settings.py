"""Centralized configuration using Pydantic Settings."""

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_files() -> tuple[Path, ...]:
    """Return env file paths in priority order (later overrides earlier)."""
    files: list[Path] = [
        Path.home() / ".config" / "free-claude-code" / ".env",
        Path(".env"),
    ]
    if explicit := os.environ.get("FCC_ENV_FILE"):
        files.append(Path(explicit))
    return tuple(files)


def _configured_env_files(model_config: Mapping[str, Any]) -> tuple[Path, ...]:
    """Return the currently configured env files for Settings."""
    configured = model_config.get("env_file")
    if configured is None:
        return ()
    if isinstance(configured, (str, Path)):
        return (Path(configured),)
    return tuple(Path(item) for item in configured)


def _env_file_contains_key(path: Path, key: str) -> bool:
    """Check whether a dotenv-style file defines the given key."""
    return _env_file_value(path, key) is not None


def _env_file_value(path: Path, key: str) -> str | None:
    """Return a dotenv value when the file explicitly defines the key."""
    if not path.is_file():
        return None

    try:
        values = dotenv_values(path)
    except OSError:
        return None

    if key not in values:
        return None
    value = values[key]
    return "" if value is None else value


def _env_file_override(model_config: Mapping[str, Any], key: str) -> str | None:
    """Return the last configured dotenv value that explicitly defines a key."""
    configured_value: str | None = None
    for env_file in _configured_env_files(model_config):
        value = _env_file_value(env_file, key)
        if value is not None:
            configured_value = value
    return configured_value


def _removed_env_var_message(model_config: Mapping[str, Any]) -> str | None:
    """Return a migration error for removed env vars, if present."""
    removed_key = "NIM_ENABLE_THINKING"
    replacement = "ENABLE_THINKING"

    if removed_key in os.environ:
        return (
            f"{removed_key} has been removed in this release. "
            f"Rename it to {replacement}."
        )

    for env_file in _configured_env_files(model_config):
        if _env_file_contains_key(env_file, removed_key):
            return (
                f"{removed_key} has been removed in this release. "
                f"Rename it to {replacement}. Found in {env_file}."
            )

    return None


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # ==================== DeepSeek Config ====================
    deepseek_api_key: str = Field(default="", validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="", validation_alias="DEEPSEEK_BASE_URL")

    # ==================== MiniMax Config ====================
    minimax_api_key: str = Field(default="", validation_alias="MINIMAX_API_KEY")
    minimax_base_url: str = Field(
        default="https://api.minimaxi.com/anthropic",
        validation_alias="MINIMAX_BASE_URL",
    )

    # ==================== Messaging Platform Selection ====================
    # Valid: "telegram" | "discord"
    messaging_platform: str = Field(
        default="discord", validation_alias="MESSAGING_PLATFORM"
    )

    # ==================== Model ====================
    # All Claude model requests are mapped to this single model (fallback)
    # Format: provider_type/model/name
    # Valid providers: "minimax" | "deepseek" | "dsh"
    model: str = "minimax/MiniMax-M3"

    # Per-model overrides (optional, falls back to MODEL)
    # Each can use a different provider
    model_opus: str | None = Field(default=None, validation_alias="MODEL_OPUS")
    model_sonnet: str | None = Field(default=None, validation_alias="MODEL_SONNET")
    model_haiku: str | None = Field(default=None, validation_alias="MODEL_HAIKU")

    # ==================== Per-Provider Proxy ====================
    minimax_proxy: str = Field(default="", validation_alias="MINIMAX_PROXY")

    # ==================== Provider Rate Limiting ====================
    provider_rate_limit: int = Field(default=40, validation_alias="PROVIDER_RATE_LIMIT")
    provider_rate_window: int = Field(
        default=60, validation_alias="PROVIDER_RATE_WINDOW"
    )
    provider_max_concurrency: int = Field(
        default=5, validation_alias="PROVIDER_MAX_CONCURRENCY"
    )
    enable_thinking: bool = Field(default=True, validation_alias="ENABLE_THINKING")

    # ==================== HTTP Client Timeouts ====================
    http_read_timeout: float = Field(
        default=120.0, validation_alias="HTTP_READ_TIMEOUT"
    )
    http_write_timeout: float = Field(
        default=10.0, validation_alias="HTTP_WRITE_TIMEOUT"
    )
    http_connect_timeout: float = Field(
        default=2.0, validation_alias="HTTP_CONNECT_TIMEOUT"
    )

    # ==================== Fast Prefix Detection ====================
    fast_prefix_detection: bool = True

    # ==================== Optimizations ====================
    enable_network_probe_mock: bool = True
    enable_title_generation_skip: bool = True
    enable_suggestion_mode_skip: bool = True
    enable_filepath_extraction_mock: bool = True

    # ==================== Voice Note Transcription ====================
    # Local Whisper transcription (requires the voice_local extra).
    voice_note_enabled: bool = Field(
        default=True, validation_alias="VOICE_NOTE_ENABLED"
    )
    # Device: "cpu" | "cuda"
    # - "cpu"/"cuda": local Whisper (requires voice_local extra: uv sync --extra voice_local)
    whisper_device: str = Field(default="cpu", validation_alias="WHISPER_DEVICE")
    # Whisper model ID or short name (for local Whisper)
    whisper_model: str = Field(default="base", validation_alias="WHISPER_MODEL")
    # Hugging Face token for faster model downloads (optional, for local Whisper)
    hf_token: str = Field(default="", validation_alias="HF_TOKEN")

    # ==================== Bot Wrapper Config ====================
    telegram_bot_token: str | None = None
    allowed_telegram_user_id: str | None = None
    discord_bot_token: str | None = Field(
        default=None, validation_alias="DISCORD_BOT_TOKEN"
    )
    allowed_discord_channels: str | None = Field(
        default=None, validation_alias="ALLOWED_DISCORD_CHANNELS"
    )
    # Local agent gateway.  The safe default keeps Claude in planning mode and
    # selects Claude for backwards compatibility; Codex is opt-in per process.
    agent_backend: str = Field(default="claude", validation_alias="AGENT_BACKEND")
    agent_permission_mode: str = Field(
        default="plan", validation_alias="AGENT_PERMISSION_MODE"
    )
    claude_auth_mode: str = Field(default="proxy", validation_alias="CLAUDE_AUTH_MODE")
    claude_bin: str = Field(default="claude", validation_alias="CLAUDE_BIN")
    codex_bin: str = Field(default="codex", validation_alias="CODEX_BIN")
    codex_model: str | None = Field(default=None, validation_alias="CODEX_MODEL")
    codex_sandbox: str = Field(default="read-only", validation_alias="CODEX_SANDBOX")
    codex_approval_required: bool = Field(
        default=True, validation_alias="CODEX_APPROVAL_REQUIRED"
    )
    # Protocol-level auto approval is opt-in and limited to low-risk rules.
    cli_auto_approval_enabled: bool = Field(
        default=False, validation_alias="CLI_AUTO_APPROVAL_ENABLED"
    )
    cli_auto_approval_scope: str = Field(
        default="once", validation_alias="CLI_AUTO_APPROVAL_SCOPE"
    )
    cli_auto_approval_commands: str = Field(
        default="", validation_alias="CLI_AUTO_APPROVAL_COMMANDS"
    )
    cli_auto_approval_workspaces: str = Field(
        default="", validation_alias="CLI_AUTO_APPROVAL_WORKSPACES"
    )
    cli_auto_approval_allow_permanent: bool = Field(
        default=False, validation_alias="CLI_AUTO_APPROVAL_ALLOW_PERMANENT"
    )
    cli_mcp_config: str = Field(default="", validation_alias="CLI_MCP_CONFIG")
    cli_runtime_preflight: bool = Field(
        default=True, validation_alias="CLI_RUNTIME_PREFLIGHT"
    )
    cli_isolation_mode: str = Field(
        default="safe", validation_alias="CLI_ISOLATION_MODE"
    )
    claude_workspace: str = "./agent_workspace"
    allowed_dir: str = ""

    # ==================== Server ====================
    host: str = "127.0.0.1"
    port: int = 8082
    log_file: str = "server.log"
    # Optional on loopback; required for non-loopback binds and DSH execution.
    anthropic_auth_token: str = Field(
        default="", validation_alias="ANTHROPIC_AUTH_TOKEN"
    )

    @model_validator(mode="before")
    @classmethod
    def reject_removed_env_vars(cls, data: Any) -> Any:
        """Fail fast when removed environment variables are still configured."""
        if message := _removed_env_var_message(cls.model_config):
            raise ValueError(message)
        return data

    # Handle empty strings for optional string fields
    @field_validator(
        "telegram_bot_token",
        "allowed_telegram_user_id",
        "discord_bot_token",
        "allowed_discord_channels",
        "model_opus",
        "model_sonnet",
        "model_haiku",
        "codex_model",
        mode="before",
    )
    @classmethod
    def parse_optional_str(cls, v: Any) -> Any:
        if v == "":
            return None
        return v

    @field_validator("whisper_device")
    @classmethod
    def validate_whisper_device(cls, v: str) -> str:
        if v not in ("cpu", "cuda"):
            raise ValueError(f"whisper_device must be 'cpu' or 'cuda', got {v!r}")
        return v

    @field_validator("agent_backend")
    @classmethod
    def validate_agent_backend(cls, v: str) -> str:
        if v not in ("claude", "codex"):
            raise ValueError("AGENT_BACKEND must be one of: 'claude', 'codex'")
        return v

    @field_validator("agent_permission_mode")
    @classmethod
    def validate_agent_permission_mode(cls, v: str) -> str:
        allowed = ("plan", "acceptEdits", "auto", "bypassPermissions")
        if v not in allowed:
            raise ValueError(
                "AGENT_PERMISSION_MODE must be one of: "
                + ", ".join(repr(item) for item in allowed)
            )
        return v

    @field_validator("claude_auth_mode")
    @classmethod
    def validate_claude_auth_mode(cls, v: str) -> str:
        if v not in ("proxy", "local"):
            raise ValueError("CLAUDE_AUTH_MODE must be 'proxy' or 'local'")
        return v

    @field_validator("cli_isolation_mode")
    @classmethod
    def validate_cli_isolation_mode(cls, v: str) -> str:
        if v not in ("safe", "inherit"):
            raise ValueError("CLI_ISOLATION_MODE must be 'safe' or 'inherit'")
        return v

    @field_validator("codex_sandbox")
    @classmethod
    def validate_codex_sandbox(cls, v: str) -> str:
        allowed = ("read-only", "workspace-write", "danger-full-access")
        if v not in allowed:
            raise ValueError(
                "CODEX_SANDBOX must be one of: "
                + ", ".join(repr(item) for item in allowed)
            )
        return v

    @field_validator("cli_auto_approval_scope")
    @classmethod
    def validate_cli_auto_approval_scope(cls, v: str) -> str:
        allowed = ("once", "session", "permanent")
        if v not in allowed:
            raise ValueError(
                "CLI_AUTO_APPROVAL_SCOPE must be one of: "
                + ", ".join(repr(item) for item in allowed)
            )
        return v

    @field_validator("model", "model_opus", "model_sonnet", "model_haiku")
    @classmethod
    def validate_model_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        valid_providers = ("minimax", "deepseek", "dsh")
        if "/" not in v:
            raise ValueError(
                f"Model must be prefixed with provider type. "
                f"Valid providers: {', '.join(valid_providers)}. "
                f"Format: provider_type/model/name"
            )
        provider = v.split("/", 1)[0]
        if provider not in valid_providers:
            raise ValueError(
                "Invalid provider: "
                f"'{provider}'. Supported: 'minimax', 'deepseek', 'dsh'"
            )
        if provider == "dsh":
            parts = v.split("/", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                raise ValueError(
                    "DeepSeek Harness models must use dsh/<provider>/<model>"
                )
        return v

    @model_validator(mode="after")
    def prefer_dotenv_secrets(self) -> Settings:
        """Let explicit .env secrets override stale shell/client values."""
        secret_fields = {
            "ANTHROPIC_AUTH_TOKEN": "anthropic_auth_token",
            "DEEPSEEK_API_KEY": "deepseek_api_key",
            "MINIMAX_API_KEY": "minimax_api_key",
        }
        for env_key, field_name in secret_fields.items():
            dotenv_value = _env_file_override(self.model_config, env_key)
            if dotenv_value is not None:
                setattr(self, field_name, dotenv_value)
        return self

    def _has_dotenv_anthropic_auth_token(self) -> bool:
        dotenv_value = _env_file_override(self.model_config, "ANTHROPIC_AUTH_TOKEN")
        return dotenv_value is not None

    def uses_process_anthropic_auth_token(self) -> bool:
        """Return whether proxy auth came from process env, not dotenv config."""
        if self._has_dotenv_anthropic_auth_token():
            return False
        return bool(os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    @property
    def provider_type(self) -> str:
        """Extract provider type from the default model string."""
        return self.model.split("/", 1)[0]

    @property
    def model_name(self) -> str:
        """Extract the actual model name from the default model string."""
        return self.model.split("/", 1)[1]

    def resolve_model(self, claude_model_name: str) -> str:
        """Resolve a Claude model name to the configured provider/model string.

        Classifies the incoming Claude model (opus/sonnet/haiku) and
        returns the model-specific override if configured, otherwise the fallback MODEL.
        """
        name_lower = claude_model_name.lower()
        if "opus" in name_lower and self.model_opus is not None:
            return self.model_opus
        if "haiku" in name_lower and self.model_haiku is not None:
            return self.model_haiku
        if "sonnet" in name_lower and self.model_sonnet is not None:
            return self.model_sonnet
        return self.model

    @staticmethod
    def parse_provider_type(model_string: str) -> str:
        """Extract provider type from any 'provider/model' string."""
        return model_string.split("/", 1)[0]

    @staticmethod
    def parse_model_name(model_string: str) -> str:
        """Extract model name from any 'provider/model' string."""
        return model_string.split("/", 1)[1]

    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()

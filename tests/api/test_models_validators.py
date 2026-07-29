from unittest.mock import patch

import pytest

from api.models.anthropic import Message, MessagesRequest, TokenCountRequest
from config.settings import Settings


@pytest.fixture
def mock_settings():
    settings = Settings()
    settings.model = "nvidia_nim/target-model-from-settings"
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    return settings


def test_messages_request_map_model_claude_to_default(mock_settings):
    with patch("api.models.anthropic.get_settings", return_value=mock_settings):
        request = MessagesRequest(
            model="claude-3-opus",
            max_tokens=100,
            messages=[Message(role="user", content="hello")],
        )

        # request.model is now the full provider/model string (new semantics)
        assert request.model == "nvidia_nim/target-model-from-settings"
        assert request.original_model == "claude-3-opus"
        assert request.upstream_model == "target-model-from-settings"


def test_messages_request_map_model_with_provider_prefix(mock_settings):
    with patch("api.models.anthropic.get_settings", return_value=mock_settings):
        request = MessagesRequest(
            model="anthropic/claude-3-haiku",
            max_tokens=100,
            messages=[Message(role="user", content="hello")],
        )

        # request.model is now the full provider/model string
        assert request.model == "nvidia_nim/target-model-from-settings"


def test_token_count_request_model_validation(mock_settings):
    with patch("api.models.anthropic.get_settings", return_value=mock_settings):
        request = TokenCountRequest(
            model="claude-3-sonnet", messages=[Message(role="user", content="hello")]
        )

        assert request.model == "target-model-from-settings"


def test_messages_request_model_mapping_logs(mock_settings):
    with (
        patch("api.models.anthropic.get_settings", return_value=mock_settings),
        patch("api.models.anthropic.logger.debug") as mock_log,
    ):
        MessagesRequest(
            model="claude-2.1",
            max_tokens=100,
            messages=[Message(role="user", content="hello")],
        )

        mock_log.assert_called()
        args = mock_log.call_args[0][0]
        assert "MODEL MAPPING" in args
        assert "claude-2.1" in args
        # Log now includes both full string and upstream model
        assert "nvidia_nim/target-model-from-settings" in args


def test_messages_request_resolved_provider_model_default(mock_settings):
    """resolved_provider_model is set to the full model string."""
    with patch("api.models.anthropic.get_settings", return_value=mock_settings):
        request = MessagesRequest(
            model="claude-3-opus",
            max_tokens=100,
            messages=[Message(role="user", content="hello")],
        )
        assert (
            request.resolved_provider_model == "nvidia_nim/target-model-from-settings"
        )


def test_messages_request_model_aware_opus_override():
    """Opus model routes to MODEL_OPUS when set."""
    settings = Settings()
    settings.model = "nvidia_nim/fallback-model"
    settings.model_opus = "open_router/deepseek/deepseek-r1"

    with patch("api.models.anthropic.get_settings", return_value=settings):
        request = MessagesRequest(
            model="claude-opus-4-20250514",
            max_tokens=100,
            messages=[Message(role="user", content="hello")],
        )
        # request.model is now the full provider/model string
        assert request.model == "open_router/deepseek/deepseek-r1"
        assert request.upstream_model == "deepseek/deepseek-r1"
        assert request.resolved_provider_model == "open_router/deepseek/deepseek-r1"
        assert request.original_model == "claude-opus-4-20250514"


def test_messages_request_model_aware_haiku_override():
    """Haiku model routes to MODEL_HAIKU when set."""
    settings = Settings()
    settings.model = "nvidia_nim/fallback-model"
    settings.model_haiku = "lmstudio/qwen2.5-7b"

    with patch("api.models.anthropic.get_settings", return_value=settings):
        request = MessagesRequest(
            model="claude-3-haiku-20240307",
            max_tokens=100,
            messages=[Message(role="user", content="hello")],
        )
        # request.model is now the full provider/model string
        assert request.model == "lmstudio/qwen2.5-7b"
        assert request.upstream_model == "qwen2.5-7b"
        assert request.resolved_provider_model == "lmstudio/qwen2.5-7b"


def test_messages_request_model_aware_sonnet_override():
    """Sonnet model routes to MODEL_SONNET when set."""
    settings = Settings()
    settings.model = "nvidia_nim/fallback-model"
    settings.model_sonnet = "nvidia_nim/meta/llama-3.3-70b-instruct"

    with patch("api.models.anthropic.get_settings", return_value=settings):
        request = MessagesRequest(
            model="claude-sonnet-4-20250514",
            max_tokens=100,
            messages=[Message(role="user", content="hello")],
        )
        # request.model is now the full provider/model string
        assert request.model == "nvidia_nim/meta/llama-3.3-70b-instruct"
        assert request.upstream_model == "meta/llama-3.3-70b-instruct"
        assert (
            request.resolved_provider_model == "nvidia_nim/meta/llama-3.3-70b-instruct"
        )


def test_messages_request_model_fallback_when_not_set():
    """When model override is None, falls back to MODEL."""
    settings = Settings()
    settings.model = "nvidia_nim/fallback-model"
    settings.model_opus = None
    settings.model_sonnet = None
    settings.model_haiku = None
    # model_opus is None

    with patch("api.models.anthropic.get_settings", return_value=settings):
        request = MessagesRequest(
            model="claude-opus-4-20250514",
            max_tokens=100,
            messages=[Message(role="user", content="hello")],
        )
        # request.model is now the full provider/model string
        assert request.model == "nvidia_nim/fallback-model"
        assert request.upstream_model == "fallback-model"
        assert request.resolved_provider_model == "nvidia_nim/fallback-model"


def test_token_count_request_model_aware():
    """TokenCountRequest also uses model-aware resolution."""
    settings = Settings()
    settings.model = "nvidia_nim/fallback-model"
    settings.model_haiku = "lmstudio/qwen2.5-7b"

    with patch("api.models.anthropic.get_settings", return_value=settings):
        request = TokenCountRequest(
            model="claude-3-haiku-20240307",
            messages=[Message(role="user", content="hello")],
        )
        assert request.model == "qwen2.5-7b"


def test_settings_dotenv_secrets_override_stale_process_env(tmp_path, monkeypatch):
    """Explicit dotenv API keys win over stale shell values."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MINIMAX_API_KEY=dotenv-minimax-key",
                "XIAOMI_API_KEY=dotenv-xiaomi-key",
                "OPENROUTER_API_KEY=dotenv-openrouter-key",
                "DEEPSEEK_API_KEY=dotenv-deepseek-key",
                "NVIDIA_NIM_API_KEY=dotenv-nim-key",
                "ANTHROPIC_AUTH_TOKEN=dotenv-auth-token",
                "MODEL=nvidia_nim/test-model",
                "VOICE_NOTE_ENABLED=false",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "你的MiniMax_API_KEY")
    monkeypatch.setenv("XIAOMI_API_KEY", "stale-xiaomi-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "stale-openrouter-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-deepseek-key")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "stale-nim-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale-auth-token")

    with patch.object(Settings, "model_config", {**Settings.model_config, "env_file": (env_file,)}):
        settings = Settings()
        uses_process_token = settings.uses_process_anthropic_auth_token()

    assert settings.minimax_api_key == "dotenv-minimax-key"
    assert settings.xiaomi_api_key == "dotenv-xiaomi-key"
    assert settings.open_router_api_key == "dotenv-openrouter-key"
    assert settings.deepseek_api_key == "dotenv-deepseek-key"
    assert settings.nvidia_nim_api_key == "dotenv-nim-key"
    assert settings.anthropic_auth_token == "dotenv-auth-token"
    assert uses_process_token is False

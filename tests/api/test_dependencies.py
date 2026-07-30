from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.dependencies import (
    cleanup_provider,
    get_provider,
    get_provider_for_type,
    get_settings,
)
from providers.deepseek import DeepSeekProvider
from providers.minimax import MiniMaxProvider


def _make_mock_settings(**overrides):
    """Create settings with all fields required by provider construction."""
    settings = MagicMock()
    settings.provider_type = "minimax"
    settings.minimax_api_key = "test_minimax_key"
    settings.minimax_base_url = "https://api.minimaxi.com/anthropic"
    settings.minimax_proxy = ""
    settings.deepseek_api_key = "test_deepseek_key"
    settings.provider_rate_limit = 40
    settings.provider_rate_window = 60
    settings.provider_max_concurrency = 5
    settings.http_read_timeout = 300.0
    settings.http_write_timeout = 10.0
    settings.http_connect_timeout = 2.0
    settings.enable_thinking = True
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


@pytest.fixture(autouse=True)
def reset_provider_registry():
    import api.dependencies

    saved = api.dependencies._providers
    api.dependencies._providers = {}
    yield
    api.dependencies._providers = saved


def test_get_settings_delegates_to_cached_settings():
    with patch("api.dependencies._get_settings") as mock_get:
        assert get_settings() is mock_get.return_value
        mock_get.assert_called_once()


def test_get_provider_returns_cached_default_provider():
    with patch("api.dependencies.get_settings") as mock_settings:
        mock_settings.return_value = _make_mock_settings()

        first = get_provider()
        second = get_provider()

    assert isinstance(first, MiniMaxProvider)
    assert first is second


def test_get_provider_for_type_constructs_supported_providers():
    with patch("api.dependencies.get_settings") as mock_settings:
        mock_settings.return_value = _make_mock_settings()
        minimax = get_provider_for_type("minimax")
        deepseek = get_provider_for_type("deepseek")

    assert isinstance(minimax, MiniMaxProvider)
    assert isinstance(deepseek, DeepSeekProvider)
    assert minimax is not deepseek


def test_deepseek_uses_fixed_base_url_and_thinking_setting():
    with patch("api.dependencies.get_settings") as mock_settings:
        mock_settings.return_value = _make_mock_settings(enable_thinking=False)
        provider = get_provider_for_type("deepseek")

    assert isinstance(provider, DeepSeekProvider)
    assert provider._base_url == "https://api.deepseek.com"
    assert provider._config.enable_thinking is False


def test_minimax_receives_proxy_and_timeout_settings():
    with (
        patch("api.dependencies.get_settings") as mock_settings,
        patch("providers.minimax.client.httpx.AsyncClient") as mock_client,
    ):
        mock_settings.return_value = _make_mock_settings(
            minimax_proxy="http://proxy.example:8080",
            http_read_timeout=600.0,
            http_write_timeout=20.0,
            http_connect_timeout=5.0,
        )
        provider = get_provider_for_type("minimax")

    assert isinstance(provider, MiniMaxProvider)
    kwargs = mock_client.call_args.kwargs
    assert kwargs["proxy"] == "http://proxy.example:8080"
    assert kwargs["timeout"].read == 600.0
    assert kwargs["timeout"].write == 20.0
    assert kwargs["timeout"].connect == 5.0


@pytest.mark.parametrize(
    ("provider_type", "setting_name", "expected_message"),
    [
        ("minimax", "minimax_api_key", "MINIMAX_API_KEY"),
        ("deepseek", "deepseek_api_key", "DEEPSEEK_API_KEY"),
    ],
)
def test_missing_provider_key_returns_503(
    provider_type, setting_name, expected_message
):
    with patch("api.dependencies.get_settings") as mock_settings:
        mock_settings.return_value = _make_mock_settings(**{setting_name: "  "})
        with pytest.raises(HTTPException) as exc_info:
            get_provider_for_type(provider_type)

    assert exc_info.value.status_code == 503
    assert expected_message in exc_info.value.detail


def test_unknown_provider_is_rejected():
    with patch("api.dependencies.get_settings") as mock_settings:
        mock_settings.return_value = _make_mock_settings()
        with pytest.raises(ValueError, match="Supported: 'minimax', 'deepseek'"):
            get_provider_for_type("removed-provider")


@pytest.mark.asyncio
async def test_cleanup_provider_closes_all_cached_clients():
    first = MagicMock()
    second = MagicMock()
    first.cleanup = AsyncMock()
    second.cleanup = AsyncMock()

    import api.dependencies

    api.dependencies._providers = {"minimax": first, "deepseek": second}
    await cleanup_provider()

    first.cleanup.assert_awaited_once()
    second.cleanup.assert_awaited_once()
    assert api.dependencies._providers == {}

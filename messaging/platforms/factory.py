"""Messaging platform factory.

Creates the appropriate messaging platform adapter based on configuration.
To add a new platform (e.g. Discord, Slack):
1. Create a new class implementing MessagingPlatform in messaging/platforms/
2. Add a case to create_messaging_platform() below
"""

from loguru import logger

from .base import MessagingPlatform


def create_messaging_platform(
    platform_type: str,
    **kwargs,
) -> MessagingPlatform | None:
    """Create a messaging platform instance based on type.

    Args:
        platform_type: Platform identifier ("telegram", "discord", etc.)
        **kwargs: Platform-specific configuration passed to the constructor.

    Returns:
        Configured MessagingPlatform instance, or None if not configured.
    """
    if platform_type == "telegram":
        bot_token = kwargs.get("bot_token")
        if not bot_token:
            logger.info("No Telegram bot token configured, skipping platform setup")
            return None

        from .telegram import TelegramPlatform

        return TelegramPlatform(
            bot_token=bot_token,
            allowed_user_id=kwargs.get("allowed_user_id"),
            agent_backend=kwargs.get("agent_backend", "claude"),
        )

    if platform_type == "discord":
        bot_token = kwargs.get("discord_bot_token")
        if not bot_token:
            logger.info("No Discord bot token configured, skipping platform setup")
            return None

        from .discord import DiscordPlatform

        return DiscordPlatform(
            bot_token=bot_token,
            allowed_channel_ids=kwargs.get("allowed_discord_channels"),
        )

    if platform_type == "feishu":
        app_id = kwargs.get("feishu_app_id")
        app_secret = kwargs.get("feishu_app_secret")
        if not app_id or not app_secret:
            logger.info("No Feishu app credentials configured, skipping platform setup")
            return None

        from .feishu import FeishuPlatform

        return FeishuPlatform(
            app_id=app_id,
            app_secret=app_secret,
            allowed_open_ids=kwargs.get("allowed_feishu_open_ids"),
            allowed_chat_ids=kwargs.get("allowed_feishu_chat_ids"),
            require_mention=kwargs.get("feishu_require_mention", True),
        )

    logger.warning(
        f"Unknown messaging platform: '{platform_type}'. Supported: 'telegram', 'discord', 'feishu'"
    )
    return None

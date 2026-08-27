"""FastAPI application factory and configuration."""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from config.logging_config import configure_logging
from config.settings import get_settings
from providers.exceptions import ProviderError

from .dependencies import cleanup_provider
from .routes import router

# Opt-in to future behavior for python-telegram-bot
os.environ["PTB_TIMEDELTA"] = "1"


def _clean_proxy_env() -> None:
    """Remove proxy env vars before provider clients are initialized.

    Preserves existing NO_PROXY entries and appends localhost entries.
    """
    proxy_vars = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]
    for var in proxy_vars:
        os.environ.pop(var, None)

    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    entries: list[str] = [
        entry.strip() for entry in no_proxy.split(",") if entry.strip()
    ]
    local_entries = ["127.0.0.1", "localhost"]
    for entry in local_entries:
        if entry not in entries:
            entries.append(entry)

    no_proxy = ",".join(entries)
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy


# Clean proxy environment before app initialization
_clean_proxy_env()

# Configure logging first (before any module logs)
_settings = get_settings()
configure_logging(_settings.log_file)


_SHUTDOWN_TIMEOUT_S = 5.0


async def _best_effort(
    name: str, awaitable, timeout_s: float = _SHUTDOWN_TIMEOUT_S
) -> None:
    """Run a shutdown step with timeout; never raise to callers."""
    try:
        await asyncio.wait_for(awaitable, timeout=timeout_s)
    except TimeoutError:
        logger.warning(f"Shutdown step timed out: {name} ({timeout_s}s)")
    except Exception as e:
        logger.warning(f"Shutdown step failed: {name}: {type(e).__name__}: {e}")


def _warn_if_process_auth_token(settings) -> None:
    """Warn when server auth was implicitly inherited from the shell."""
    uses_process_token = getattr(settings, "uses_process_anthropic_auth_token", None)
    if callable(uses_process_token) and uses_process_token():
        logger.warning(
            "ANTHROPIC_AUTH_TOKEN is set in the process environment but not in "
            "a configured .env file. The proxy will require that token. Add "
            "ANTHROPIC_AUTH_TOKEN= to .env to disable proxy auth, or set the "
            "same token in .env to make server auth explicit."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    settings = get_settings()
    logger.info("Starting Claude Code Proxy...")
    _warn_if_process_auth_token(settings)

    # Initialize messaging platform if configured
    messaging_platform = None
    message_handler = None
    cli_manager = None
    harness_bridge = None
    harness_config = None
    harness_error = None

    # The official DSH runtime is optional and starts lazily on the first DSH
    # request.  Invalid opt-in configuration fails closed for DSH routes while
    # leaving native Provider startup untouched.
    try:
        from harness.bridge import DeepSeekHarnessManager
        from harness.config import HarnessConfig, HarnessConfigError

        harness_config = HarnessConfig.from_env()
        if harness_config.enabled:
            auth_token = getattr(settings, "anthropic_auth_token", "")
            if not isinstance(auth_token, str) or not auth_token.strip():
                harness_error = (
                    "ANTHROPIC_AUTH_TOKEN must be configured when DSH_ENABLED=true"
                )
                logger.error(
                    "DeepSeek Harness configuration rejected: {}", harness_error
                )
            else:
                try:
                    harness_config.validate_cordis_config()
                except HarnessConfigError as exc:
                    harness_error = str(exc)
                    logger.error("DeepSeek Harness configuration rejected: {}", exc)
                else:
                    harness_bridge = DeepSeekHarnessManager(
                        harness_config,
                        api_key=getattr(settings, "deepseek_api_key", "") or None,
                        base_url=getattr(settings, "deepseek_base_url", "") or None,
                    )
                    logger.info("DeepSeek Harness integration enabled (lazy runtime)")
    except HarnessConfigError as exc:
        harness_error = str(exc)
        logger.error("DeepSeek Harness configuration rejected: {}", exc)

    try:
        # Use the messaging factory to create the right platform
        from messaging.platforms.factory import create_messaging_platform

        messaging_platform = create_messaging_platform(
            platform_type=settings.messaging_platform,
            bot_token=settings.telegram_bot_token,
            allowed_user_id=settings.allowed_telegram_user_id,
            agent_backend=getattr(settings, "agent_backend", "claude"),
            discord_bot_token=settings.discord_bot_token,
            allowed_discord_channels=settings.allowed_discord_channels,
        )

        if messaging_platform:
            from cli.approval import (
                DEFAULT_SAFE_COMMAND_PREFIXES,
                ApprovalPolicy,
                ApprovalScope,
            )
            from cli.manager import CLISessionManager
            from cli.workspace import resolve_workspace_root
            from messaging.handler import ClaudeMessageHandler
            from messaging.session import SessionStore

            # Setup workspace - configured roots must already exist and are
            # canonicalized before any CLI process receives them.
            workspace = str(
                resolve_workspace_root(
                    settings.allowed_dir or None, create=bool(settings.allowed_dir)
                )
            )

            # Session data stored in agent_workspace
            data_path = os.path.abspath(settings.claude_workspace)
            os.makedirs(data_path, exist_ok=True)

            api_url = f"http://{settings.host}:{settings.port}/v1"
            allowed_dirs = [workspace] if settings.allowed_dir else []
            plans_dir_abs = os.path.abspath(
                os.path.join(settings.claude_workspace, "plans")
            )
            plans_directory = os.path.relpath(plans_dir_abs, workspace)
            approval_scope_value = getattr(settings, "cli_auto_approval_scope", "once")
            if not isinstance(approval_scope_value, str):
                approval_scope_value = "once"
            try:
                approval_scope = ApprovalScope(approval_scope_value)
            except ValueError:
                approval_scope = ApprovalScope.ONCE
            approval_commands_value = getattr(
                settings, "cli_auto_approval_commands", ""
            )
            if not isinstance(approval_commands_value, str):
                approval_commands_value = ""
            approval_commands = [
                value.strip()
                for value in approval_commands_value.split(",")
                if value.strip()
            ]
            approval_workspaces_value = getattr(
                settings, "cli_auto_approval_workspaces", ""
            )
            if not isinstance(approval_workspaces_value, str):
                approval_workspaces_value = ""
            approval_workspaces = [
                value.strip()
                for value in approval_workspaces_value.split(",")
                if value.strip()
            ] or [workspace]
            approval_enabled = getattr(settings, "cli_auto_approval_enabled", False)
            if not isinstance(approval_enabled, bool):
                approval_enabled = False
            approval_allow_permanent = getattr(
                settings, "cli_auto_approval_allow_permanent", False
            )
            if not isinstance(approval_allow_permanent, bool):
                approval_allow_permanent = False
            mcp_config_path = getattr(settings, "cli_mcp_config", "")
            if not isinstance(mcp_config_path, str):
                mcp_config_path = ""
            approval_policy = ApprovalPolicy(
                enabled=approval_enabled,
                allowed_command_prefixes=approval_commands
                or DEFAULT_SAFE_COMMAND_PREFIXES,
                allowed_workspaces=approval_workspaces,
                max_auto_scope=approval_scope,
                allow_permanent=approval_allow_permanent,
            )
            cli_manager = CLISessionManager(
                workspace_path=workspace,
                api_url=api_url,
                allowed_dirs=allowed_dirs,
                plans_directory=plans_directory,
                agent_backend=getattr(settings, "agent_backend", "claude"),
                agent_permission_mode=getattr(
                    settings, "agent_permission_mode", "plan"
                ),
                claude_auth_mode=getattr(settings, "claude_auth_mode", "proxy"),
                claude_bin=getattr(settings, "claude_bin", "claude"),
                codex_bin=getattr(settings, "codex_bin", "codex"),
                codex_model=getattr(settings, "codex_model", None),
                codex_sandbox=getattr(settings, "codex_sandbox", "read-only"),
                codex_approval_required=getattr(
                    settings, "codex_approval_required", True
                ),
                preflight_runtime=getattr(settings, "cli_runtime_preflight", True),
                isolation_mode=getattr(settings, "cli_isolation_mode", "safe"),
                approval_policy=approval_policy,
                mcp_config_path=mcp_config_path,
            )

            # Initialize session store
            session_store = SessionStore(
                storage_path=os.path.join(data_path, "sessions.json")
            )

            # Create and register message handler
            message_handler = ClaudeMessageHandler(
                platform=messaging_platform,
                cli_manager=cli_manager,
                session_store=session_store,
            )

            # Restore tree state if available
            saved_trees = session_store.get_all_trees()
            if saved_trees:
                logger.info(f"Restoring {len(saved_trees)} conversation trees...")
                from messaging.trees.queue_manager import TreeQueueManager

                message_handler.replace_tree_queue(
                    TreeQueueManager.from_dict(
                        {
                            "trees": saved_trees,
                            "node_to_tree": session_store.get_node_mapping(),
                        },
                        queue_update_callback=message_handler.update_queue_positions,
                        node_started_callback=message_handler.mark_node_processing,
                    )
                )
                # Reconcile restored state - anything PENDING/IN_PROGRESS is lost across restart
                if message_handler.tree_queue.cleanup_stale_nodes() > 0:
                    # Sync back and save
                    tree_data = message_handler.tree_queue.to_dict()
                    session_store.sync_from_tree_data(
                        tree_data["trees"], tree_data["node_to_tree"]
                    )

            # Wire up the handler
            messaging_platform.on_message(message_handler.handle_message)

            # Start the platform
            await messaging_platform.start()
            logger.info(
                f"{messaging_platform.name} platform started with message handler"
            )

    except ImportError as e:
        logger.warning(f"Messaging module import error: {e}")
    except Exception as e:
        logger.error(f"Failed to start messaging platform: {e}")
        import traceback

        logger.error(traceback.format_exc())

    # Store in app state for access in routes
    app.state.messaging_platform = messaging_platform
    app.state.message_handler = message_handler
    app.state.cli_manager = cli_manager
    app.state.harness_bridge = harness_bridge
    app.state.harness_config = harness_config
    app.state.harness_error = harness_error

    yield

    # Cleanup
    if message_handler and hasattr(message_handler, "session_store"):
        try:
            message_handler.session_store.flush_pending_save()
        except Exception as e:
            logger.warning(f"Session store flush on shutdown: {e}")
    logger.info("Shutdown requested, cleaning up...")
    if messaging_platform:
        await _best_effort("messaging_platform.stop", messaging_platform.stop())
    if cli_manager:
        await _best_effort("cli_manager.stop_all", cli_manager.stop_all())
    if harness_bridge:
        await _best_effort("harness_bridge.close", harness_bridge.close())
    await _best_effort("cleanup_provider", cleanup_provider())

    # Ensure background limiter worker doesn't keep the loop alive.
    try:
        from messaging.limiter import MessagingRateLimiter

        await _best_effort(
            "MessagingRateLimiter.shutdown_instance",
            MessagingRateLimiter.shutdown_instance(),
            timeout_s=2.0,
        )
    except Exception:
        # Limiter may never have been imported/initialized.
        pass

    logger.info("Server shut down cleanly")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Claude Code Proxy",
        version="2.0.0",
        lifespan=lifespan,
    )

    # Register routes
    app.include_router(router)

    # Exception handlers
    @app.exception_handler(ProviderError)
    async def provider_error_handler(request: Request, exc: ProviderError):
        """Handle provider-specific errors and return Anthropic format."""
        logger.error(f"Provider Error: {exc.error_type} - {exc.message}")
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_anthropic_format(),
        )

    @app.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception):
        """Handle general errors and return Anthropic format."""
        logger.error(f"General Error: {exc!s}")
        import traceback

        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "An unexpected error occurred.",
                },
            },
        )

    return app


# Default app instance for uvicorn
app = create_app()

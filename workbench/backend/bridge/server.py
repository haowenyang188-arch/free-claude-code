"""Standalone Bridge server for testing and development."""

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from workbench.backend.bridge import BridgeService, create_bridge_router
from workbench.backend.domain.models import RuntimeKind
from workbench.backend.workflow.adapters.claude_code import ClaudeCodeAdapter
from workbench.backend.workflow.runners import RuntimeNeutralRunner


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    app = FastAPI(
        title="SOP Orchestrator Bridge",
        description="HTTP Bridge for Dify → Claude Code/Codex integration",
        version="1.0.0",
    )

    # CORS middleware for Dify integration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # In production, restrict to Dify domain
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize components
    claude_code_adapter = ClaudeCodeAdapter(preflight_runtime=True)
    runner = RuntimeNeutralRunner(adapters=[claude_code_adapter])
    runner.register_runtime("claude_code", RuntimeKind.CLAUDE_CODE)

    bridge_service = BridgeService(runner=runner)

    # Mount Bridge routes
    bridge_router = create_bridge_router(bridge_service=bridge_service)
    app.include_router(bridge_router)

    @app.get("/")
    async def root():
        return {
            "service": "SOP Orchestrator Bridge",
            "version": "1.0.0",
            "docs": "/docs",
            "health": "/api/bridge/health",
        }

    return app


app = create_app()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Start SOP Orchestrator Bridge")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    print(f"Starting SOP Orchestrator Bridge on {args.host}:{args.port}")
    print(f"API docs: http://{args.host}:{args.port}/docs")
    print(f"Health check: http://{args.host}:{args.port}/api/bridge/health")

    uvicorn.run(
        "workbench.backend.bridge.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )

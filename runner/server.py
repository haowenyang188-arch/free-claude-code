from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .service import (
    ALLOWED_STOP_FIELDS,
    CHANNELS,
    RunnerError,
    RunnerManager,
    UnsupportedFieldError,
    validate_start_payload,
)

manager = RunnerManager()
HOST = os.environ.get("RUNNER_HOST", "127.0.0.1")
try:
    PORT = int(os.environ.get("RUNNER_PORT", "8790"))
except ValueError:
    PORT = 8790


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await manager.shutdown()


app = FastAPI(title="Local Scraper Runner", version="1.0.0", lifespan=lifespan)
allowed_origins = [
    item.strip()
    for item in os.environ.get(
        "RUNNER_ALLOWED_ORIGINS", "http://127.0.0.1:3000,http://localhost:3000"
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def _error(
    error: RunnerError | TypeError | ValueError, status_code: int | None = None
) -> JSONResponse:
    if isinstance(error, RunnerError):
        code, message, status = error.code, error.message, error.status_code
    else:
        code, message, status = "INVALID_REQUEST", str(error), 422
    return JSONResponse(
        status_code=status_code or status,
        content={"error": {"code": code, "message": message}},
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "runner_version": "1.0.0",
        "channels": sorted(CHANNELS),
        "scraper_root": str(manager.scraper_root),
    }


@app.post("/runs/start", status_code=202)
async def start_run(request: Request):
    try:
        payload = await request.json()
        start_request = validate_start_payload(payload)
    except UnsupportedFieldError as exc:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "UNSUPPORTED_FIELDS",
                    "message": str(exc),
                    "fields": exc.fields,
                }
            },
        )
    except (TypeError, ValueError) as exc:
        return _error(exc)
    try:
        return await manager.start(start_request)
    except RunnerError as exc:
        return _error(exc)


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    try:
        return manager.get(run_id).public()
    except RunnerError as exc:
        return _error(exc)


@app.get("/runs/{run_id}/logs")
async def get_logs(run_id: str, after: int = 0):
    try:
        return manager.logs(run_id, after)
    except RunnerError as exc:
        return _error(exc)


@app.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return _error(ValueError("request body must be a JSON object"))
    extra = set(payload) - ALLOWED_STOP_FIELDS
    if extra:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "UNSUPPORTED_FIELDS",
                    "message": "unsupported field(s)",
                    "fields": sorted(extra),
                }
            },
        )
    kill_chrome = payload.get("kill_chrome")
    if kill_chrome is not None and not isinstance(kill_chrome, bool):
        return _error(ValueError("kill_chrome must be a boolean"))
    try:
        return await manager.stop(run_id, kill_chrome=kill_chrome)
    except RunnerError as exc:
        return _error(exc)


@app.post("/runs/{run_id}/register")
async def register_run(run_id: str):
    try:
        return await manager.register(run_id)
    except RunnerError as exc:
        return _error(exc)


@app.post("/runs/{run_id}/import")
async def import_run(run_id: str):
    try:
        return await manager.import_output(run_id)
    except RunnerError as exc:
        return _error(exc)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()

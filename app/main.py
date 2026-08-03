"""FastAPI composition and explicit role command entrypoint."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.health import router as health_router
from app.core.config import ConfigurationError, Role, Settings, load_settings
from app.core.observability import active_logging_runtime, configure_logging, log_event
from app.core.trace import resolve_trace_id, trace_id_context
from app.db.session import create_engine, session_factory
from app.roles import run_role_self_check
from app.schemas.response import ApiResponse, fail


def _response_content(response: ApiResponse[Any]) -> dict[str, Any]:
    return response.model_dump(mode="json")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the shared database engine for one application instance."""

    settings: Settings = app.state.settings
    existing_logging_runtime = active_logging_runtime()
    logging_runtime = existing_logging_runtime or configure_logging(settings.log_level, role=settings.role.value)
    owns_logging_runtime = existing_logging_runtime is None
    app.state.logging_runtime = logging_runtime
    try:
        engine = create_engine(settings)
        app.state.db_engine = engine
        app.state.session_factory = session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()
    finally:
        if owns_logging_runtime:
            logging_runtime.stop()


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if not isinstance(template, str):
        return "unmatched"
    root_path = request.scope.get("root_path")
    return f"{root_path}{template}" if isinstance(root_path, str) else template


async def request_trace_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Attach trace context and emit one structured completion event per request."""

    trace_id = resolve_trace_id(request.headers.get("x-request-id"))
    request.state.trace_id = trace_id
    started = perf_counter()
    request.state.started_at = started
    token = trace_id_context.set(trace_id)
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = round((perf_counter() - started) * 1000, 3)
        log_event(
            "request_complete",
            trace_id=trace_id,
            duration_ms=duration_ms,
            status=500,
            route=_route_template(request),
            error=type(exc).__name__,
        )
        raise
    else:
        duration_ms = round((perf_counter() - started) * 1000, 3)
        log_event(
            "request_complete",
            trace_id=trace_id,
            duration_ms=duration_ms,
            status=response.status_code,
            route=_route_template(request),
        )
    finally:
        trace_id_context.reset(token)
    response.headers["x-request-id"] = trace_id
    return response


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    http_exc = cast(StarletteHTTPException, exc)
    code = 40400 if http_exc.status_code == 404 else 40000 if http_exc.status_code < 500 else 50000
    body = fail(code, str(http_exc.detail), trace_id=request.state.trace_id)
    return JSONResponse(
        status_code=http_exc.status_code,
        content=_response_content(body),
        headers={"x-request-id": request.state.trace_id},
    )


async def validation_exception_handler(request: Request, _exc: Exception) -> JSONResponse:
    body = fail(42200, "request validation failed", trace_id=request.state.trace_id)
    return JSONResponse(
        status_code=422,
        content=_response_content(body),
        headers={"x-request-id": request.state.trace_id},
    )


async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    started = getattr(request.state, "started_at", perf_counter())
    log_event(
        "unhandled_exception",
        trace_id=request.state.trace_id,
        duration_ms=round((perf_counter() - started) * 1000, 3),
        status=500,
        route=_route_template(request),
        error=type(exc).__name__,
    )
    body = fail(50000, "internal server error", trace_id=request.state.trace_id)
    return JSONResponse(
        status_code=500,
        content=_response_content(body),
        headers={"x-request-id": request.state.trace_id},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Compose the API application; lifecycle and handlers live at module scope."""

    active = settings or load_settings()
    if active.role is not Role.API:
        raise ValueError(f"HTTP app requires role=api, got {active.role.value}")
    app = FastAPI(title="GROVE WS-0", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = active
    app.include_router(health_router, prefix="/api/v1")
    app.middleware("http")(request_trace_middleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)
    return app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GROVE role entrypoint")
    parser.add_argument("--role", choices=[role.value for role in Role])
    parser.add_argument("--self-check", action="store_true")
    return parser.parse_args()


def _load_cli_settings(cli_role: str | None) -> Settings:
    env_role = os.environ.get("GROVE_ROLE")
    if cli_role and env_role is not None and env_role != cli_role:
        raise ConfigurationError(f"CLI role {cli_role!r} conflicts with GROVE_ROLE {env_role!r}")
    return load_settings({"role": cli_role}) if cli_role else load_settings()


def main() -> int:
    args = _parse_args()
    settings = _load_cli_settings(args.role)
    if args.self_check:
        print(json.dumps(run_role_self_check(settings), ensure_ascii=False, sort_keys=True))
        return 0
    if settings.role is not Role.API:
        raise SystemExit("non-api roles must use --self-check; no idle worker loop is provided")
    import uvicorn

    logging_runtime = configure_logging(settings.log_level, role=settings.role.value)
    try:
        uvicorn.run(create_app(settings), host="0.0.0.0", port=8000, log_config=None)  # noqa: S104
    finally:
        logging_runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

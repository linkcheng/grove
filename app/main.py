"""FastAPI composition and explicit role command entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.execution import router as execution_router
from app.api.v1.health import router as health_router
from app.api.v1.observation import router as observation_router
from app.auth.context import active_tenant_context
from app.core.config import ConfigurationError, Role, Settings, load_settings
from app.core.errors import AppError
from app.core.observability import active_logging_runtime, configure_logging, log_event
from app.core.telemetry import record_operation
from app.core.telemetry_export import TelemetryExportRuntime
from app.core.trace import resolve_trace_id, trace_id_context
from app.db.session import create_engine, session_factory
from app.roles import run_role_self_check
from app.schemas.response import ApiResponse, fail
from app.services.observation import SSEBackfillCoalescer


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
        app.state.sse_backfill_coalescer = SSEBackfillCoalescer()
        telemetry_runtime = TelemetryExportRuntime()
        app.state.telemetry_export_runtime = telemetry_runtime
        telemetry_runtime.start()
        try:
            yield
        finally:
            telemetry_runtime.stop()
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
        record_operation(
            "api.request",
            duration_ms=float(duration_ms),
            role="api",
            operation="request",
            outcome="error",
        )
        raise
    else:
        duration_ms = round((perf_counter() - started) * 1000, 3)
        if response.status_code < 400:
            outcome = "ok"
        elif response.status_code == 409:
            outcome = "conflict"
        elif response.status_code < 500:
            outcome = "rejected"
        else:
            outcome = "error"
        log_event(
            "request_complete",
            trace_id=trace_id,
            duration_ms=duration_ms,
            status=response.status_code,
            route=_route_template(request),
        )
        record_operation(
            "api.request",
            duration_ms=float(duration_ms),
            role="api",
            operation="request",
            outcome=outcome,
        )
    finally:
        trace_id_context.reset(token)
    response.headers["x-request-id"] = trace_id
    return response


async def active_tenant_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Ensure an authenticated context cannot leak between reused requests."""

    token = active_tenant_context.set(None)
    try:
        return await call_next(request)
    finally:
        active_tenant_context.reset(token)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    http_exc = cast(StarletteHTTPException, exc)
    code = {
        400: 40000,
        401: 40100,
        403: 40300,
        404: 40400,
        409: 40900,
        422: 42200,
        503: 50301,
    }.get(http_exc.status_code, 50000 if http_exc.status_code >= 500 else 40000)
    error_code = {
        400: "BadRequest",
        401: "AuthenticationRequired",
        403: "Forbidden",
        404: "NotFound",
        409: "Conflict",
        422: "InputContractInvalid",
        503: "DependencyUnavailable",
    }.get(http_exc.status_code, "InternalServerError" if http_exc.status_code >= 500 else "BadRequest")
    body = fail(code, str(http_exc.detail), trace_id=request.state.trace_id, error_code=error_code)
    if http_exc.status_code in {401, 403}:
        log_event(
            "security_rejection",
            trace_id=request.state.trace_id,
            duration_ms=round((perf_counter() - getattr(request.state, "started_at", perf_counter())) * 1000, 3),
            status=http_exc.status_code,
            route=_route_template(request),
            reason_class="authentication" if http_exc.status_code == 401 else "authorization",
        )
    return JSONResponse(
        status_code=http_exc.status_code,
        content=_response_content(body),
        headers={"x-request-id": request.state.trace_id},
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    validation = cast(RequestValidationError, exc)
    violations = [
        {"field": ".".join(str(part) for part in error.get("loc", ())), "reason": str(error.get("msg", "invalid"))}
        for error in validation.errors()
    ]
    body = fail(
        42200,
        "request validation failed",
        trace_id=request.state.trace_id,
        error_code="InputContractInvalid",
        field_violations=violations,
    )
    return JSONResponse(
        status_code=422,
        content=_response_content(body),
        headers={"x-request-id": request.state.trace_id},
    )


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    app_error = cast(AppError, exc)
    body = fail(
        app_error.code,
        app_error.message,
        trace_id=request.state.trace_id,
        error_code=app_error.error_code,
        retry_after=app_error.retry_after,
    )
    headers = {"x-request-id": request.state.trace_id}
    if app_error.retry_after is not None:
        headers["retry-after"] = str(app_error.retry_after)
    if app_error.status_code == 403:
        log_event(
            "security_rejection",
            trace_id=request.state.trace_id,
            duration_ms=round((perf_counter() - getattr(request.state, "started_at", perf_counter())) * 1000, 3),
            status=403,
            route=_route_template(request),
            reason_class="authorization",
        )
    return JSONResponse(
        status_code=app_error.status_code,
        content=_response_content(body),
        headers=headers,
    )


async def database_dependency_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map bounded connection/pool failures to the documented 503 contract."""

    log_event(
        "database_dependency_unavailable",
        trace_id=request.state.trace_id,
        duration_ms=round((perf_counter() - getattr(request.state, "started_at", perf_counter())) * 1000, 3),
        status=503,
        route=_route_template(request),
        error=type(exc).__name__,
    )
    body = fail(
        50302,
        "dependency unavailable",
        trace_id=request.state.trace_id,
        error_code="DependencyUnavailable",
        retry_after=1,
    )
    return JSONResponse(
        status_code=503,
        content=_response_content(body),
        headers={"x-request-id": request.state.trace_id, "retry-after": "1"},
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
    body = fail(50000, "internal server error", trace_id=request.state.trace_id, error_code="InternalServerError")
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
    app.include_router(execution_router, prefix="/api/v1")
    app.include_router(observation_router, prefix="/api/v1")
    app.middleware("http")(active_tenant_context_middleware)
    app.middleware("http")(request_trace_middleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(AppError, app_error_handler)
    for database_error in (OperationalError, InterfaceError, SQLAlchemyTimeoutError):
        app.add_exception_handler(database_error, database_dependency_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)
    return app


def _run_runtime_worker(settings: Settings) -> int:
    """Start the bounded runtime worker poll loop."""
    from app.execution import PostgresExecutionDriver
    from app.worker.loop import run_worker

    engine = create_engine(settings)
    session_maker = session_factory(engine)
    driver = PostgresExecutionDriver(
        session_factory=session_maker,
        lease_seconds=30.0,
    )
    telemetry_runtime = TelemetryExportRuntime()
    telemetry_runtime.start()

    async def run_composed_worker() -> None:
        if settings.inference_mode == "disabled":
            await run_worker(
                driver=driver,
                tenant_id=settings.worker_tenant_id,
                worker_id=settings.worker_id,
                runtime_build_hash=settings.runtime_build_hash,
                database_url=settings.database_url_value(),
            )
            return
        from app.worker.inference import production_inference_lifespan

        async with production_inference_lifespan(
            app_env=settings.app_env,
            runtime_build_hash=settings.runtime_build_hash,
        ) as (inference_port, inference_request_factory):
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from sqlalchemy.ext.asyncio import create_async_engine as _cae

            from app.asset_risk.composition import compose_asset_risk_kernel

            _engine = _cae(settings.database_url_value())
            _sessions = async_sessionmaker(_engine, expire_on_commit=False)
            asset_risk_kernel = compose_asset_risk_kernel(
                inference_port=inference_port,
                inference_request_factory=inference_request_factory,
                runtime_session_factory=_sessions,
                worker_tenant_id=settings.worker_tenant_id,
            )
            await run_worker(
                driver=driver,
                tenant_id=settings.worker_tenant_id,
                worker_id=settings.worker_id,
                runtime_build_hash=settings.runtime_build_hash,
                database_url=settings.database_url_value(),
                inference_port=inference_port,
                inference_request_factory=inference_request_factory,
                asset_risk_kernel=asset_risk_kernel,
            )
            await _engine.dispose()

    try:
        asyncio.run(run_composed_worker())
    finally:
        telemetry_runtime.stop()
    return 0


def _run_projection_reconciliation(settings: Settings) -> int:
    """Start the bounded projection/reconciliation poll loop."""

    engine = create_engine(settings)
    session_maker = session_factory(engine)
    telemetry_runtime = TelemetryExportRuntime()
    telemetry_runtime.start()
    try:
        asyncio.run(_run_projection_loop(session_maker))
    finally:
        telemetry_runtime.stop()
    return 0


async def _run_projection_loop(session_maker: Any) -> None:
    import signal as _signal

    from app.observation.projection import ProjectionReconciler

    reconciler = ProjectionReconciler(session_maker)
    loop = asyncio.get_event_loop()
    for sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            loop.add_signal_handler(sig, reconciler.request_shutdown)
        except NotImplementedError:
            pass
    await reconciler.run()


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
        if args.self_check:
            print(json.dumps(run_role_self_check(settings), ensure_ascii=False, sort_keys=True))
            return 0
        if settings.role is Role.RUNTIME_WORKER:
            return _run_runtime_worker(settings)
        if settings.role is Role.PROJECTION_RECONCILIATION:
            return _run_projection_reconciliation(settings)
        raise SystemExit("non-api/non-runtime_worker/non-projection roles must use --self-check")
    import uvicorn

    logging_runtime = configure_logging(settings.log_level, role=settings.role.value)
    try:
        uvicorn.run(create_app(settings), host="0.0.0.0", port=8000, log_config=None)  # noqa: S104
    finally:
        logging_runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

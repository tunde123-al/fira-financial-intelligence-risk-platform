"""FastAPI application factory.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
OpenAPI docs at /docs (Swagger UI) and /openapi.json.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    routes_admin,
    routes_cases,
    routes_config,
    routes_copilot,
    routes_core,
    routes_investigations,
    routes_knowledge,
    routes_monitoring,
    routes_network,
    routes_quality,
)
from app.config import get_settings
from app.core.observability import METRICS, Timer, bind, configure_logging, log_event, request_id_var
from app.security.hardening import BodyLimitMiddleware, LoginThrottle, TokenRevocations, production_problems
from app.security.ratelimit import RateLimiter

log = logging.getLogger("fira.api")

DESCRIPTION = """
Financial Intelligence & Risk Agent — decision support for financial-crime investigation.

All consequential actions (freezing/closing accounts, filing reports) remain with authorised humans.
Authenticate with `POST /api/auth/login` and send `Authorization: Bearer <token>`.
"""


OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness and readiness."},
    {"name": "metrics", "description": "Prometheus metrics: application, business and database gauges."},
    {"name": "auth", "description": "Login, logout, current user."},
    {"name": "transactions", "description": "Transactions and the ingestion gate."},
    {"name": "monitoring", "description": "Monitoring runs, detectors and KPIs."},
    {"name": "alerts", "description": "Alert queue, workflow, outcome feedback, quality and investigator work."},
    {"name": "triage", "description": "Explainable heuristic alert triage."},
    {"name": "risk", "description": "Customer risk assessment."},
    {"name": "cases", "description": "Case management."},
    {"name": "investigations", "description": "Investigations, money-mule indicators and graph questions."},
    {"name": "evidence", "description": "Evidence attached to investigations and cases."},
    {"name": "graph", "description": "Transfer-graph queries."},
    {"name": "data-quality", "description": "Ingestion batches, quarantined rows, coverage and success."},
    {"name": "copilot", "description": "Grounded AI Investigation Copilot (structured, read-only)."},
    {"name": "config", "description": "Configuration governance and change log."},
    {"name": "evaluation", "description": "Offline evaluation on labelled synthetic data."},
    {"name": "audit", "description": "Append-only audit trail."},
]


def create_app(container: Any | None = None) -> FastAPI:
    settings = container.settings if container is not None else get_settings()
    configure_logging(settings.log_level, environment=settings.environment)
    problems = production_problems(settings)
    if problems:  # refuse to start misconfigured in production (never prints a secret value)
        raise RuntimeError("unsafe production configuration: " + "; ".join(problems))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "container", None) is None:
            from app.services.container import build_container

            app.state.container = build_container(settings)
        from app.security.auth import bootstrap_users, jwt_secret

        jwt_secret(app.state.container.settings)  # fail fast on missing secret in production
        created = bootstrap_users(app.state.container.store, app.state.container.settings)
        log_event(log, "startup", users_bootstrapped=created, agent_engine=app.state.container.agent.engine_name)
        yield

    docs = settings.docs_enabled
    app = FastAPI(title="FIRA — Financial Intelligence & Risk Agent", version="1.0.0", description=DESCRIPTION,
                  lifespan=lifespan, openapi_tags=OPENAPI_TAGS, docs_url="/docs" if docs else None,
                  redoc_url="/redoc" if docs else None, openapi_url="/openapi.json" if docs else None)
    app.state.container = container
    app.state.throttle = LoginThrottle(settings.login_max_failures, settings.login_window_minutes * 60.0)
    app.state.revocations = TokenRevocations()
    limiter = RateLimiter(settings.rate_limit_per_minute)
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)

    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list(), allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id")
        rid = rid if rid and len(rid) <= 64 and rid.replace("-", "").isalnum() else uuid.uuid4().hex
        timer = Timer()
        with bind(request_id=rid):
            key = request.client.host if request.client else "unknown"
            auth = request.headers.get("authorization", "")
            if auth:
                key = f"tok:{hash(auth) & 0xffffffff:x}"
            if not request.url.path.startswith("/health"):
                ok, retry = limiter.allow(key)
                if not ok:
                    METRICS.inc("fira_http_rate_limited_total")
                    return JSONResponse({"detail": "rate limit exceeded", "request_id": rid}, status_code=429,
                                        headers={"Retry-After": str(int(retry) + 1), "X-Request-ID": rid})
            try:
                response = await call_next(request)
            except Exception as e:  # secure error handling: no internals in the response
                log_event(log, "unhandled_exception", logging.ERROR, path=request.url.path, exc_type=type(e).__name__)
                METRICS.inc("fira_http_errors_total", path=request.url.path)
                response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
            response.headers["X-Request-ID"] = rid
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
            response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
            response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
            if settings.is_production:  # TLS is terminated in front of the app (Render, a reverse proxy)
                response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            if settings.frontend_dist_dir is not None and not request.url.path.startswith(("/api", "/docs", "/redoc", "/openapi")):
                # same policy as the nginx container; not applied to Swagger UI, which loads its assets from a CDN
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            METRICS.inc("fira_http_requests_total", method=request.method, path=path, status=str(response.status_code))
            METRICS.observe("fira_http_latency_ms", timer.ms, path=path)
            log_event(log, "request", operation=f"{request.method} {path}", method=request.method, path=path,
                      status=response.status_code, latency_ms=timer.ms, duration_ms=timer.ms,
                      user_id=getattr(request.state, "principal", None) and request.state.principal.user_id)
            return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        errs = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()[:10]]
        return JSONResponse({"detail": "validation error", "errors": errs, "request_id": request_id_var.get()},
                            status_code=422)

    app.include_router(routes_core.router)
    app.include_router(routes_investigations.router)
    app.include_router(routes_knowledge.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_monitoring.router)
    app.include_router(routes_cases.router)
    app.include_router(routes_config.router)
    app.include_router(routes_copilot.router)
    app.include_router(routes_network.router)
    app.include_router(routes_network.mule)
    app.include_router(routes_quality.router)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            from fastapi.openapi.utils import get_openapi

            from app.api.tags import retag

            app.openapi_schema = retag(get_openapi(title=app.title, version=app.version, description=app.description,
                                                   routes=app.routes, tags=OPENAPI_TAGS))
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    dist = settings.frontend_dist_dir
    if dist is not None and (Path(dist) / "index.html").exists():
        # Registered last so every API route (and /docs) keeps precedence over the static catch-all.
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    return app


app = create_app()

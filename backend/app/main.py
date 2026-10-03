"""FastAPI application factory.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
OpenAPI docs at /docs (Swagger UI) and /openapi.json.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import routes_admin, routes_core, routes_investigations, routes_knowledge
from app.config import get_settings
from app.core.observability import METRICS, Timer, bind, configure_logging, log_event, request_id_var
from app.security.ratelimit import RateLimiter

log = logging.getLogger("fira.api")

DESCRIPTION = """
Financial Intelligence & Risk Agent — decision support for financial-crime investigation.

All consequential actions (freezing/closing accounts, filing reports) remain with authorised humans.
Authenticate with `POST /api/auth/login` and send `Authorization: Bearer <token>`.
"""


def create_app(container: Any | None = None) -> FastAPI:
    settings = container.settings if container is not None else get_settings()
    configure_logging(settings.log_level)

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

    app = FastAPI(title="FIRA — Financial Intelligence & Risk Agent", version="1.0.0", description=DESCRIPTION,
                  lifespan=lifespan)
    app.state.container = container
    limiter = RateLimiter(settings.rate_limit_per_minute)

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
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            METRICS.inc("fira_http_requests_total", method=request.method, path=path, status=str(response.status_code))
            METRICS.observe("fira_http_latency_ms", timer.ms, path=path)
            log_event(log, "request", method=request.method, path=path, status=response.status_code,
                      latency_ms=timer.ms, user_id=getattr(request.state, "principal", None) and
                      request.state.principal.user_id)
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
    return app


app = create_app()

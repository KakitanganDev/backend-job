"""
Observability: structured JSON logging, request-id propagation, OpenTelemetry.

Wired up at app startup. Three things:

1. structlog renders every log record as a single JSON line, with a
   `request_id` field bound from a contextvar — so every log emitted
   while handling a request is tagged with the same id, including
   logs from libraries that use the stdlib logger.

2. A FastAPI middleware reads / generates an `X-Request-ID` and binds
   it for the duration of the request.

3. OpenTelemetry SDK with FastAPI + SQLAlchemy auto-instrumentation
   and a console span exporter, so spans print to stdout without any
   external collector. Toggleable via OTEL_ENABLED=false for tests.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextvars import ContextVar

import structlog
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def _add_request_id(_, __, event_dict):
    rid = _request_id_var.get()
    if rid is not None:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    pre_chain = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _add_request_id,
        timestamper,
    ]

    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=[logging.StreamHandler()],
    )
    # Replace the formatter on every handler with structlog's renderer.
    for handler in logging.getLogger().handlers:
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=pre_chain,
                processor=structlog.processors.JSONRenderer(),
            )
        )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            _add_request_id,
            timestamper,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Bind X-Request-ID to a contextvar so every log line is tagged."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = _request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response


def setup_otel(app: FastAPI, engine) -> None:
    """Wire OpenTelemetry FastAPI + SQLAlchemy instrumentation.

    Uses a console span exporter so this is self-contained for the demo —
    no collector required. In production you'd swap to OTLP and point at
    the cluster collector.
    """
    if os.getenv("OTEL_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return

    from opentelemetry import trace
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )

    resource = Resource.create(
        {"service.name": os.getenv("OTEL_SERVICE_NAME", "kakitangan-leave-api")}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=engine)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)

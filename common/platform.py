"""Shared platform helpers used by every service: Consul registration,
service discovery, and OpenTelemetry tracing setup."""

import os
import socket
import time
from contextlib import asynccontextmanager

import httpx

CONSUL_URL = os.environ.get("CONSUL_HTTP_ADDR", "http://consul:8500")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "unknown-service")
SERVICE_PORT = int(os.environ.get("SERVICE_PORT", "8000"))
# Hostname inside the Docker network. Consul calls this host for health checks,
# and other services use it as the target address, so it must be reachable
# from other containers (not "localhost").
SERVICE_HOST = os.environ.get("SERVICE_HOST", socket.gethostname())
SERVICE_ID = f"{SERVICE_NAME}-{SERVICE_HOST}"


def register(attempts: int = 10, delay: float = 2.0) -> None:
    """Register this instance in Consul, with an HTTP health check.

    Retries because a service container can boot before Consul is ready to
    accept registrations."""
    payload = {
        "ID": SERVICE_ID,
        "Name": SERVICE_NAME,
        "Address": SERVICE_HOST,
        "Port": SERVICE_PORT,
        "Check": {
            "HTTP": f"http://{SERVICE_HOST}:{SERVICE_PORT}/health",
            "Interval": "10s",
            "Timeout": "2s",
            # Drop the entry if the container dies without deregistering.
            "DeregisterCriticalServiceAfter": "1m",
        },
    }
    for attempt in range(attempts):
        try:
            httpx.put(
                f"{CONSUL_URL}/v1/agent/service/register", json=payload, timeout=5.0
            ).raise_for_status()
            print(f"[platform] registered {SERVICE_ID} in Consul", flush=True)
            return
        except Exception as exc:  # noqa: BLE001 - startup race, retry is the point
            if attempt == attempts - 1:
                raise
            print(f"[platform] Consul not ready ({exc}), retrying...", flush=True)
            time.sleep(delay)


def deregister() -> None:
    """Remove this instance from Consul on graceful shutdown."""
    try:
        httpx.put(f"{CONSUL_URL}/v1/agent/service/deregister/{SERVICE_ID}", timeout=5.0)
        print(f"[platform] deregistered {SERVICE_ID}", flush=True)
    except Exception as exc:  # noqa: BLE001 - shutdown must not crash the process
        print(f"[platform] deregister failed: {exc}", flush=True)


def discover(name: str) -> str:
    """Return the base URL of one healthy instance of `name` from Consul.

    Queried per call on purpose: no cache means no stale address, and the
    lookup is visible in the logs during a demo.
    ponytail: always picks the first instance, no load balancing across replicas."""
    response = httpx.get(
        f"{CONSUL_URL}/v1/health/service/{name}", params={"passing": "true"}, timeout=5.0
    )
    response.raise_for_status()
    instances = response.json()
    if not instances:
        raise LookupError(f"no healthy instance of '{name}' registered in Consul")
    service = instances[0]["Service"]
    return f"http://{service['Address']}:{service['Port']}"


def init_tracing(app) -> None:
    """Auto-instrument FastAPI and httpx so one user request shows up in Jaeger
    as a single connected trace across every service it touches."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    # Health and registry polling would otherwise flood Jaeger with noise and
    # bury the traces that matter during a demo.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,registry")
    # Instruments outgoing calls too: this is what links caller and callee spans.
    HTTPXClientInstrumentor().instrument()


@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan: join the service registry on boot, leave it on shutdown."""
    register()
    yield
    deregister()

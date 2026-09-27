"""API Gateway: the single entry point for clients.

It owns no data. Its jobs are routing (resolving targets through Consul),
authentication (verifying JWTs so downstream services do not each re-implement
it), and serving the demo dashboard."""

from pathlib import Path

import httpx
import jwt
import os
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from common.platform import CONSUL_URL, discover, init_tracing, lifespan

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret")
JWT_ALGORITHM = "HS256"

# URL prefix -> service name registered in Consul. Login lives on the user
# service, which is why "auth" maps there too.
ROUTES = {
    "users": "user-service",
    "auth": "user-service",
    "products": "product-service",
    "orders": "order-service",
}
# Prefixes that require a valid token before the request is forwarded.
PROTECTED = {"orders"}
# Hop-by-hop headers must not be forwarded; x-user-id is stripped so a client
# cannot forge an identity that downstream services trust.
BLOCKED_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "x-user-id",
}

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="API Gateway", lifespan=lifespan)
init_tracing(app)
# Instrumented by OpenTelemetry, so forwarded calls become child spans and the
# whole request shows up in Jaeger as one trace.
client = httpx.AsyncClient(timeout=15.0)


def authenticate(request: Request) -> str:
    """Verify the Bearer token and return the user id it was issued for."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        payload = jwt.decode(
            header.split(" ", 1)[1], JWT_SECRET, algorithms=[JWT_ALGORITHM]
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}")
    return payload["sub"]


@app.get("/health")
def health():
    """Health endpoint polled by Consul."""
    return {"status": "ok", "service": "gateway"}


@app.get("/")
def dashboard():
    """Serve the single-page demo dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/registry")
async def registry():
    """Live snapshot of the Consul registry, so the dashboard can show which
    services are up without the viewer switching tabs."""
    try:
        response = await client.get(f"{CONSUL_URL}/v1/health/state/any", timeout=5.0)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - report, do not crash the page
        raise HTTPException(status_code=503, detail=f"consul unreachable: {exc}")

    # A service counts as healthy only when every one of its checks passes.
    statuses: dict[str, set[str]] = {}
    for check in response.json():
        name = check.get("ServiceName")
        if name:
            statuses.setdefault(name, set()).add(check["Status"])
    return [
        {
            "service": name,
            "status": "passing" if found == {"passing"} else sorted(found)[0],
        }
        for name, found in sorted(statuses.items())
    ]


@app.api_route(
    "/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
async def proxy(path: str, request: Request):
    """Forward a client request to the service that owns that path prefix."""
    prefix = path.split("/")[0]
    service_name = ROUTES.get(prefix)
    if not service_name:
        raise HTTPException(status_code=404, detail=f"no route for '/{prefix}'")

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in BLOCKED_REQUEST_HEADERS
    }
    # The gateway is the trust boundary: it authenticates once and passes the
    # verified identity inward as a header.
    if prefix in PROTECTED:
        headers["X-User-Id"] = authenticate(request)

    try:
        target = discover(service_name)
    except Exception as exc:  # noqa: BLE001 - no healthy instance is a 503
        raise HTTPException(status_code=503, detail=f"{service_name} unavailable: {exc}")

    try:
        upstream = await client.request(
            request.method,
            f"{target}/{path}",
            content=await request.body(),
            params=request.query_params,
            headers=headers,
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"{service_name} did not answer: {exc}")

    # Only content-type is passed back: httpx already decoded the body, so the
    # upstream content-encoding and content-length no longer describe it.
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )

"""Order Service: the only service that talks to other services.
It resolves their addresses through Consul, never through hardcoded hostnames."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import JSON, Column, text
from sqlmodel import Field, Session, SQLModel, create_engine

from common.platform import deregister, discover, init_tracing, register

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
# Reused connection pool for downstream calls; instrumented by OpenTelemetry so
# these calls appear as child spans of the incoming request.
client = httpx.Client(timeout=10.0)


class Order(SQLModel, table=True):
    """Database table owned by this service. Line items are stored as JSON: this
    service must not own a foreign key into another service's database."""

    __tablename__ = "orders"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int
    items: list = Field(sa_column=Column(JSON))
    total: int
    status: str = "CONFIRMED"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OrderItem(BaseModel):
    product_id: int
    quantity: int = PydanticField(gt=0)


class OrderCreate(BaseModel):
    """`user_id` is only read when the request did not come through the gateway
    (direct calls during debugging); normally the gateway supplies X-User-Id."""

    items: list[OrderItem] = PydanticField(min_length=1)
    user_id: int | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables, then join the Consul registry; leave it on shutdown."""
    SQLModel.metadata.create_all(engine)
    register()
    yield
    deregister()


app = FastAPI(title="Order Service", lifespan=lifespan)
init_tracing(app)


def get_session():
    """One database session per request."""
    with Session(engine) as session:
        yield session


def locate(service_name: str) -> str:
    """Resolve a sibling service through Consul, or fail with 503 if no healthy
    instance is registered."""
    try:
        return discover(service_name)
    except Exception as exc:  # noqa: BLE001 - discovery failure is a 503, not a crash
        raise HTTPException(status_code=503, detail=f"{service_name} unavailable: {exc}")


def release_all(product_url: str, lines: list[dict]) -> None:
    """Return stock that was already reserved for an order that then failed.

    ponytail: best-effort compensation, no retry queue or outbox. If a release
    call itself fails, the loss is logged and left for manual repair."""
    for line in lines:
        try:
            client.post(
                f"{product_url}/products/{line['product_id']}/release",
                json={"quantity": line["quantity"]},
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[order] WARNING release failed for product {line['product_id']}: {exc}",
                flush=True,
            )


@app.get("/health")
def health():
    """Health endpoint polled by Consul; fails if the database is unreachable."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - must answer, not crash
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}")
    return {"status": "ok", "service": "order-service"}


@app.post("/orders", response_model=Order, status_code=201)
def create_order(
    body: OrderCreate,
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
    session: Session = Depends(get_session),
):
    """Create an order across three services:
    1. confirm the buyer exists (User Service),
    2. read authoritative prices (Product Service),
    3. reserve stock item by item, compensating if one fails."""
    user_id = x_user_id if x_user_id is not None else body.user_id
    if user_id is None:
        raise HTTPException(status_code=400, detail="missing user: send X-User-Id or user_id")

    # Step 1: the buyer must exist. Price and stock are never taken on trust
    # from the client, only from the owning service.
    user_response = client.get(f"{locate('user-service')}/users/{user_id}")
    if user_response.status_code == 404:
        raise HTTPException(status_code=404, detail="user not found")
    user_response.raise_for_status()

    product_url = locate("product-service")

    # Step 2: resolve each line against the catalog.
    lines: list[dict] = []
    for item in body.items:
        response = client.get(f"{product_url}/products/{item.product_id}")
        if response.status_code == 404:
            raise HTTPException(
                status_code=404, detail=f"product {item.product_id} not found"
            )
        response.raise_for_status()
        product = response.json()
        lines.append(
            {
                "product_id": product["id"],
                "name": product["name"],
                "price": product["price"],
                "quantity": item.quantity,
                "subtotal": product["price"] * item.quantity,
            }
        )

    # Step 3: reserve stock. A failure halfway through must not leave earlier
    # items reserved for an order that never exists.
    reserved: list[dict] = []
    for line in lines:
        response = client.post(
            f"{product_url}/products/{line['product_id']}/reserve",
            json={"quantity": line["quantity"]},
        )
        if response.is_success:
            reserved.append(line)
            continue
        release_all(product_url, reserved)
        if response.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail=f"insufficient stock for product {line['product_id']}",
            )
        raise HTTPException(status_code=502, detail="stock reservation failed")

    order = Order(
        user_id=user_id,
        items=lines,
        total=sum(line["subtotal"] for line in lines),
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


@app.get("/orders/{order_id}", response_model=Order)
def get_order(
    order_id: int,
    x_user_id: int | None = Header(default=None, alias="X-User-Id"),
    session: Session = Depends(get_session),
):
    """Read a single order, but only the requester's own.

    Someone else's order is reported as missing rather than forbidden, so the
    response does not reveal which order ids exist."""
    if x_user_id is None:
        raise HTTPException(status_code=400, detail="missing user: send X-User-Id")
    order = session.get(Order, order_id)
    if not order or order.user_id != x_user_id:
        raise HTTPException(status_code=404, detail="order not found")
    return order

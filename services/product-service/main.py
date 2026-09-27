"""Product Service: catalog plus stock reserve/release.
Prices are whole rupiah (integers), which keeps money arithmetic exact without
dragging Decimal serialization into the API."""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import text
from sqlmodel import Field, Session, SQLModel, create_engine, select

from common.platform import deregister, init_tracing, register

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)


class Product(SQLModel, table=True):
    """Database table owned by this service."""

    __tablename__ = "products"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    price: int  # whole rupiah
    stock: int


class ProductCreate(BaseModel):
    name: str = PydanticField(min_length=1, max_length=120)
    price: int = PydanticField(ge=0)
    stock: int = PydanticField(ge=0)


class StockChange(BaseModel):
    """Quantity for a reserve or release call."""

    quantity: int = PydanticField(gt=0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables, then join the Consul registry; leave it on shutdown."""
    SQLModel.metadata.create_all(engine)
    register()
    yield
    deregister()


app = FastAPI(title="Product Service", lifespan=lifespan)
init_tracing(app)


def get_session():
    """One database session per request."""
    with Session(engine) as session:
        yield session


@app.get("/health")
def health():
    """Health endpoint polled by Consul; fails if the database is unreachable."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - must answer, not crash
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}")
    return {"status": "ok", "service": "product-service"}


@app.post("/products", response_model=Product, status_code=201)
def create_product(body: ProductCreate, session: Session = Depends(get_session)):
    """Add a product to the catalog."""
    product = Product(name=body.name, price=body.price, stock=body.stock)
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


@app.get("/products", response_model=list[Product])
def list_products(session: Session = Depends(get_session)):
    """Full catalog. ponytail: no pagination, the demo catalog is tiny."""
    return session.exec(select(Product).order_by(Product.id)).all()


@app.get("/products/{product_id}", response_model=Product)
def get_product(product_id: int, session: Session = Depends(get_session)):
    """Used by Order Service to read the authoritative price and stock."""
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="product not found")
    return product


@app.post("/products/{product_id}/reserve")
def reserve_stock(
    product_id: int, body: StockChange, session: Session = Depends(get_session)
):
    """Decrement stock only if enough is available.

    Done as a single conditional UPDATE instead of read-then-write: the database
    checks and decrements atomically, so two concurrent orders cannot both pass
    the check and oversell the same unit."""
    if not session.get(Product, product_id):
        raise HTTPException(status_code=404, detail="product not found")
    result = session.execute(
        text(
            "UPDATE products SET stock = stock - :qty "
            "WHERE id = :pid AND stock >= :qty RETURNING stock"
        ),
        {"qty": body.quantity, "pid": product_id},
    ).first()
    session.commit()
    if result is None:
        raise HTTPException(status_code=409, detail="insufficient stock")
    return {"product_id": product_id, "reserved": body.quantity, "stock": result[0]}


@app.post("/products/{product_id}/release")
def release_stock(
    product_id: int, body: StockChange, session: Session = Depends(get_session)
):
    """Give reserved stock back. Called by Order Service to compensate when an
    order fails partway through."""
    result = session.execute(
        text("UPDATE products SET stock = stock + :qty WHERE id = :pid RETURNING stock"),
        {"qty": body.quantity, "pid": product_id},
    ).first()
    session.commit()
    if result is None:
        raise HTTPException(status_code=404, detail="product not found")
    return {"product_id": product_id, "released": body.quantity, "stock": result[0]}

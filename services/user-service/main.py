"""User Service: registration, login (issues JWT), and user lookup.
Owns its own database; no other service touches the users table directly."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import text
from sqlmodel import Field, Session, SQLModel, create_engine, select

from common.platform import deregister, init_tracing, register

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret")
JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = 12

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)


class User(SQLModel, table=True):
    """Database table owned by this service."""

    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    email: str
    hashed_password: str


class RegisterRequest(BaseModel):
    """Request body for registration. Password is length-capped because bcrypt
    silently ignores anything past 72 bytes."""

    username: str = PydanticField(min_length=3, max_length=50)
    email: str = PydanticField(min_length=3, max_length=255)
    password: str = PydanticField(min_length=6, max_length=72)


class LoginRequest(BaseModel):
    username: str
    password: str = PydanticField(max_length=72)


class UserOut(BaseModel):
    """Public view of a user: never exposes the password hash."""

    id: int
    username: str
    email: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables, then join the Consul registry; leave it on shutdown."""
    SQLModel.metadata.create_all(engine)
    register()
    yield
    deregister()


app = FastAPI(title="User Service", lifespan=lifespan)
init_tracing(app)


def get_session():
    """One database session per request."""
    with Session(engine) as session:
        yield session


@app.get("/health")
def health():
    """Health endpoint polled by Consul. Touches the database on purpose, so a
    service with a dead database is reported unhealthy instead of falsely green."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - must answer, not crash
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}")
    return {"status": "ok", "service": "user-service"}


@app.post("/users", response_model=UserOut, status_code=201)
def register_user(body: RegisterRequest, session: Session = Depends(get_session)):
    """Create a user with a bcrypt-hashed password."""
    taken = session.exec(select(User).where(User.username == body.username)).first()
    if taken:
        raise HTTPException(status_code=409, detail="username already taken")
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    user = User(username=body.username, email=body.email, hashed_password=hashed)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@app.post("/auth/login")
def login(body: LoginRequest, session: Session = Depends(get_session)):
    """Verify credentials and return a signed JWT the gateway can validate."""
    user = session.exec(select(User).where(User.username == body.username)).first()
    if not user or not bcrypt.checkpw(body.password.encode(), user.hashed_password.encode()):
        raise HTTPException(status_code=401, detail="invalid username or password")
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "user_id": user.id}


@app.get("/users/{user_id}", response_model=UserOut)
def get_user(user_id: int, session: Session = Depends(get_session)):
    """Used by Order Service to validate that an order's owner exists."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    return user

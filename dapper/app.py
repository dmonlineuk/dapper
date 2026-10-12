
import os
import asyncio
import secrets

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

# Configuration
HEARTBEAT_TIMEOUT = int(
    os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "90")
)

# The API token is loaded at startup, not hard-coded.
API_TOKEN: str | None = None

# In-memory state, keyed by persistent client UUID.
hosts: dict[str, dict[str, Any]] = {}
hosts_lock = asyncio.Lock()

# HTTP bearer authentication.
bearer_scheme = HTTPBearer(auto_error=False)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def validate_api_token(token: str | None) -> str:
    """Validate the configured API token."""

    if not token:
        raise RuntimeError(
            "DAPPER_API_TOKEN must be configured."
        )

    if len(token) < 32:
        raise RuntimeError(
            "DAPPER_API_TOKEN must be at least 32 characters."
        )

    return token

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration before accepting requests."""

    global API_TOKEN

    API_TOKEN = validate_api_token(
        os.getenv("DAPPER_API_TOKEN")
    )

    yield

    API_TOKEN = None

app = FastAPI(
    title="Dapper API",
    version="0.3.0",
    lifespan=lifespan,
)

async def require_authentication(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        bearer_scheme
    ),
):
    """Require a valid bearer token."""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if API_TOKEN is None or not secrets.compare_digest(
        credentials.credentials,
        API_TOKEN,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

class Telemetry(BaseModel):
    client_id: UUID
    hostname: str = Field(min_length=1, max_length=253)
    platform: str = Field(min_length=1, max_length=64)
    architecture: str = Field(min_length=1, max_length=64)
    uptime_seconds: float | None = Field(default=None, ge=0)
    metrics: dict[str, Any] = Field(default_factory=dict)

@app.get("/api/v1/health")
async def health():
    # Intentionally public: useful for health checks.
    return {
        "status": "ok",
        "time": utc_now(),
    }

@app.post(
    "/api/v1/telemetry",
    dependencies=[Depends(require_authentication)],
)
async def receive_telemetry(data: Telemetry):
    client_id = str(data.client_id)
    hostname = data.hostname.strip().rstrip(".").lower()

    if not hostname:
        raise HTTPException(
            status_code=422,
            detail="Hostname cannot be empty",
        )

    now = utc_now()

    async with hosts_lock:
        previous = hosts.get(client_id, {})

        hosts[client_id] = {
            "client_id": client_id,
            "hostname": hostname,
            "platform": data.platform,
            "architecture": data.architecture,
            "uptime_seconds": data.uptime_seconds,
            "metrics": data.metrics,
            "first_seen": previous.get("first_seen", now),
            "last_seen": now,
        }

    return {
        "accepted": True,
        "client_id": client_id,
        "hostname": hostname,
    }

@app.get(
    "/api/v1/hosts",
    dependencies=[Depends(require_authentication)],
)
async def get_hosts():
    now = utc_now()

    async with hosts_lock:
        snapshot = list(hosts.values())

    results = []

    for host in snapshot:
        age = (now - host["last_seen"]).total_seconds()

        results.append({
            **host,
            "status": (
                "online"
                if age <= HEARTBEAT_TIMEOUT
                else "offline"
            ),
            "seconds_since_seen": round(age, 1),
        })

    return {
        "count": len(results),
        "hosts": sorted(
            results,
            key=lambda host: host["hostname"],
        ),
    }

@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(
        Path(__file__).parent / "dashboard.html"
    )
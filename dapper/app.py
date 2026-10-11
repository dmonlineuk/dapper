import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

HEARTBEAT_TIMEOUT = int(
    os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "90")
)

# Latest known state, keyed by normalized hostname.
hosts: dict[str, dict[str, Any]] = {}
hosts_lock = asyncio.Lock()

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

class Telemetry(BaseModel):
    hostname: str = Field(min_length=1, max_length=253)
    platform: str = Field(min_length=1, max_length=64)
    architecture: str = Field(min_length=1, max_length=64)
    uptime_seconds: float | None = Field(default=None, ge=0)
    metrics: dict[str, Any] = Field(default_factory=dict)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # In-memory state is discarded when the process exits.

app = FastAPI(
    title="Dapper API",
    version="0.1.0",
    lifespan=lifespan,
)

@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "time": utc_now()}

@app.post("/api/v1/telemetry")
async def receive_telemetry(data: Telemetry):
    hostname = data.hostname.strip().rstrip(".").lower()

    if not hostname:
        raise HTTPException(
            status_code=422, detail="Hostname cannot be empty"
        )

    now = utc_now()

    async with hosts_lock:
        previous = hosts.get(hostname, {})

        hosts[hostname] = {
            "hostname": hostname,
            "platform": data.platform,
            "architecture": data.architecture,
            "uptime_seconds": data.uptime_seconds,
            "metrics": data.metrics,
            "first_seen": previous.get("first_seen", now),
            "last_seen": now,
        }

    return {"accepted": True, "hostname": hostname}

@app.get("/api/v1/hosts")
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
            results, key=lambda host: host["hostname"]
        ),
    }
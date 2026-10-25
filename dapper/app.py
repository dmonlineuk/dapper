import asyncio
import os
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
HEARTBEAT_TIMEOUT = int(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "90"))
DEFAULT_DASHBOARD_REFRESH_SECONDS = 10
MIN_DASHBOARD_REFRESH_SECONDS = 1
MAX_DASHBOARD_REFRESH_SECONDS = 60


def get_dashboard_refresh_seconds() -> int:
    raw_value = os.getenv(
        "DAPPER_DASHBOARD_REFRESH_SECONDS",
        str(DEFAULT_DASHBOARD_REFRESH_SECONDS),
    )
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            "DAPPER_DASHBOARD_REFRESH_SECONDS must be an integer "
            "between 1 and 60."
        ) from exc
    if not MIN_DASHBOARD_REFRESH_SECONDS <= value <= MAX_DASHBOARD_REFRESH_SECONDS:
        raise RuntimeError(
            "DAPPER_DASHBOARD_REFRESH_SECONDS must be between 1 and 60."
        )
    return value


DASHBOARD_REFRESH_SECONDS = get_dashboard_refresh_seconds()

# The API token is loaded at startup, not hard-coded.
API_TOKEN: str | None = None

# In-memory state, keyed by persistent client UUID.
hosts: dict[str, dict[str, Any]] = {}
hosts_lock = asyncio.Lock()

# Mutable dashboard configuration is held in process memory only.
# Environment configuration supplies the initial refresh interval.
dashboard_refresh_seconds = DASHBOARD_REFRESH_SECONDS
host_interface_preferences: dict[str, dict[str, list[str]]] = {}
config_lock = asyncio.Lock()

# HTTP bearer authentication.
bearer_scheme = HTTPBearer(auto_error=False)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_api_token(token: str | None) -> str:
    """Validate the configured API token."""
    if not token:
        raise RuntimeError("DAPPER_API_TOKEN must be configured.")
    if len(token) < 32:
        raise RuntimeError("DAPPER_API_TOKEN must be at least 32 characters.")
    return token


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configuration before accepting requests."""
    global API_TOKEN
    API_TOKEN = validate_api_token(os.getenv("DAPPER_API_TOKEN"))
    yield
    API_TOKEN = None


app = FastAPI(
    title="Dapper API",
    version="0.4.3",
    lifespan=lifespan,
)


async def require_authentication(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
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
    agent_version: str | None = Field(default=None, min_length=1, max_length=32)
    uptime_seconds: float | None = Field(default=None, ge=0)
    pending_restart: bool | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    network: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)


class InterfacePreferenceUpdate(BaseModel):
    hidden: list[str] | None = None
    order: list[str] | None = None


class ConfigUpdate(BaseModel):
    dashboard_refresh_seconds: int | None = Field(default=None, ge=1, le=60)
    host_interface_preferences: dict[str, InterfacePreferenceUpdate] | None = None


def config_snapshot() -> dict[str, Any]:
    """Return a detached view of the current in-memory configuration."""
    return {
        "dashboard_refresh_seconds": dashboard_refresh_seconds,
        "min_dashboard_refresh_seconds": MIN_DASHBOARD_REFRESH_SECONDS,
        "max_dashboard_refresh_seconds": MAX_DASHBOARD_REFRESH_SECONDS,
        "host_interface_preferences": {
            client_id: {
                "hidden": list(preference.get("hidden", [])),
                "order": list(preference.get("order", [])),
            }
            for client_id, preference in host_interface_preferences.items()
        },
    }


@app.get("/api/v1/health")
async def health():
    # Intentionally public: useful for health checks.
    return {
        "status": "ok",
        "time": utc_now(),
    }


@app.get(
    "/api/v1/config",
    dependencies=[Depends(require_authentication)],
)
async def get_config():
    async with config_lock:
        return config_snapshot()


@app.patch(
    "/api/v1/config",
    dependencies=[Depends(require_authentication)],
)
async def patch_config(update: ConfigUpdate):
    """Apply validated partial updates to in-memory dashboard configuration."""
    global dashboard_refresh_seconds

    update_data = update.dict(exclude_unset=True)
    preference_updates = update_data.get("host_interface_preferences")
    validated_updates: dict[str, dict[str, list[str] | None]] = {}

    if preference_updates is not None:
        for supplied_client_id, preference_update in preference_updates.items():
            try:
                client_id = str(UUID(supplied_client_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid client ID: {supplied_client_id}",
                ) from exc

            async with hosts_lock:
                host = hosts.get(client_id)
                if host is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Unknown host: {client_id}",
                    )
                network = host.get("network") or {}
                interfaces = network.get("interfaces") or []
                known_names = {
                    item.get("name")
                    for item in interfaces
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                }

            fields = preference_update if isinstance(preference_update, dict) else preference_update.dict(exclude_unset=True)
            for field_name in ("hidden", "order"):
                names = fields.get(field_name)
                if names is None:
                    continue
                if any(not isinstance(name, str) or not name for name in names):
                    raise HTTPException(
                        status_code=422,
                        detail=f"{field_name} must contain non-empty interface names",
                    )
                if len(names) != len(set(names)):
                    raise HTTPException(
                        status_code=422,
                        detail=f"{field_name} must not contain duplicate interface names",
                    )
                unknown_names = sorted(set(names) - known_names)
                if unknown_names:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Unknown interface name(s) for {client_id}: "
                            + ", ".join(unknown_names)
                        ),
                    )
            validated_updates[client_id] = fields

    async with config_lock:
        new_refresh = update_data.get("dashboard_refresh_seconds")
        if new_refresh is not None:
            dashboard_refresh_seconds = new_refresh

        for client_id, fields in validated_updates.items():
            existing = host_interface_preferences.get(
                client_id,
                {"hidden": [], "order": []},
            )
            merged = {
                "hidden": list(existing.get("hidden", [])),
                "order": list(existing.get("order", [])),
            }
            for field_name in ("hidden", "order"):
                if field_name in fields and fields[field_name] is not None:
                    merged[field_name] = list(fields[field_name])

            if not merged["hidden"] and not merged["order"]:
                host_interface_preferences.pop(client_id, None)
            else:
                host_interface_preferences[client_id] = merged

        return config_snapshot()


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
            "agent_version": data.agent_version,
            "uptime_seconds": data.uptime_seconds,
            "pending_restart": data.pending_restart,
            "metrics": data.metrics,
            "network": data.network,
            "storage": data.storage,
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
            "status": "online" if age <= HEARTBEAT_TIMEOUT else "offline",
            "seconds_since_seen": round(age, 1),
        })
    return {
        "count": len(results),
        "server_version": app.version,
        "hosts": sorted(results, key=lambda host: host["hostname"]),
    }


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(Path(__file__).parent / "dashboard.html")

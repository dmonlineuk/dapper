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
host_storage_preferences: dict[str, dict[str, list[str]]] = {}
host_display_names: dict[str, str] = {}
config_lock = asyncio.Lock()
NETWORK_DETAIL_FIELDS = {
    "link_speed", "ipv4", "ipv6", "mac_address", "mtu", "duplex",
    "traffic", "packets", "errors", "drops",
}

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
    version="0.4.6",
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
    card_visible: list[str] | None = None
    detail_fields: list[str] | None = None


class StoragePreferenceUpdate(BaseModel):
    card_visible: list[str] | None = None
    order: list[str] | None = None


class ConfigUpdate(BaseModel):
    dashboard_refresh_seconds: int | None = Field(default=None, ge=1, le=60)
    host_interface_preferences: dict[str, InterfacePreferenceUpdate] | None = None
    host_storage_preferences: dict[str, StoragePreferenceUpdate] | None = None
    host_display_names: dict[str, str] | None = None


def config_snapshot() -> dict[str, Any]:
    """Return a detached view of the current in-memory configuration."""
    return {
        "dashboard_refresh_seconds": dashboard_refresh_seconds,
        "min_dashboard_refresh_seconds": MIN_DASHBOARD_REFRESH_SECONDS,
        "max_dashboard_refresh_seconds": MAX_DASHBOARD_REFRESH_SECONDS,
        "host_interface_preferences": {
            client_id: {key: list(values) for key, values in preference.items()}
            for client_id, preference in host_interface_preferences.items()
        },
        "host_storage_preferences": {
            client_id: {key: list(values) for key, values in preference.items()}
            for client_id, preference in host_storage_preferences.items()
        },
        "host_display_names": dict(host_display_names),
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
    preference_updates = update_data.get("host_interface_preferences") or {}
    storage_updates = update_data.get("host_storage_preferences") or {}
    display_name_updates = update_data.get("host_display_names") or {}
    validated_updates: dict[str, dict[str, list[str] | None]] = {}
    validated_storage: dict[str, dict[str, list[str] | None]] = {}
    validated_display_names: dict[str, str] = {}

    for supplied_client_id, display_name in display_name_updates.items():
        try:
            client_id = str(UUID(supplied_client_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid client ID: {supplied_client_id}") from exc
        if not isinstance(display_name, str) or len(display_name.strip()) > 64:
            raise HTTPException(status_code=422, detail="Display names must be at most 64 characters")
        async with hosts_lock:
            if client_id not in hosts:
                raise HTTPException(status_code=404, detail=f"Unknown host: {client_id}")
        validated_display_names[client_id] = display_name.strip()

    for supplied_client_id, preference_update in preference_updates.items():
        try:
            client_id = str(UUID(supplied_client_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid client ID: {supplied_client_id}") from exc

        async with hosts_lock:
            host = hosts.get(client_id)
            if host is None:
                raise HTTPException(status_code=404, detail=f"Unknown host: {client_id}")
            interfaces = (host.get("network") or {}).get("interfaces") or []
            known_names = {
                item.get("name") for item in interfaces
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }

        fields = (
            preference_update
            if isinstance(preference_update, dict)
            else preference_update.dict(exclude_unset=True)
        )
        for field_name in ("hidden", "order", "card_visible"):
            names = fields.get(field_name)
            if names is None:
                continue
            if any(not isinstance(name, str) or not name for name in names):
                raise HTTPException(status_code=422, detail=f"{field_name} must contain non-empty interface names")
            if len(names) != len(set(names)):
                raise HTTPException(status_code=422, detail=f"{field_name} must not contain duplicate interface names")
            unknown_names = sorted(set(names) - known_names)
            if unknown_names:
                raise HTTPException(status_code=422, detail=f"Unknown interface name(s) for {client_id}: " + ", ".join(unknown_names))
        detail_fields = fields.get("detail_fields")
        if detail_fields is not None:
            if len(detail_fields) != len(set(detail_fields)):
                raise HTTPException(status_code=422, detail="detail_fields must not contain duplicates")
            unknown_fields = sorted(set(detail_fields) - NETWORK_DETAIL_FIELDS)
            if unknown_fields:
                raise HTTPException(status_code=422, detail="Unsupported network detail field(s): " + ", ".join(unknown_fields))
        validated_updates[client_id] = fields

    for supplied_client_id, preference_update in storage_updates.items():
        try:
            client_id = str(UUID(supplied_client_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid client ID: {supplied_client_id}") from exc

        async with hosts_lock:
            host = hosts.get(client_id)
            if host is None:
                raise HTTPException(status_code=404, detail=f"Unknown host: {client_id}")
            drives = (host.get("storage") or {}).get("drives") or []
            known_mountpoints = {
                item.get("mountpoint") for item in drives
                if isinstance(item, dict) and isinstance(item.get("mountpoint"), str)
            }

        fields = (
            preference_update
            if isinstance(preference_update, dict)
            else preference_update.dict(exclude_unset=True)
        )
        for field_name in ("card_visible", "order"):
            names = fields.get(field_name)
            if names is None:
                continue
            if any(not isinstance(name, str) or not name for name in names):
                raise HTTPException(status_code=422, detail=f"{field_name} must contain non-empty drive mount points")
            if len(names) != len(set(names)):
                raise HTTPException(status_code=422, detail=f"{field_name} must not contain duplicate drive mount points")
            unknown_names = sorted(set(names) - known_mountpoints)
            if unknown_names:
                raise HTTPException(status_code=422, detail=f"Unknown drive mount point(s) for {client_id}: " + ", ".join(unknown_names))
        validated_storage[client_id] = fields

    async with config_lock:
        new_refresh = update_data.get("dashboard_refresh_seconds")
        if new_refresh is not None:
            dashboard_refresh_seconds = new_refresh

        for client_id, fields in validated_updates.items():
            merged = {
                key: list(values)
                for key, values in host_interface_preferences.get(client_id, {}).items()
            }
            for field_name in ("hidden", "order", "card_visible", "detail_fields"):
                if field_name in fields and fields[field_name] is not None:
                    merged[field_name] = list(fields[field_name])

            if not merged:
                host_interface_preferences.pop(client_id, None)
            else:
                host_interface_preferences[client_id] = merged

        for client_id, fields in validated_storage.items():
            merged = {
                key: list(values)
                for key, values in host_storage_preferences.get(client_id, {}).items()
            }
            for field_name in ("card_visible", "order"):
                if field_name in fields and fields[field_name] is not None:
                    merged[field_name] = list(fields[field_name])

            if not merged:
                host_storage_preferences.pop(client_id, None)
            else:
                host_storage_preferences[client_id] = merged

        for client_id, display_name in validated_display_names.items():
            if display_name:
                host_display_names[client_id] = display_name
            else:
                host_display_names.pop(client_id, None)

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

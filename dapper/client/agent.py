import json
import logging
import os
import platform
import signal
import socket
import sys
import time

from pathlib import Path
from threading import Event

import psutil
import requests


VERSION = "0.1.0"
DEFAULT_INTERVAL = 30
REQUEST_TIMEOUT = 10

stop_event = Event()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger("dapper-agent")


def default_config_path() -> Path:
    """Return a sensible configuration path for this OS."""

    if sys.platform == "win32":
        return Path(os.environ["PROGRAMDATA"]) / "Dapper" / "client.json"

    if sys.platform == "darwin":
        return (
            Path("/Library/Application Support/Dapper")
            / "client.json"
        )

    return Path("/etc/dapper/client.json")


def load_config() -> dict:
    """Load configuration without exposing credentials."""

    config_path = Path(
        os.environ.get(
            "DAPPER_CONFIG",
            str(default_config_path()),
        )
    )

    try:
        with config_path.open(encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError):
        logger.error("Unable to read Dapper configuration.")
        raise SystemExit(1)

    server_url = config.get("server_url")
    api_token = config.get("api_token")
    interval = config.get("interval_seconds", DEFAULT_INTERVAL)

    if not isinstance(server_url, str) or not server_url.strip():
        logger.error("Configuration requires server_url.")
        raise SystemExit(1)

    if not isinstance(api_token, str) or len(api_token) < 32:
        logger.error("Configuration requires a valid API token.")
        raise SystemExit(1)

    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not 5 <= interval <= 3600
    ):
        logger.error(
            "interval_seconds must be between 5 and 3600."
        )
        raise SystemExit(1)

    server_url = server_url.strip().rstrip("/")

    if not server_url.startswith(("http://", "https://")):
        logger.error("server_url must use HTTP or HTTPS.")
        raise SystemExit(1)

    # Refuse credentials embedded in URLs.
    from urllib.parse import urlsplit

    parsed_url = urlsplit(server_url)

    if parsed_url.username or parsed_url.password:
        logger.error("Credentials must not be embedded in server_url.")
        raise SystemExit(1)

    return {
        "server_url": server_url,
        "api_token": api_token,
        "interval_seconds": interval,
    }


def collect_telemetry() -> dict:
    """Collect a small set of cross-platform system metrics."""

    memory = psutil.virtual_memory()

    return {
        "hostname": socket.gethostname(),
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "uptime_seconds": max(
            0,
            int(time.time() - psutil.boot_time()),
        ),
        "metrics": {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": memory.percent,
            "memory_total_bytes": memory.total,
            "memory_available_bytes": memory.available,
        },
    }


def send_telemetry(
    session: requests.Session,
    config: dict,
) -> None:
    """Send telemetry to Dapper."""

    url = f"{config['server_url']}/api/v1/telemetry"

    payload = collect_telemetry()

    response = session.post(
        url,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 401:
        logger.error(
            "Dapper rejected authentication. Check the API token."
        )
        return

    response.raise_for_status()

    logger.debug(
        "Telemetry accepted for %s.",
        payload["hostname"],
    )


def handle_signal(signum, frame):
    logger.info("Shutdown requested.")
    stop_event.set()


def main():
    config = load_config()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    with requests.Session() as session:
        session.headers.update({
            "Authorization": f"Bearer {config['api_token']}",
            "Content-Type": "application/json",
            "User-Agent": f"Dapper-Agent/{VERSION}",
        })

        logger.info("Dapper agent %s starting.", VERSION)

        # Prime CPU measurements so the first report is meaningful.
        psutil.cpu_percent(interval=None)

        while not stop_event.is_set():
            started = time.monotonic()

            try:
                send_telemetry(session, config)

            except requests.Timeout:
                logger.warning("Dapper request timed out.")

            except requests.ConnectionError:
                logger.warning("Unable to connect to Dapper.")

            except requests.HTTPError as exc:
                logger.warning(
                    "Dapper returned HTTP %s.",
                    exc.response.status_code
                    if exc.response is not None
                    else "unknown",
                )

            except Exception:
                # Record unexpected errors without logging credentials
                # or the HTTP request itself.
                logger.exception("Unexpected agent error.")

            elapsed = time.monotonic() - started
            remaining = max(
                0,
                config["interval_seconds"] - elapsed,
            )

            stop_event.wait(remaining)

    logger.info("Dapper agent stopped.")


if __name__ == "__main__":
    main()
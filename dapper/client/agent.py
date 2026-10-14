
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tempfile

from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psutil
import requests

from functools import lru_cache

VERSION = "0.4.0"
DEFAULT_INTERVAL = 30
REQUEST_TIMEOUT = 10
UPDATE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
UPDATE_CHECK_TIMEOUT_SECONDS = 45

update_status_lock = Lock()
update_status_cache = {
    "available_count": None,
    "security_available": None,
    "status": "pending",
    "checked_at": None,
}
next_update_check = 0.0

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

def save_config(config_path: Path, config: dict) -> None:
    """Atomically save configuration, including the persistent client ID."""

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None

    try:
        # Create the temporary file in the same directory so os.replace()
        # can replace the destination atomically on the same filesystem.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_path = Path(file.name)
            json.dump(config, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_path, config_path)

    except OSError:
        logger.error("Unable to save Dapper configuration.")
        raise SystemExit(1)

    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

def load_config() -> dict:
    """Load configuration and establish a persistent client identity."""

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

    if not isinstance(config, dict):
        logger.error("Dapper configuration must be a JSON object.")
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
        or not 1 <= interval <= 3600
    ):
        logger.error(
            "interval_seconds must be between 1 and 3600."
        )
        raise SystemExit(1)

    server_url = server_url.strip().rstrip("/")

    if not server_url.startswith(("http://", "https://")):
        logger.error("server_url must use HTTP or HTTPS.")
        raise SystemExit(1)

    # Refuse credentials embedded in URLs.
    parsed_url = urlsplit(server_url)

    if parsed_url.username or parsed_url.password:
        logger.error("Credentials must not be embedded in server_url.")
        raise SystemExit(1)

    # Create an identity only when one has never been assigned.
    # An existing but invalid ID is an error, not a reason to silently
    # assign a new identity and potentially duplicate this host.
    client_id = config.get("client_id")

    if client_id is None:
        client_id = str(uuid4())
        config["client_id"] = client_id
        save_config(config_path, config)
        logger.info("Created a persistent Dapper client identity.")

    else:
        try:
            client_id = str(UUID(client_id))
        except (ValueError, TypeError, AttributeError):
            logger.error(
                "Configuration contains an invalid client_id UUID."
            )
            raise SystemExit(1)

        # Normalise UUID formatting while keeping the identity unchanged.
        if config["client_id"] != client_id:
            config["client_id"] = client_id
            save_config(config_path, config)

    return {
        **config,
        "client_id": client_id,
        "server_url": server_url,
        "api_token": api_token,
        "interval_seconds": interval,
    }

@lru_cache(maxsize=1)
def macos_hardware_port_types() -> dict[str, str]:
    """Map macOS interface names to best-effort connection types."""

    if sys.platform != "darwin":
        return {}

    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    if result.returncode != 0:
        return {}

    mappings = {}

    for block in re.split(r"\n\s*\n", result.stdout.strip()):
        port_match = re.search(
            r"^Hardware Port:\s*(.+)$",
            block,
            re.MULTILINE,
        )
        device_match = re.search(
            r"^Device:\s*(.+)$",
            block,
            re.MULTILINE,
        )

        if not port_match or not device_match:
            continue

        port = port_match.group(1).strip().lower()
        device = device_match.group(1).strip()

        if any(word in port for word in ("wi-fi", "wifi", "airport", "wireless")):
            mappings[device] = "wifi"
        elif "ethernet" in port:
            mappings[device] = "ethernet"

    return mappings


def classify_interface(name: str) -> str:
    """Classify an interface using its name and available OS metadata."""

    lower_name = name.lower()

    if lower_name in ("lo", "lo0", "loopback"):
        return "loopback"

    if any(
        marker in lower_name
        for marker in ("vpn", "tun", "tap", "utun", "wireguard", "wg")
    ):
        return "vpn"

    if any(
        marker in lower_name
        for marker in (
            "docker", "veth", "virbr", "vmnet",
            "vbox", "br-", "bridge",
        )
    ):
        return "virtual"

    # Common wireless interface names.
    if any(
        marker in lower_name
        for marker in ("wifi", "wi-fi", "wlan", "wireless", "airport")
    ):
        return "wifi"

    # Common Ethernet interface names.
    if lower_name.startswith(("eth", "enp", "eno", "ens", "enx")):
        return "ethernet"

    # macOS commonly uses en0, en1, etc. Consult the hardware-port
    # mapping rather than assuming every "en" interface is Ethernet.
    if sys.platform == "darwin":
        return macos_hardware_port_types().get(name, "unknown")

    # Linux exposes wireless interfaces through sysfs.
    if sys.platform.startswith("linux"):
        interface_path = Path("/sys/class/net") / name

        if (interface_path / "wireless").exists():
            return "wifi"

        if (interface_path / "device").exists():
            return "ethernet"

    # Avoid guessing when the available information is insufficient.
    return "unknown"


def collect_network_telemetry() -> dict:
    """Collect best-effort network details for each interface."""

    try:
        addresses_by_interface = psutil.net_if_addrs()
        stats_by_interface = psutil.net_if_stats()
        counters_by_interface = psutil.net_io_counters(pernic=True)
    except (OSError, psutil.Error):
        logger.warning("Unable to collect network interface telemetry.")
        return {"interfaces": []}

    interfaces = []

    link_families = {
        family
        for family in (
            getattr(psutil, "AF_LINK", None),
            getattr(socket, "AF_PACKET", None),
        )
        if family is not None
    }

    names = sorted(
        set(addresses_by_interface)
        | set(stats_by_interface)
        | set(counters_by_interface)
    )

    for name in names:
        addresses = addresses_by_interface.get(name, [])
        stats = stats_by_interface.get(name)
        counters = counters_by_interface.get(name)

        ipv4_addresses = []
        ipv6_addresses = []
        mac_address = None

        for address in addresses:
            value = address.address

            if not value:
                continue

            if address.family == socket.AF_INET:
                ipv4_addresses.append(value)

            elif address.family == socket.AF_INET6:
                # Keep any IPv6 scope identifier supplied by the OS.
                ipv6_addresses.append(value)

            elif address.family in link_families:
                # Ignore empty and all-zero link-layer addresses.
                candidate = value.strip()

                if candidate and any(
                    character in candidate for character in (":", "-")
                ):
                    compact = candidate.replace(":", "").replace("-", "")

                    if compact and any(
                        character != "0" for character in compact
                    ):
                        mac_address = candidate.upper()

        speed = stats.speed if stats else 0

        duplex = None
        if stats:
            if stats.duplex == psutil.NIC_DUPLEX_FULL:
                duplex = "full"
            elif stats.duplex == psutil.NIC_DUPLEX_HALF:
                duplex = "half"

        interface = {
            "name": name,
            "type": classify_interface(name),
            "is_up": stats.isup if stats else None,
            "mac_address": mac_address,
            "ipv4_addresses": sorted(set(ipv4_addresses)),
            "ipv6_addresses": sorted(set(ipv6_addresses)),
            "link_speed_mbps": speed if speed and speed > 0 else None,
            "mtu": stats.mtu if stats and stats.mtu > 0 else None,
            "duplex": duplex,
            "bytes_sent": counters.bytes_sent if counters else None,
            "bytes_received": counters.bytes_recv if counters else None,
            "packets_sent": counters.packets_sent if counters else None,
            "packets_received": counters.packets_recv if counters else None,
            "errors_in": counters.errin if counters else None,
            "errors_out": counters.errout if counters else None,
            "drops_in": counters.dropin if counters else None,
            "drops_out": counters.dropout if counters else None,
        }

        interfaces.append(interface)

    return {"interfaces": interfaces}

def run_update_command(command: list[str], timeout: int = UPDATE_CHECK_TIMEOUT_SECONDS):
    """Run a read-only update query without exposing its output to telemetry."""

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Update query failed.")
    return result.stdout


def apt_security_update_status(package_names: set[str]):
    """Classify pending APT upgrades by the repository supplying the candidate."""

    if not package_names:
        return False
    if not shutil.which("apt-cache"):
        return None

    try:
        output = run_update_command(
            ["apt-cache", "policy", *sorted(package_names)],
            timeout=UPDATE_CHECK_TIMEOUT_SECONDS,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return None

    package_blocks = re.split(r"(?m)^Package:\s*", output)[1:]
    if not package_blocks:
        return None

    classified = 0
    security_available = False
    for block in package_blocks:
        candidate_match = re.search(r"(?m)^\s*Candidate:\s*(\S+)", block)
        if not candidate_match or candidate_match.group(1) == "(none)":
            continue

        candidate = candidate_match.group(1)
        version_lines = list(
            re.finditer(r"(?m)^\s+(?:\*{3}\s+)?(\S+)\s+\d+\s*$", block)
        )
        for index, version_match in enumerate(version_lines):
            if version_match.group(1) != candidate:
                continue
            end = (
                version_lines[index + 1].start()
                if index + 1 < len(version_lines)
                else len(block)
            )
            candidate_sources = block[version_match.end():end]
            classified += 1
            if re.search(r"security", candidate_sources, flags=re.IGNORECASE):
                security_available = True
            break

    if classified != len(package_names):
        return None
    return security_available

def check_os_updates() -> dict:
    """Return update counts only; package names remain local to this check."""

    if sys.platform == "win32":
        script = r"""
$ErrorActionPreference = 'Stop'
$session = New-Object -ComObject Microsoft.Update.Session
$searcher = $session.CreateUpdateSearcher()
$result = $searcher.Search("IsInstalled=0 and IsHidden=0 and Type='Software'")
$securityAvailable = $false
for ($index = 0; $index -lt $result.Updates.Count; $index++) {
    $update = $result.Updates.Item($index)
    if (-not [string]::IsNullOrWhiteSpace([string]$update.MsrcSeverity)) {
        $securityAvailable = $true
    }
}
[PSCustomObject]@{
    available_count = $result.Updates.Count
    security_available = $securityAvailable
} | ConvertTo-Json -Compress
"""
        output = run_update_command(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            timeout=UPDATE_CHECK_TIMEOUT_SECONDS,
        )
        result = json.loads(output)
        if not isinstance(result, dict):
            raise RuntimeError("Windows Update returned an unexpected result.")
        count = result.get("available_count")
        security_available = result.get("security_available")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or not isinstance(security_available, bool)
        ):
            raise RuntimeError("Windows Update returned an unexpected result.")
        return {
            "available_count": count,
            "security_available": security_available,
            "status": "checked",
        }

    if sys.platform == "darwin":
        if not shutil.which("softwareupdate"):
            return {"available_count": None, "security_available": None, "status": "unsupported"}
        output = run_update_command(["softwareupdate", "--list"])
        labels = re.findall(
            r"^\s*\*\s+(?:Label|Name):\s*.+$",
            output,
            flags=re.MULTILINE,
        )
        if not labels and not re.search(
            r"no (?:new )?(?:software|updates) available",
            output,
            flags=re.IGNORECASE,
        ):
            raise RuntimeError("macOS update output could not be interpreted.")
        return {
            "available_count": len(labels),
            "security_available": False if not labels else None,
            "status": "checked",
        }

    if sys.platform.startswith("linux"):
        if shutil.which("dnf"):
            packages = run_update_command(
                ["dnf", "repoquery", "--upgrades", "--qf", "%{name}"]
            )
            package_names = {line.strip() for line in packages.splitlines() if line.strip()}
            try:
                security_packages = run_update_command(
                    ["dnf", "repoquery", "--upgrades", "--security", "--qf", "%{name}"]
                )
                security_available = any(line.strip() for line in security_packages.splitlines())
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                security_available = None
            return {
                "available_count": len(package_names),
                "security_available": security_available,
                "status": "checked",
            }

        if shutil.which("apt-get"):
            output = run_update_command(["apt-get", "-s", "-q", "upgrade"])
            package_names = {
                match.group(1)
                for match in re.finditer(r"^Inst\s+(\S+)", output, flags=re.MULTILINE)
            }
            return {
                "available_count": len(package_names),
                "security_available": apt_security_update_status(package_names),
                "status": "checked",
            }

        if shutil.which("pacman"):
            output = run_update_command(["pacman", "-Qu"])
            package_names = {
                line.split()[0]
                for line in output.splitlines()
                if line.split()
            }
            return {
                "available_count": len(package_names),
                "security_available": False if not package_names else None,
                "status": "checked",
            }

    return {"available_count": None, "security_available": None, "status": "unsupported"}

def refresh_os_update_status() -> None:
    """Refresh cached update status away from the telemetry request path."""

    try:
        result = check_os_updates()
    except Exception:
        # Keep update-query failures out of the telemetry request path and
        # never include command output or package names in reported status.
        result = {
            "available_count": None,
            "security_available": None,
            "status": "error",
        }

    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    with update_status_lock:
        update_status_cache.update(result)

def get_os_update_status() -> dict:
    """Return cached results and schedule an infrequent background check."""

    global next_update_check
    now = time.monotonic()
    start_check = False
    with update_status_lock:
        if now >= next_update_check:
            next_update_check = now + UPDATE_CHECK_INTERVAL_SECONDS
            update_status_cache["status"] = "checking"
            start_check = True
        status = dict(update_status_cache)

    if start_check:
        Thread(target=refresh_os_update_status, daemon=True).start()
    return status

def collect_telemetry(client_id: str) -> dict:
    """Collect system metrics and per-interface network telemetry."""

    memory = psutil.virtual_memory()
    physical_cpu_count = psutil.cpu_count(logical=False)
    logical_cpu_count = psutil.cpu_count(logical=True)
    per_cpu_percent = psutil.cpu_percent(interval=None, percpu=True)

    return {
        "client_id": client_id,
        "hostname": socket.gethostname(),
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "agent_version": VERSION,
        "uptime_seconds": max(
            0,
            int(time.time() - psutil.boot_time()),
        ),
        "metrics": {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "cpu_core_count": physical_cpu_count,
            "cpu_logical_count": logical_cpu_count,
            "cpu_per_core_percent": per_cpu_percent,
            "memory_percent": memory.percent,
            "memory_total_bytes": memory.total,
            "memory_available_bytes": memory.available,
            "os_updates": get_os_update_status(),
        },
        "network": collect_network_telemetry(),
    }

def send_telemetry(
    session: requests.Session,
    config: dict,
) -> None:
    """Send telemetry to Dapper."""

    url = f"{config['server_url']}/api/v1/telemetry"

    payload = collect_telemetry(config["client_id"])

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
        "Telemetry accepted for %s (%s).",
        payload["hostname"],
        payload["client_id"],
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
        psutil.cpu_percent(interval=None, percpu=True)

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

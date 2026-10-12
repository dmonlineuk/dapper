# Dapper Client

The Dapper client is a lightweight, cross-platform agent that collects
system telemetry and reports it to a Dapper server.

The agent is written in Python and currently supports:

- Linux
- Windows
- macOS

It collects the hostname, operating system, CPU architecture, uptime,
CPU utilisation, and memory statistics.

The agent sends telemetry to the server periodically using an HTTP API
and a shared bearer token.

## Requirements

- Python 3.12 or later (use a version tested with your dependencies).
- Network connectivity to a running Dapper server.
- The shared Dapper API token.
- Administrator/root privileges for service installation.

The agent requires the following Python packages:

- `psutil`
- `requests`

Install them using the included `requirements.txt`.

## Configuration

All clients use a JSON configuration file containing the server address,
API token, and reporting interval.

Example:

```json
{
  "server_url": "http://<servername-or-ip>:8000",
  "api_token": "REPLACE_WITH_YOUR_DAPPER_API_TOKEN",
  "interval_seconds": 30
}
```

Configuration properties:

| Property | Description | Default |
|---|---|---|
| `server_url` | Base URL of the Dapper server | Required |
| `api_token` | Shared Dapper API bearer token | Required |
| `interval_seconds` | Time between telemetry reports, in seconds | `30` |

The reporting interval must be between 5 and 3600 seconds.

The API token must be at least 32 characters long and must match the
token configured on the Dapper server.

The agent supports the `DAPPER_CONFIG` environment variable to override
the default configuration file path.

Default configuration paths:

| Operating system | Configuration path |
|---|---|
| Linux | `/etc/dapper/client.json` |
| Windows | `C:\ProgramData\Dapper\client.json` |
| macOS | `/Library/Application Support/Dapper/client.json` |

Protect the configuration file against unauthorised access. It contains
the shared API token, which grants access to the Dapper API.

## Linux

The recommended Linux deployment uses systemd to run the agent as a
system service.

### Initial setup

Run the following commands from the `client` directory of the Dapper
repository.

Create the installation directories:

```bash
sudo mkdir -p /opt/dapper
sudo mkdir -p /etc/dapper
```

Copy the agent and its requirements:

```bash
sudo install -m 644 agent.py /opt/dapper/agent.py
sudo install -m 644 requirements.txt /opt/dapper/requirements.txt
```

Create a virtual environment and install the dependencies:

```bash
sudo python3 -m venv /opt/dapper/venv

sudo /opt/dapper/venv/bin/python -m pip install \
  -r /opt/dapper/requirements.txt
```

### Create the service account

The agent does not need root privileges to collect the metrics currently
supported by Dapper.

Create a dedicated service account:

```bash
sudo useradd \
  --system \
  --no-create-home \
  --shell /usr/sbin/nologin \
  dapper
```

If the account already exists, this command can be skipped.

Set ownership and permissions:

```bash
sudo chown -R root:root /opt/dapper
sudo chmod -R a+rX /opt/dapper

sudo chown root:dapper /etc/dapper
sudo chmod 750 /etc/dapper
```

### Configure the client

Create `/etc/dapper/client.json`:

```bash
sudo nano /etc/dapper/client.json
```

Add the configuration described in the Configuration section.

Protect the file:

```bash
sudo chown root:dapper /etc/dapper/client.json
sudo chmod 640 /etc/dapper/client.json
```

The configuration is readable by root and the Dapper service group,
but not by ordinary users.

### Test the agent

Before installing the service, run the agent manually:

```bash
sudo -u dapper /opt/dapper/venv/bin/python \
  /opt/dapper/agent.py
```

Check that the agent starts and reports telemetry successfully.

Stop it with `Ctrl+C`.

### Configure systemd

Create the service unit:

```bash
sudo nano /etc/systemd/system/dapper-agent.service
```

Add the following:

```ini
[Unit]
Description=Dapper Network Monitoring Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple

User=dapper
Group=dapper

ExecStart=/opt/dapper/venv/bin/python /opt/dapper/agent.py

Restart=on-failure
RestartSec=10

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

### Start the service

Reload systemd and enable the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dapper-agent
```

Check its status:

```bash
sudo systemctl status dapper-agent
```

Follow the logs:

```bash
sudo journalctl -u dapper-agent -f
```

### Restart or stop the service

Restart:

```bash
sudo systemctl restart dapper-agent
```

Stop:

```bash
sudo systemctl stop dapper-agent
```

Start:

```bash
sudo systemctl start dapper-agent
```

## IPFire (without systemd)

This section describes a deployment for IPFire systems that do not use systemd. It uses a Python virtual environment and, if supported by the installed IPFire version, /etc/sysconfig/rc.local for startup.
Important: Confirm that your IPFire version executes /etc/sysconfig/rc.local during startup before relying on it. Do not replace an existing file or remove its existing commands. You can install and test Dapper without rebooting IPFire.

### Initial setup

Run as root or through a root shell. Confirm Python and virtual-environment support:

```bash
python3 --version
python3 -m venv --help
Create the application and configuration directories:
mkdir -p /opt/dapper
mkdir -p /etc/dapper
From the Dapper client source directory, copy the agent and requirements:
install -m 644 agent.py /opt/dapper/agent.py
install -m 644 requirements.txt /opt/dapper/requirements.txt
Create the virtual environment and install the dependencies:
python3 -m venv /opt/dapper/venv

/opt/dapper/venv/bin/python -m pip install \
  -r /opt/dapper/requirements.txt
```

### Configure the client

Create /etc/dapper/client.json:

```bash
vim /etc/dapper/client.json
Use the configuration format described in the Configuration section. Set server_url, api_token, and interval_seconds for this node. Restrict access to the file because it contains the shared API token:
chmod 600 /etc/dapper/client.json
```

The agent must run as a user that can read this file. Running it as root allows it to read a file with these permissions.

### Test the agent manually

Run the agent in the foreground:

```bash
/opt/dapper/venv/bin/python /opt/dapper/agent.py
```

Confirm that IPFire appears in the dashboard and telemetry updates. Stop the test with Ctrl+C before starting another instance.

### Start the agent without rebooting

You do not need to reboot IPFire just to start the agent. To launch it in the background and append output to a log:

```bash
/opt/dapper/venv/bin/python /opt/dapper/agent.py \
  >> /var/log/dapper-agent.log 2>&1 &
```

Do not run this if an instance is already running, or you may start duplicate agents reporting the same client identity.
Check the process and log:

```bash
ps aux | grep '[a]gent.py'
tail -n 50 /var/log/dapper-agent.log
```

This starts the agent for the current boot; it does not configure automatic startup.

### Configure startup with rc.local

Inspect the file before editing:

```bash
ls -l /etc/sysconfig/rc.local
cat /etc/sysconfig/rc.local
```

If the file exists and is used by your IPFire version, edit it:

```bash
vim /etc/sysconfig/rc.local
```

Preserve existing contents and add this command, keeping any existing shebang at the top:

```bash
/opt/dapper/venv/bin/python /opt/dapper/agent.py \
  >> /var/log/dapper-agent.log 2>&1 &
```

Do not replace other startup commands or add a second shebang. If needed, make the file executable:

```bash
chmod +x /etc/sysconfig/rc.local
```

Making the file executable does not prove IPFire invokes it at boot. Confirm the startup mechanism for your installed IPFire version. Executing the entire rc.local file manually can rerun all its commands, so inspect it first and avoid doing so if it contains operations that should only run during boot.
You can test the Dapper command immediately using the background-start command above, without rebooting. A successful manual launch verifies the command, but only a later boot during a suitable maintenance window can confirm that the startup hook is invoked.

### Troubleshooting

- Check /var/log/dapper-agent.log for Python, configuration, or connection errors.
- Verify that the virtual-environment Python and agent.py exist at the paths above.
- Verify that the agent can read /etc/dapper/client.json.
- Confirm that the configured server URL is reachable from IPFire.
- If the agent works manually but not after boot, verify that your IPFire version invokes /etc/sysconfig/rc.local and inspect startup logs.

## Windows

The recommended Windows deployment uses Python and the WinSW service
wrapper to run the agent as a Windows service.

### Prerequisites

Install Python using winget, if it is not already installed:

```powershell
winget install --id Python.Python.3.13 -e
```

Confirm that Python is available:

```powershell
py -3.13 --version
```

Download the appropriate WinSW executable from the
[WinSW releases page](https://github.com/winsw/winsw/releases).

This guide uses the WinSW v2.12.0 release and assumes a 64-bit Windows
installation. Download `WinSW-x64.exe` and rename it to `DapperAgent.exe`.

### Initial setup

Open an elevated PowerShell terminal.

Run these commands from the Dapper `client` directory.

Create the installation directories:

```powershell
New-Item -ItemType Directory -Force `
  -Path 'C:\Program Files\Dapper'

New-Item -ItemType Directory -Force `
  -Path 'C:\ProgramData\Dapper'
```

Copy the agent and requirements:

```powershell
Copy-Item .\agent.py `
  'C:\Program Files\Dapper\agent.py'

Copy-Item .\requirements.txt `
  'C:\Program Files\Dapper\requirements.txt'
```

Copy the downloaded WinSW executable, adjusting the source path
if necessary:

```powershell
Copy-Item `
  "$env:USERPROFILE\Downloads\WinSW-x64.exe" `
  'C:\Program Files\Dapper\DapperAgent.exe'
```

Create the virtual environment:

```powershell
py -3.13 -m venv `
  'C:\Program Files\Dapper\venv'
```

Install the dependencies:

```powershell
& 'C:\Program Files\Dapper\venv\Scripts\python.exe' `
  -m pip install `
  -r 'C:\Program Files\Dapper\requirements.txt'
```

### Configure the client

Create the configuration file:

```powershell
notepad 'C:\ProgramData\Dapper\client.json'
```

Add the configuration described in the Configuration section.

Restrict access to the configuration file:

```powershell
icacls 'C:\ProgramData\Dapper' /inheritance:r `
  /grant:r '*S-1-5-18:(OI)(CI)F' `
  '*S-1-5-32-544:(OI)(CI)F'
```

These security identifiers represent SYSTEM and the local
Administrators group, respectively.

Ensure the service runs under an account that can read the
configuration file.

### Test the agent

Run the agent manually before installing the service:

```powershell
& 'C:\Program Files\Dapper\venv\Scripts\python.exe' `
  'C:\Program Files\Dapper\agent.py'
```

Check that the agent starts and reports telemetry successfully.

Stop it with `Ctrl+C`.

### Configure WinSW

Create the file:

`C:\Program Files\Dapper\DapperAgent.xml`

The XML filename must match the executable basename.

Add:

```xml
<service>
  <id>DapperAgent</id>
  <name>Dapper Network Monitoring Agent</name>
  <description>Reports system telemetry to Dapper.</description>

  <executable>C:\Program Files\Dapper\venv\Scripts\python.exe</executable>
  <arguments>"C:\Program Files\Dapper\agent.py"</arguments>

  <workingdirectory>C:\Program Files\Dapper</workingdirectory>

  <log mode="roll"></log>
</service>
```

The service uses the Python interpreter in the virtual environment.

The default WinSW service identity is LocalSystem. Keep the configuration
file restricted to SYSTEM and Administrators, as above.

### Install and start the service

Run these commands from an elevated PowerShell terminal:

```powershell
& 'C:\Program Files\Dapper\DapperAgent.exe' install

& 'C:\Program Files\Dapper\DapperAgent.exe' start
```

Check the service status:

```powershell
Get-Service DapperAgent
```

WinSW writes service logs alongside its executable by default, subject
to its logging configuration. Check these logs if the service fails to
start or the agent cannot connect to Dapper.

### Restart or stop the service

Restart:

```powershell
Restart-Service DapperAgent
```

Stop:

```powershell
Stop-Service DapperAgent
```

Start:

```powershell
Start-Service DapperAgent
```

If you modify the WinSW XML configuration, consult the documentation
for your installed WinSW version to determine whether the service needs
to be reinstalled.

## macOS

The recommended macOS deployment uses a Python virtual environment
and a system-level launchd LaunchDaemon.

A LaunchDaemon starts independently of an interactive user session.

### Prerequisites

Install Python using Homebrew, if it is not already installed:

```zsh
brew install python@3.13
```

Verify the installation:

```zsh
python3.13 --version
```

### Initial setup

Run the following commands from the Dapper `client` directory.

Create the installation directory:

```zsh
sudo mkdir -p "/Library/Application Support/Dapper"
```

Copy the agent and requirements:

```zsh
sudo install -m 644 agent.py \
  "/Library/Application Support/Dapper/agent.py"

sudo install -m 644 requirements.txt \
  "/Library/Application Support/Dapper/requirements.txt"
```

Create the virtual environment:

```zsh
sudo python3.13 -m venv \
  "/Library/Application Support/Dapper/venv"
```

Install the dependencies:

```zsh
sudo "/Library/Application Support/Dapper/venv/bin/python" \
  -m pip install \
  -r "/Library/Application Support/Dapper/requirements.txt"
```

### Configure the client

Create the configuration file:

```zsh
sudo nano "/Library/Application Support/Dapper/client.json"
```

Add the configuration described in the Configuration section.

Protect the configuration file:

```zsh
sudo chown root:wheel \
  "/Library/Application Support/Dapper/client.json"

sudo chmod 600 \
  "/Library/Application Support/Dapper/client.json"
```

The LaunchDaemon runs as root and can read this file.

### Test the agent

Run the agent manually:

```zsh
sudo "/Library/Application Support/Dapper/venv/bin/python" \
  "/Library/Application Support/Dapper/agent.py"
```

Check that the agent starts and reports telemetry successfully.

Stop it with `Ctrl+C`.

### Configure launchd

Create the LaunchDaemon plist:

```zsh
sudo nano /Library/LaunchDaemons/com.dapper.agent.plist
```

Add the following:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC
  "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">

<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.dapper.agent</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Library/Application Support/Dapper/venv/bin/python</string>
        <string>/Library/Application Support/Dapper/agent.py</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/var/log/dapper-agent.log</string>

    <key>StandardErrorPath</key>
    <string>/var/log/dapper-agent-error.log</string>
</dict>
</plist>
```

Set ownership and permissions:

```zsh
sudo chown root:wheel \
  /Library/LaunchDaemons/com.dapper.agent.plist

sudo chmod 644 \
  /Library/LaunchDaemons/com.dapper.agent.plist
```

Validate the plist:

```zsh
plutil -lint /Library/LaunchDaemons/com.dapper.agent.plist
```

### Load and start the service

Load the LaunchDaemon:

```zsh
sudo launchctl bootstrap system \
  /Library/LaunchDaemons/com.dapper.agent.plist
```

Check its status:

```zsh
sudo launchctl print system/com.dapper.agent
```

### Restart or stop the service

Restart:

```zsh
sudo launchctl kickstart -k system/com.dapper.agent
```

Stop the service:

```zsh
sudo launchctl bootout system/com.dapper.agent
```

Start it again after stopping:

```zsh
sudo launchctl bootstrap system \
  /Library/LaunchDaemons/com.dapper.agent.plist
```

If you change only `client.json`, restarting the agent is sufficient.

If you modify the plist itself, unload and reload the service so that
launchd applies the updated configuration.

### View logs

Follow standard output:

```zsh
tail -f /var/log/dapper-agent.log
```

Follow standard error:

```zsh
tail -f /var/log/dapper-agent-error.log
```

## Troubleshooting

### The agent cannot connect to Dapper

Check the configured `server_url` and confirm that the client can reach
the server.

From a client machine, test the health endpoint:

```text
http://<servername-or-ip>:8000/api/v1/health
```

The endpoint should return a JSON response indicating that the server
is healthy.

If the connection fails, check the server address, firewall rules,
network routing, and Podman port publishing.

### Authentication fails

Confirm that the `api_token` in `client.json` matches the token
configured on the Dapper server.

The token must be at least 32 characters long.

Do not include the token in support requests or logs.

### The agent runs manually but not as a service

Check that:

- The service points to the correct Python interpreter.
- The virtual environment contains the required dependencies.
- The service can read `client.json`.
- The service has permission to execute the agent.
- The configured server URL is reachable from the service.
- The service logs do not contain configuration or authentication errors.

Remember that an interactive shell and a background service can have
different environment variables and permissions.

### The client does not appear in the dashboard

Confirm that the agent is running and that telemetry is being accepted
by the server.

The dashboard retrieves discovered clients from the Dapper API.
A client will appear after its first successful telemetry report.

### The client appears offline

The server marks clients offline when their last successful report
exceeds the configured heartbeat timeout.

Check the client logs, network connectivity, and server availability.

## Security considerations

Dapper currently uses a shared bearer token.

Every client configured with this token can submit telemetry to the
server. Protect the token and configuration file accordingly.

The example configuration uses HTTP for simplicity. HTTP does not
encrypt the token or telemetry in transit.

Do not expose the API directly to the public internet. For networks
where traffic may be intercepted, configure HTTPS before deploying
the agent across untrusted network segments.

The current agent does not provide individual client credentials or
cryptographic client identities.

## Development

The client source code and its requirements are located in this
directory.

To run the agent manually during development:

```bash
python -m venv .venv
```

Activate the virtual environment and install the dependencies:

```bash
python -m pip install -r requirements.txt
```

Configure a `client.json` file and point the agent to it using the
`DAPPER_CONFIG` environment variable.

For example, on Linux or macOS:

```bash
export DAPPER_CONFIG="$PWD/client.json"
python agent.py
```

In PowerShell:

```powershell
$env:DAPPER_CONFIG = "$PWD\client.json"
python .\agent.py
```

Do not commit real API tokens or configuration files containing secrets
to source control.

## Example service configurations

Example configuration files for the supported platforms are provided
in this directory:

- `dapper-agent.service` — Linux systemd service
- `DapperAgent.xml` — Windows WinSW service
- `com.dapper.agent.plist` — macOS launchd LaunchDaemon

Use the examples as a starting point and adjust installation paths
where necessary.
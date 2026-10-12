# dapper client
Steps to configure the clients in the network.

## Linux

### Initial setup

```bash
# service folders
sudo mkdir -p /opt/dapper
sudo mkdir -p /etc/dapper

sudo cp agent.py /opt/dapper/agent.py

sudo python3 -m venv /opt/dapper/venv

sudo /opt/dapper/venv/bin/pip install \
  -r requirements.txt
```

### Configure using `/etc/dapper/client.json`

```json
{
  "server_url": "http://<servername-or-ip>:8000",
  "api_token": "REPLACE_WITH_YOUR_DAPPER_API_TOKEN",
  "interval_seconds": 30
}
```

### Service setup using `/etc/systemd/system/dapper-agent.service`

```ini
[Unit]
Description=Dapper Network Monitoring Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple

ExecStart=/opt/dapper/venv/bin/python /opt/dapper/agent.py

Restart=on-failure
RestartSec=10

User=root
Group=root

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

### Start service and check status

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dapper-agent

sudo systemctl status dapper-agent
sudo journalctl -u dapper-agent -f
```

## Windows

### Pre-requisites:

Install python e.g. `winget install python.python.3.14`
Download WinSW-x64.exe from https://github.com/winsw/winsw/releases/#release-v2.12.0

### Initial setup - use an elevated terminal

```pwsh
# service folders
mkdir 'C:\Program Files\Dapper'
mkdir C:\ProgramData\Dapper

cp agent.py 'C:\Program Files\Dapper\agent.py'
cp 'C:\Users\<user>\Downloads\WinSW-x64.exe' 'C:\Program Files\Dapper\DapperAgent.exe'

python -m venv 'C:\Program Files\Dapper\venv'
'C:\Program Files\Dapper\Scripts\pip.exe' install -r requirements.txt
```

### Configure using `C:\ProgramData\Dapper\client.json`

```json
{
  "server_url": "http://<servername-or-ip>:8000",
  "api_token": "REPLACE_WITH_YOUR_DAPPER_API_TOKEN",
  "interval_seconds": 30
}
```

### Service setup using `C:\Program Files\Dapper\DapperAgent.xml`

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

### Start service and check status

```pwsh
'C:\Program Files\Dapper\DapperAgent.exe' install
'C:\Program Files\Dapper\DapperAgent.exe' start
```

## MacOS

### Pre-requisites:

Install python e.g. `brew install python@3.14`

### Initial setup

```zsh
sudo mkdir -p /Library/Application\ Support/Dapper

sudo cp agent.py /Library/Application\ Support/Dapper

sudo python3.14 -m venv /Library/Application\ Support/Dapper/venv

sudo /Library/Application\ Support/Dapper/venv/bin/pip install -r requirements
```

### Configure using `/Library/Application\ Support/Dapper/client.json`

```json
{
  "server_url": "http://<servername-or-ip>:8000",
  "api_token": "REPLACE_WITH_YOUR_DAPPER_API_TOKEN",
  "interval_seconds": 30
}
```

### Service setup using `/Library/LaunchDaemons/com.dapper.agent.plist`

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

### Start service and check status

```zsh
sudo chown root:wheel \
  /Library/LaunchDaemons/com.dapper.agent.plist

sudo chmod 644 \
  /Library/LaunchDaemons/com.dapper.agent.plist

sudo launchctl bootstrap system \
  /Library/LaunchDaemons/com.dapper.agent.plist

sudo launchctl print system/com.dapper.agent
```

Examples for `client.json`, `dapper-agent.service`, `DapperAgent.xml` and 
`com.dapper.agent.plist` can be found in this folder.

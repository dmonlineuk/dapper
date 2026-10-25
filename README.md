# dapper
Network Health Monitoring and Dashboard

## Server

### build

```bash
cd dapper
podman build -t dapper:0.4.3 .
```

### run

```bash
podman run -d \
  --name dapper \
  --restart=unless-stopped \
  --env-file .env \
  -p 8000:8000 \
  -e HEARTBEAT_TIMEOUT_SECONDS=90 \
  dapper:0.4.3
```

### Push

```bash
# Have your github PAT ready to go, requires write:packages
# Paste in as the password when prompted
podman login ghcr.io --username dmonlineuk

podman tag dapper:0.4.3 ghcr.io/dmonlineuk/dapper:0.4.3

podman push ghcr.io/dmonlineuk/dapper:0.4.3
```

## Clients

See README.md in the dapper/client subfolder for OS specific instructions.

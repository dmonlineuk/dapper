# dapper
Network Health Monitoring and Dashboard

## Server

### build

```bash
cd dapper
podman build -t dapper .
```

### run

```bash
podman run -d \
  --name dapper \
  --restart=unless-stopped \
  --env-file .env \
  -p 8000:8000 \
  -e HEARTBEAT_TIMEOUT_SECONDS=90 \
  dapper
```

## Clients

See README.md in the dapper/client subfolder for OS specific instructions.

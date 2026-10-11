# dapper
Network Health Monitoring and Dashboard

## build

```bash
cd dapper
podman build -t dapper .
```

## run

```bash
podman run -d \
  --name dapper \
  --restart=unless-stopped \
  -p 8000:8000 \
  -e HEARTBEAT_TIMEOUT_SECONDS=90 \
  dapper
```

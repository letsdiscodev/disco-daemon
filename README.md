# Disco Daemon

## Build for Docker Hub

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64/v8 \
  --tag letsdiscodev/daemon \
  --push \
  .
```
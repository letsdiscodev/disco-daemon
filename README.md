# Disco Daemon

## Build for Docker Hub

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64/v8 \
  --tag letsdiscodev/daemon \
  --push \
  .
```

## Regenerate requirements.txt

We edit `requirements.in` to list the dependencies.
```bash
docker compose run --rm --no-deps web \
  uv pip compile requirements.in -o requirements.txt
```
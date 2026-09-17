# Disco Daemon

## Build for Docker Hub

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64/v8 \
  --tag letsdiscodev/daemon \
  --push \
  .
```

## Tests

```
uv run pytest
```

Unit tests for the pure parts (the vector config renderer, the syslog url parser, the
collector command builders, the reconciler with docker stubbed). The integration gate is
disco-tester with `--build-local`.

## Log forwarding image

Syslog destinations and `disco logs` run `timberio/vector` (pinned in
`disco/utils/vectorconfig.py`, multi-arch). Bump the tag and the manifest digest together
and run disco-tester's `vector-proof` and the logging steps before releasing.

## Linters/Formatters

```
bin/ruff check --fix .
bin/ruff format .
bin/mypy .
```

## Generating an Alembic revision

```
docker compose build --no-cache web
docker compose run --rm web rm -f data/disco.sqlite3
docker compose run --rm web touch data/disco.sqlite3
docker compose run --rm web alembic upgrade head
docker compose run --rm web alembic revision --autogenerate -m "0.1.0"
```

## Regenerate requirements.txt

We edit `requirements.in` to list the dependencies.
```bash
docker compose run --rm --no-deps web \
  uv pip compile requirements.in -o requirements.txt
```

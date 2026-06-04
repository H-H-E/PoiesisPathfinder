# Docker Smoke Test Checkpoint

Date: 2026-06-04
Branch: `codex-deployment-readiness-latest-todo`

## Scope

This checkpoint adds the Docker Gate A smoke runner:

```bash
python3 scripts/docker_smoke.py
```

The runner starts an isolated Compose project named
`poiesispathfinder-smoke`, forces `DRY_RUN_UPSTREAM=true`, verifies FastAPI
health, confirms the seed job produced seven dashboard-safe student records,
loads the Next.js dashboard, sends one dry-run chat request, runs the spam
block helper, and removes the smoke-test containers and volumes unless
`--keep-running` is supplied.

## Commands Run

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/docker_smoke.py scripts/spam_test.py
```

Result: passed

```bash
python3 scripts/docker_smoke.py --help
```

Result: passed

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Result: `41 passed, 1 warning`

```bash
docker compose config
```

Result: passed

```bash
python3 scripts/docker_smoke.py
```

Result: blocked by local Docker socket permissions:
`permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`

## Status

The smoke-test artifact is implemented and covered by unit tests, but the
Docker smoke TODO and Gate A should remain open until the runner passes on a
host with Docker daemon access.

# Prometheus Metrics Checkpoint

Date: 2026-06-04
Branch: `codex-deployment-readiness-latest-todo`

## Scope

This checkpoint adds `GET /metrics`, protected by the existing
`x-admin-token` admin auth. The endpoint renders Prometheus-style text from the
SQLite audit log and user table:

- request counts by route, status code, and error class
- request error counts and error ratio
- positive token usage by student ID and model
- upstream latency count, sum, and max for real forwarded requests
- active and inactive student key gauges

Raw student bearer keys are not exported.

## Commands Run

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_auth_and_forwarding.py
```

Result: `19 passed, 1 warning`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Result: `51 passed, 1 warning`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m py_compile app/__init__.py app/config.py app/database.py app/limiter.py app/main.py reset_monthly_tokens.py seed_club.py
```

Result: passed

```bash
docker compose config
```

Result: passed

## Expected Evidence

- `test_metrics_endpoint_requires_admin_and_exports_prometheus_snapshot` proves
  `/metrics` requires admin auth, emits text/plain Prometheus-style output,
  includes request, error, token, latency, error-ratio, and student-state
  metrics, and does not leak raw student keys.
- `docker compose config` proves the deployment stack still parses with the new
  metrics code.

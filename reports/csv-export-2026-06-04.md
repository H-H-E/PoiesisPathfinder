# Teacher CSV Export Checkpoint

Date: 2026-06-04
Branch: `codex-deployment-readiness-latest-todo`

## Scope

This checkpoint adds `GET /admin/export.csv`, protected by the existing
`x-admin-token` admin auth. The endpoint returns one row per student with safe
club-administration fields:

- student ID, name, key preview, and active/inactive status
- current token total and remaining allowance
- audit event count, successful request count, incident count
- audited positive tokens, latest incident, and last activity timestamp

Raw student bearer keys are not exported.

## Commands Run

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_auth_and_forwarding.py
```

Result: `20 passed, 1 warning`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Result: `52 passed, 1 warning`

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

- `test_admin_csv_export_requires_admin_and_summarizes_usage_and_incidents`
  proves `/admin/export.csv` requires admin auth, emits `text/csv`, includes
  safe usage and incident summary values, and does not leak raw student keys.
- `docker compose config` proves the deployment stack still parses.

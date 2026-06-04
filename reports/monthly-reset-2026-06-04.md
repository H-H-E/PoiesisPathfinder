# Monthly Reset Job Checkpoint

Date: 2026-06-04
Branch: `codex-deployment-readiness-latest-todo`

## Scope

This checkpoint adds `gateway-brain/reset_monthly_tokens.py`, an operator-run
monthly billing reset job. The job resets every student's
`total_tokens_consumed` to `0` for a named `YYYY-MM` billing period and writes
one `/admin/monthly-reset` audit event per student with the negative token delta
that explains the reset.

The job intentionally preserves each student's active/inactive state. Operators
must review inactive keys and unlock them manually when safe.

## Commands Run

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_monthly_reset.py
```

Result: `3 passed`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Result: `49 passed, 1 warning`

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

- `test_monthly_reset_clears_totals_and_retains_audit_history` proves token
  totals reset to zero, prior audit rows remain queryable, monthly reset audit
  rows are written for all seven students, and inactive state is preserved.
- `test_monthly_reset_script_rejects_invalid_period` proves the job requires a
  concrete `YYYY-MM` billing boundary.
- `test_monthly_reset_script_prints_safe_summary` proves the operator summary
  does not print raw student bearer keys.

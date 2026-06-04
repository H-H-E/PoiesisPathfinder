# Per-Model Policy Checkpoint

Date: 2026-06-04
Branch: `codex-deployment-readiness-latest-todo`

## Scope

This checkpoint adds server-side per-model policy controls for student chat
requests. Operators can configure:

- `POIESIS_ALLOWED_MODELS`
- `POIESIS_BLOCKED_MODELS`
- `POIESIS_STUDENT_ALLOWED_MODELS`
- `POIESIS_STUDENT_BLOCKED_MODELS`

Global allow/block lists apply to every student. Student allowlists narrow the
effective allowlist for that student, and student blocklists add to global
blocked models. Rejected models return HTTP 403 before Redis rate-limit
recording or upstream forwarding.

## Commands Run

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_seed_and_dry_run.py
```

Result: `18 passed, 1 warning`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Result: `46 passed, 1 warning`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m py_compile app/__init__.py app/config.py app/database.py app/limiter.py app/main.py seed_club.py
```

Result: passed

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/docker_smoke.py scripts/spam_test.py
```

Result: passed

```bash
docker compose config
```

Result: passed

## Evidence

- `test_global_model_allowlist_blocks_unlisted_model_before_rate_limit_recording`
  proves a global allowlist rejects unlisted models before quota recording.
- `test_student_model_allowlist_overrides_global_allowlist` proves student
  allowlists can narrow model access for one student.
- `test_student_model_blocklist_blocks_model_and_writes_audit_event` proves a
  student blocklist rejects a model, avoids rate-limit recording, and writes an
  audit event with `error_class=model_blocked`.
- `docker-compose.yml` passes the policy environment variables into
  `gateway-brain`, so the controls are available in the Compose deployment.

## Remaining Release Blockers

- Gate A still requires Docker daemon access to run
  `python3 scripts/docker_smoke.py` end to end.
- Gate D still requires real Portkey/Minimax credentials and a controlled
  non-dry-run probe.

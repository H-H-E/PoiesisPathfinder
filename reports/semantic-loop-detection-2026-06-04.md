# Semantic Loop Detection Checkpoint

Date: 2026-06-04
Branch: `codex-deployment-readiness-latest-todo`

## Scope

This checkpoint adds audit-only repeated-prompt detection for chat requests.
When `SEMANTIC_LOOP_DETECTION_ENABLED=true`, the gateway hashes the validated
chat `messages` payload per student. Once the same student repeats the same
payload at least `SEMANTIC_LOOP_REPEAT_THRESHOLD` times, the gateway writes an
audit event with `error_class=semantic_loop_detected`.

The detection does not block the request and does not store raw prompt text.

## Commands Run

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_seed_and_dry_run.py
```

Result: `19 passed, 1 warning`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Result: `50 passed, 1 warning`

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

- `test_repeated_identical_prompt_writes_semantic_loop_audit_without_raw_prompt`
  proves the second identical prompt writes exactly one semantic loop audit
  event, both requests still succeed, rate-limit recording still occurs for
  admitted requests, and the persisted prompt fingerprint does not contain the
  raw prompt text.
- `docker compose config` proves the loop detection env vars are available in
  the Compose deployment.

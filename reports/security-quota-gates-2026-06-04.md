# Security and Quota Gate Validation

Date: 2026-06-04
Branch: `codex-deployment-readiness-latest-todo`

## Scope

This checkpoint validates the local evidence for:

- Gate B: security baseline
- Gate C: quota integrity

It does not validate Gate A Docker runtime startup or Gate D real
Portkey/Minimax traffic.

## Commands Run

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_auth_and_forwarding.py
```

Result: `18 passed, 1 warning`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Result: `43 passed, 1 warning`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m py_compile app/__init__.py app/config.py app/database.py app/limiter.py app/main.py seed_club.py
```

Result: passed

## Gate B Evidence

| Requirement | Evidence |
| --- | --- |
| Admin auth fails closed | `gateway-brain/tests/test_auth_and_forwarding.py::test_admin_routes_fail_closed_without_configured_admin_token` verifies admin routes return HTTP 503 when no admin token is configured and insecure admin mode is false. |
| Raw virtual keys are not exposed outside FastAPI | `gateway-brain/tests/test_auth_and_forwarding.py::test_admin_users_payload_hides_raw_keys_and_hashes` verifies `/admin/users` omits raw keys and key hashes. |
| Redis keys do not contain usable secrets | `gateway-brain/tests/test_auth_and_forwarding.py::test_redis_limiter_keys_use_student_id_not_raw_virtual_key` verifies limiter keys use stable `student_id` values and not `sk-poiesis-...` bearer keys. |
| Every request receives a correlation ID | `gateway-brain/tests/test_auth_and_forwarding.py::test_chat_forwarding_threads_request_id_to_logs_headers_and_portkey` verifies request IDs are threaded to logs, response headers, and Portkey metadata. |

## Gate C Evidence

| Requirement | Evidence |
| --- | --- |
| Burst and five-hour windows are checked atomically | `gateway-brain/tests/test_atomic_limiter.py::test_burst_rejection_does_not_consume_long_window_quota` and `gateway-brain/tests/test_atomic_limiter.py::test_default_burst_guardrails_block_standard_third_and_high_speed_second_requests` verify paired limiter behavior. |
| Missing usage is handled by policy | `gateway-brain/tests/test_auth_and_forwarding.py::test_chat_forwarding_missing_usage_returns_502_without_token_update` verifies successful upstream responses without usable `usage` are rejected without token updates. |
| Concurrent requests cannot race past quota | `gateway-brain/tests/test_atomic_limiter.py::test_concurrent_requests_cannot_race_past_long_window_quota` verifies Redis atomicity under concurrent attempts. |
| Monthly cap crossing locks the key immediately | `gateway-brain/tests/test_seed_and_dry_run.py::test_record_token_usage_deactivates_when_ceiling_is_reached` and `gateway-brain/tests/test_seed_and_dry_run.py::test_dry_run_completion_that_crosses_ceiling_locks_future_requests` verify post-accounting deactivation. |
| Token accounting remains consistent | `gateway-brain/tests/test_seed_and_dry_run.py::test_concurrent_token_usage_records_do_not_lose_sqlite_updates` and `gateway-brain/tests/test_auth_and_forwarding.py::test_final_portkey_response_after_retries_is_accounted_once` verify SQLite increments and retry accounting. |

## Remaining Release Blockers

- Gate A still requires a Docker-enabled runtime where `python3 scripts/docker_smoke.py`
  can prove Compose startup, seed completion, dashboard load, dry-run chat, and
  spam blocking.
- Gate D still requires real Portkey/Minimax credentials and one controlled
  non-dry-run request to prove provider configuration and real `usage` shape.

# PoiesisPathfinder Local Verification Checkpoint

Date: 2026-06-04
Branch: `codex-deployment-readiness-latest-todo`
Baseline commit: `1194bc8`

## Scope

This checkpoint validates the local backend safety coverage added before
deployment readiness work continues. It supports Milestone 8's backend pytest
coverage item and records current blockers for Docker runtime smoke and real
Portkey/Minimax traffic.

It does not close the release gates by itself. Gate A still needs a successful
runtime compose smoke, Gate D still needs real upstream credentials, and Gate E
still needs the operator runbook checklist.

## Commands Run

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Result: `38 passed, 1 warning`

```bash
cd gateway-brain
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m py_compile app/__init__.py app/config.py app/database.py app/limiter.py app/main.py seed_club.py
```

Result: passed

```bash
cd /home/codexdev/work/codex-mega-git/PoiesisPathfinder
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/spam_test.py
python3 scripts/spam_test.py --help
```

Result: passed

```bash
cd /home/codexdev/work/codex-mega-git/PoiesisPathfinder
docker compose config
```

Result: passed

```bash
cd /home/codexdev/work/codex-mega-git/PoiesisPathfinder
docker compose -p poiesispathfinder-smoke up --build --wait
```

Result: blocked by local Docker socket permissions:
`permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`

## Backend Coverage Evidence

Milestone 8 asks for pytest coverage across the backend safety logic below.
The current suite covers each area with focused tests:

| Requirement | Evidence |
| --- | --- |
| Auth failures | `gateway-brain/tests/test_auth_and_forwarding.py::test_chat_auth_rejects_missing_malformed_unknown_and_inactive_keys` |
| Monthly cap lockout | `gateway-brain/tests/test_seed_and_dry_run.py::test_record_token_usage_deactivates_when_ceiling_is_reached`; `gateway-brain/tests/test_seed_and_dry_run.py::test_dry_run_completion_that_crosses_ceiling_locks_future_requests` |
| Redis five-hour window | `gateway-brain/tests/test_atomic_limiter.py::test_standard_five_hour_window_blocks_643rd_default_request`; `gateway-brain/tests/test_atomic_limiter.py::test_high_speed_five_hour_window_blocks_322nd_default_request`; `gateway-brain/tests/test_atomic_limiter.py::test_concurrent_requests_cannot_race_past_long_window_quota` |
| Burst window | `gateway-brain/tests/test_atomic_limiter.py::test_burst_rejection_does_not_consume_long_window_quota`; `gateway-brain/tests/test_atomic_limiter.py::test_default_burst_guardrails_block_standard_third_and_high_speed_second_requests` |
| Token accounting | `gateway-brain/tests/test_seed_and_dry_run.py::test_dry_run_chat_completion_is_openai_compatible_and_records_usage`; `gateway-brain/tests/test_auth_and_forwarding.py::test_chat_forwarding_records_prompt_and_completion_usage_once`; `gateway-brain/tests/test_auth_and_forwarding.py::test_final_portkey_response_after_retries_is_accounted_once`; missing-usage, timeout, and request-error no-update tests in `gateway-brain/tests/test_auth_and_forwarding.py` |
| Admin reset and token adjustment | `gateway-brain/tests/test_auth_and_forwarding.py::test_admin_reset_window_endpoint_resets_standard_high_speed_or_all_windows`; `gateway-brain/tests/test_auth_and_forwarding.py::test_admin_reset_window_endpoint_rejects_unknown_student_without_reset`; `gateway-brain/tests/test_auth_and_forwarding.py::test_admin_adjust_tokens_applies_positive_and_negative_deltas`; `gateway-brain/tests/test_auth_and_forwarding.py::test_admin_adjust_tokens_clamps_negative_totals_at_zero`; `gateway-brain/tests/test_auth_and_forwarding.py::test_admin_adjust_tokens_rejects_unknown_student_without_adjustment` |

Additional relevant coverage includes raw-key hiding, safe key seeding, key
preview truncation, malformed/oversized request rejection before rate-limit
recording, server-side tier derivation, structured rate-limit errors, audit log
writes for successful dry-run requests and rate-limit rejections, Portkey
credential stripping, request ID propagation, and Redis fail-closed behavior.

## Open Blockers

- Docker runtime smoke is not validated in this environment because the current
  user cannot access `/var/run/docker.sock`. This leaves the compose startup,
  seed job, dashboard load, dry-run chat, spam block, and persistence acceptance
  checks open.
- The real Portkey/Minimax probe needs external credentials and a controlled
  non-dry-run request. Provider header/config selection, real `usage` shape,
  and upstream token accounting must remain unchecked until that probe runs.
- Release Gate B and Gate C have strong local implementation and pytest
  evidence, but should not be closed until an explicit gate validation report
  records admin-auth failure, dashboard payload inspection, Redis key inspection,
  and quota behavior in the deployed runtime.
- Gate E remains open until the operator runbook checklist is written as a
  complete start, verify, monitor, reset, lock, rotate, and recovery procedure.

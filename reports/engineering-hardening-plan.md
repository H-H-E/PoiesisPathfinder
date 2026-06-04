# PoiesisPathfinder Engineering Hardening Plan

Date: 2026-06-04

## Executive Summary

PoiesisPathfinder has the right first shape: FastAPI owns governance, Redis owns rolling-window limits, SQLite owns per-student budget state, Portkey owns provider translation, and Next.js gives the operator a compact dashboard. Before real Minimax traffic is enabled, the plan needs stronger safety gates around identity, quota accounting, admin access, auditability, and failure modes.

The main hardening move is to convert the backlog from "build these pieces" into "prove these invariants before exposing upstream quota." The system should fail closed when admin auth, Redis, usage accounting, or upstream routing is ambiguous.

## Current Architecture Evidence

- FastAPI entrypoint: `gateway-brain/app/main.py`
- SQLite helpers: `gateway-brain/app/database.py`
- Redis sliding-window limiter: `gateway-brain/app/limiter.py`
- Runtime settings: `gateway-brain/app/config.py`
- Dashboard client: `dashboard/src/components/DashboardClient.tsx`
- Dashboard API proxy: `dashboard/src/lib/gateway.ts`
- Existing backlog: `TODO.md`

## Safety Invariants

These invariants should become release gates.

1. Student virtual keys never leave the FastAPI trust boundary.
2. Student virtual keys are not stored, logged, returned to the dashboard, or embedded in Redis keys in raw form after production seeding.
3. Admin endpoints are unreachable without a configured admin secret, even in local Docker mode unless explicitly set to development mode.
4. A request is forwarded to Portkey only after auth, monthly budget, request shape, body size, model policy, and all rate-limit checks pass.
5. A request that is rejected by a burst limiter does not consume the five-hour rolling-window quota unless the product intentionally wants blocked attempts counted as abuse.
6. Token totals are updated exactly once per successful upstream completion.
7. If the upstream response lacks a usable `usage` block, the system follows an explicit policy: reject, estimate with audit annotation, or quarantine the response.
8. When a successful response pushes a user over the monthly ceiling, the key is deactivated immediately in the same accounting path.
9. Redis unavailable means fail closed with HTTP 503, not pass-through to upstream.
10. Every allow, block, admin override, and token adjustment has an audit record with a correlation ID.

## Trust Boundaries

### Boundary 1: Student Machine to FastAPI

- Protocol: HTTP to `POST /v1/chat/completions`
- Credential: `Authorization: Bearer sk-poiesis-...`
- Risk: leaked student key, replayed key, oversized request body, client-chosen tier abuse, infinite loop traffic.
- Harden:
  - Validate key format before DB lookup.
  - Store keyed hashes instead of raw keys.
  - Add request body size and message-count caps.
  - Derive tier from server-side policy or requested model, not from a trusted client header.
  - Add correlation ID and structured decision logging.

### Boundary 2: FastAPI to Redis

- Protocol: Redis internal network.
- Asset: rolling-window request state.
- Risk: raw key leakage in Redis keys, race conditions, partial accounting between burst and long windows.
- Harden:
  - Use stable `student_id` or HMAC key digest in Redis keys.
  - Replace separate long-window and burst recordings with a single atomic Lua script that checks both windows and records both only when both pass.
  - Add fail-closed handling for Redis connection errors.

### Boundary 3: FastAPI to SQLite

- Protocol: local file-backed DB.
- Asset: student identity, active state, token budgets, audit history.
- Risk: raw key disclosure, lost token updates, weak incident reconstruction.
- Harden:
  - Split `users` into non-secret identity fields and hashed credentials.
  - Add `audit_events`.
  - Add `monthly_budget_period` or billing-cycle fields before monthly reset automation.
  - Wrap post-hook token increment plus over-budget deactivation in one transaction.

### Boundary 4: FastAPI to Portkey

- Protocol: internal HTTP.
- Credential: Minimax or Portkey upstream credential.
- Risk: wrong provider header, student key forwarding, usage not returned, retry semantics causing double counting, metadata privacy leakage.
- Harden:
  - Confirm exact Portkey Minimax provider/config behavior before disabling dry-run.
  - Never forward student Authorization headers.
  - Use pseudonymous metadata, not student names.
  - Treat non-JSON, streaming, and missing-usage responses according to explicit accounting policy.
  - Add timeout mapping and upstream error classes.

### Boundary 5: Browser to Next.js Dashboard

- Protocol: browser HTTP to Next.js routes.
- Credential: no browser-side admin secret; Next.js holds `POIESIS_ADMIN_TOKEN` server-side.
- Risk: dashboard exposed on club network, admin token unset, raw virtual keys returned to browser, destructive action misclicks.
- Harden:
  - Require admin auth at the dashboard entrypoint, not only at FastAPI.
  - Return `student_id` and key preview only, not raw keys.
  - Add confirmation for destructive overrides.
  - Add action audit notes for resets, token deltas, lock/unlock.

## Priority Findings

### P0: Admin Auth Is Optional If `POIESIS_ADMIN_TOKEN` Is Missing

Evidence: `require_admin` only enforces auth when `settings.poiesis_admin_token` is truthy.

Impact: If the service starts without the env var, `/admin/users` and mutation endpoints are effectively open to anyone who can reach FastAPI.

Plan:

- Add `ADMIN_AUTH_REQUIRED=true` by default.
- Fail startup if admin auth is required and `POIESIS_ADMIN_TOKEN` is empty.
- Allow unauthenticated admin only with an explicit `ALLOW_INSECURE_ADMIN=true` development flag.

### P0: Raw Virtual Keys Are Used As Identifiers Across Boundaries

Evidence: SQLite primary key is `virtual_key`; Redis keys include the raw virtual key; admin payloads include `virtual_key`; Next.js uses virtual key in API path params.

Impact: Logs, Redis inspection, dashboard browser state, and route paths can leak usable student credentials.

Plan:

- Add `student_id` as the stable public identifier.
- Store `virtual_key_hash` using HMAC-SHA256 with a server secret.
- Return only `student_id` and `virtual_key_preview` to dashboard.
- Use `student_id` or HMAC digest in Redis keys.

### P0: Burst Rejection Can Consume Five-Hour Quota

Evidence: `enforce_rate_limits` records the five-hour window before checking the burst window.

Impact: Rapidly rejected burst attempts can still drain the five-hour allowance.

Plan:

- Replace the two-step limiter with one Lua script that checks both windows first, then records both if both allow.
- Decide and document whether rejected attempts should count as abuse separately in audit logs.

### P0: Missing `usage` Can Bypass Monthly Budget Accounting

Evidence: `usage_total_tokens` returns `0` when the response lacks a recognized usage block.

Impact: If Portkey/Minimax returns a successful response without usage, the student can spend upstream quota without reducing local budget.

Plan:

- Before real upstream mode, verify that Portkey returns OpenAI-compatible `usage` for Minimax.
- Add `MISSING_USAGE_POLICY=reject|estimate|allow` with `reject` as the safe default for real upstream.
- Audit every missing-usage event.

### P1: Monthly Ceiling Is Enforced Before Request But Not Immediately After Increment

Evidence: `deactivate_if_spent` runs before forwarding; `increment_tokens` runs after successful response.

Impact: A response can push the user over budget, but the user is only deactivated on the next request.

Plan:

- Add `increment_tokens_and_deactivate_if_spent`.
- Return an audit event when a successful request locks the key.

### P1: Client-Declared Tier Is Trusted

Evidence: `infer_request_tier` accepts `x-poiesis-tier`, `x-request-tier`, or `poiesis_tier`.

Impact: If tiers later map to model classes, students can choose the cheaper/higher allowance route unless server policy controls it.

Plan:

- Keep client tier only for dry-run testing.
- In production, infer tier from requested model or per-student policy.
- Reject unknown models before rate-limit recording.

### P1: No Audit Log Yet

Evidence: database schema only contains `users`.

Impact: Operators cannot reconstruct why a student was blocked, who reset a window, or how tokens changed.

Plan:

- Add `audit_events` with correlation ID, event type, student ID, key preview, tier, model, status, token delta, rate-limit state, admin actor, and timestamp.
- Include audit writes for blocks, forwards, upstream failures, token increments, and admin overrides.

### P1: Dashboard Actions Need Safer Interaction Design

Evidence: dashboard has immediate reset, lock/unlock, and token delta buttons.

Impact: Operator misclick can reset limits, lock keys, or alter token balances without a reason trail.

Plan:

- Add confirmation states for reset-all, lock, unlock, and non-default token deltas.
- Require an admin note for token adjustments and lock/unlock.
- Display last audit event per student.

## Hardened Execution Plan

### Gate A: Dry-Run Local System

Goal: prove orchestration and local safety without Minimax traffic.

Required:

- Root `docker-compose.yml`
- Seed job
- README quickstart
- Dry-run chat success
- Standard burst block on the third request in 60 seconds
- Dashboard loads through Next.js proxy

Exit criteria:

- `docker compose up --build` starts Redis, FastAPI, dashboard, Portkey, and seed job.
- Dry-run curl returns OpenAI-shaped JSON with `usage`.
- Spam curl returns HTTP 429.

### Gate B: Security Baseline

Goal: prevent accidental credential and admin exposure.

Required:

- Mandatory admin token or explicit insecure-dev flag
- Raw-key removal from dashboard payloads and Redis key names
- HMAC-hashed virtual key lookup
- Request body size limit
- Audit log table
- Correlation ID propagation

Exit criteria:

- Admin endpoints fail closed when token is missing.
- Dashboard never receives raw virtual keys.
- Redis key scan does not reveal student credentials.

### Gate C: Quota Integrity

Goal: make quota math resistant to races and ambiguous upstream responses.

Required:

- Atomic multi-window limiter
- Missing-usage policy
- Post-increment budget deactivation
- Tests for long-window, burst-window, concurrent requests, and over-budget lockout

Exit criteria:

- Blocked burst requests do not alter five-hour counts unless explicitly configured.
- Concurrent requests cannot exceed Redis quota.
- Successful response crossing the monthly cap locks the key immediately.

### Gate D: Real Portkey/Minimax Probe

Goal: enable non-dry-run mode with one controlled request.

Required:

- Confirm Portkey image name and provider header/config for Minimax.
- Confirm `usage` shape from real response.
- Confirm no student key appears in Portkey logs or upstream metadata.
- Confirm upstream errors do not increment tokens.

Exit criteria:

- One real Minimax request succeeds through the full stack.
- Token usage increments exactly once.
- A missing or malformed `usage` response is handled according to configured policy.

### Gate E: Operator Runbook

Goal: make the system safe for coding-club operation.

Required:

- Key distribution checklist
- Key rotation workflow
- Incident workflow for runaway scripts
- Monthly reset policy
- Dashboard override policy
- Validation report template

Exit criteria:

- A non-developer operator can start, seed, verify, monitor, reset, lock, and rotate keys from documented commands.

## Test Matrix

| Area | Test | Expected Result |
| --- | --- | --- |
| Auth | Missing student bearer | HTTP 401 |
| Auth | Unknown student key | HTTP 401 |
| Admin | Missing admin token in required mode | Startup failure or HTTP 401 |
| Budget | User at monthly cap | HTTP 403 and inactive key |
| Budget | Response crosses cap | Token update succeeds and key becomes inactive |
| Rate limit | Standard third request within 60s | HTTP 429 |
| Rate limit | Standard 643rd request in 5h | HTTP 429 |
| Rate limit | Concurrent requests near limit | No quota overrun |
| Accounting | Missing usage in real mode | Reject or audit estimate per policy |
| Proxy | Upstream 5xx | No token increment |
| Dashboard | User payload | No raw virtual key |
| Dashboard | Reset all | Confirmation and audit event |
| Secrets | Redis key scan | No raw virtual keys |
| Secrets | Portkey metadata | No raw virtual keys or unnecessary student PII |

## Recommended Next Implementation Order

1. Add root `docker-compose.yml`, `README.md`, and a dry-run smoke script.
2. Make admin auth fail closed and add startup validation.
3. Add `student_id`, `virtual_key_hash`, and key hashing helpers.
4. Refactor admin endpoints and dashboard routes to use `student_id`.
5. Replace two-step limiter with atomic multi-window check/record.
6. Add audit table and correlation IDs.
7. Add missing-usage policy and post-increment cap deactivation.
8. Add pytest coverage and Docker smoke tests.
9. Validate Portkey Minimax behavior with one real controlled request.
10. Finalize the operator runbook.

## Open Decisions

- Should rejected burst attempts count against a separate abuse counter?
- Should high-speed tier be inferred by model name, student policy, or admin-selected route?
- Should monthly budgets reset automatically by calendar month or by upstream billing-cycle date?
- Should the dashboard be reachable from the club network or bound to localhost/VPN only?
- Should student names be sent to Portkey metadata, or should all upstream observability use pseudonymous IDs?

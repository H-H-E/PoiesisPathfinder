# PoiesisPathfinder Detailed TODOs

This backlog expands the PRD into buildable slices for a local AI governance gateway:

Hermes client -> FastAPI governance brain -> Portkey sidecar -> Minimax API.

## Milestone 0: Repo Readiness

- [ ] Confirm repository ownership and remote target.
  - Acceptance: `git remote -v` points at the intended GitHub repository.
  - Acceptance: `main` can be pushed and cloned by collaborators.
- [ ] Add a contributor-safe `.env.example`.
  - Include `MINIMAX_API_KEY`, `PORTKEY_PROVIDER`, `PORTKEY_CONFIG`, `POIESIS_ADMIN_TOKEN`, `DRY_RUN_UPSTREAM`, and dashboard/backend URLs.
  - Do not commit real API keys.
- [ ] Add a root `README.md`.
  - Explain the network flow, local startup, dry-run mode, real Minimax mode, dashboard access, and troubleshooting.
- [ ] Add `.gitignore`.
  - Ignore SQLite files, Redis dumps, Python caches, `node_modules`, Next build output, and local env files.

## Milestone 1: Docker Orchestration

- [ ] Create root `docker-compose.yml`.
  - Include `gateway-brain`, `redis`, `portkey`, `dashboard`, and a one-shot seed service.
  - Acceptance: `docker compose up --build` starts all long-running services.
- [ ] Wire FastAPI to Redis through the internal Docker network.
  - Acceptance: `GET http://localhost:8000/health` returns Redis healthy.
- [ ] Wire FastAPI to Portkey through the internal Docker network.
  - Acceptance: FastAPI forwards a valid request to `http://portkey:8787/v1/chat/completions`.
- [ ] Persist SQLite data across restarts.
  - Acceptance: seeded students and token totals survive `docker compose down` followed by `docker compose up`.
- [ ] Document Portkey image ambiguity.
  - Note that the PRD names `@portkey-ai/gateway`, while the public Docker image is commonly published as `portkeyai/gateway`.
  - Acceptance: compose uses a working image name and the README explains how to change it if Portkey updates packaging.

## Milestone 2: Student Key Seeding

- [ ] Seed exactly seven initial student records.
  - Acceptance: every record has `virtual_key`, `student_name`, `total_tokens_consumed = 0`, and `is_active = true`.
- [ ] Support secure key regeneration.
  - Acceptance: `python seed_club.py --random` creates seven non-demo keys without printing secrets anywhere except the operator terminal.
- [ ] Add an operator checklist for key distribution.
  - Include student handoff, lost-key rotation, and what to do if a key leaks.
- [ ] Add a key format policy.
  - Acceptance: all student keys use the `sk-poiesis-...` prefix and are easy to distinguish from the Minimax master key.

## Milestone 3: FastAPI Governance Brain

- [ ] Harden pre-flight authentication.
  - Verify `Authorization: Bearer sk-poiesis-...`.
  - Reject missing, malformed, unknown, or inactive keys with HTTP 401.
  - Acceptance: no student key is forwarded to Portkey.
- [ ] Enforce monthly token ceiling.
  - Ceiling: `185,700,000` tokens per user.
  - Acceptance: when a user reaches the ceiling, `is_active` flips false and future requests return HTTP 403.
- [ ] Preserve the OpenAI-compatible request path.
  - Endpoint: `POST /v1/chat/completions`.
  - Acceptance: Hermes can use the gateway as an OpenAI-compatible base URL.
- [ ] Block unsupported streaming by default.
  - Reason: post-response accounting needs a final `usage` block.
  - Acceptance: `stream: true` returns HTTP 400 unless streaming accounting is explicitly implemented.
- [ ] Add structured error payloads.
  - Include `message`, `student_key_preview`, `limit`, `remaining`, `reset_after_seconds`, and `scope` where applicable.
- [ ] Add request correlation IDs.
  - Acceptance: each proxied request has a log-visible ID shared across FastAPI logs and Portkey metadata.

## Milestone 4: Redis Sliding Window Limits

- [ ] Enforce the standard five-hour window.
  - Limit: `642` requests per `18,000` seconds per user.
  - Acceptance: the 643rd request in the window returns HTTP 429.
- [ ] Enforce the high-speed five-hour window.
  - Limit: `321` requests per `18,000` seconds per user.
  - Acceptance: the 322nd high-speed request returns HTTP 429.
- [ ] Keep a short burst guardrail.
  - Standard: 2 requests per minute.
  - High-speed: 1 request per minute.
  - Acceptance: runaway loops are blocked quickly in local curl verification.
- [ ] Use atomic Redis operations.
  - Acceptance: concurrent requests cannot race past the quota.
- [ ] Add Redis key expiration.
  - Acceptance: idle user rate-limit keys disappear after the rolling window plus cleanup buffer.
- [ ] Add admin reset endpoints.
  - Reset standard window, high-speed window, or all windows for one student.

## Milestone 5: Portkey and Minimax Forwarding

- [ ] Verify the exact Portkey provider header for Minimax.
  - Current implementation assumes `x-portkey-provider: minimax`.
  - Acceptance: a real Minimax request succeeds through Portkey in non-dry-run mode.
- [ ] Decide whether to use a Portkey config object or provider-only routing.
  - Option A: `x-portkey-provider` plus Minimax API key in `Authorization`.
  - Option B: `x-portkey-config` with provider and retry settings.
- [ ] Strip student credentials before forwarding.
  - Acceptance: upstream only sees the master Minimax/Portkey credential.
- [ ] Add Portkey metadata.
  - Include student name, key preview, request tier, and request correlation ID.
- [ ] Confirm retries do not double-count tokens.
  - Acceptance: token accounting happens once from the final successful OpenAI-style response.
- [ ] Add timeout and upstream error handling.
  - Acceptance: upstream timeouts return clear 502/504-style errors without updating token usage.

## Milestone 6: Token Accounting

- [ ] Parse OpenAI-style `usage`.
  - Fields: `prompt_tokens`, `completion_tokens`, and `total_tokens`.
  - Acceptance: `total_tokens_consumed` increases by the response usage total.
- [ ] Make SQLite increments atomic.
  - Acceptance: concurrent successful responses do not lose updates.
- [ ] Add admin token adjustments.
  - Negative deltas restore allowance after a false positive or test.
  - Positive deltas allow manual accounting correction.
- [ ] Add an audit log table.
  - Fields: request ID, student key preview, token delta, route, model, status code, timestamp, and error class.
  - Acceptance: admin can explain why a student was blocked.

## Milestone 7: Brutalist Telemetry Dashboard

- [ ] Show all seven student tiles.
  - Each tile displays status, key preview, total spend, remaining monthly budget, and window counters.
- [ ] Add live refresh.
  - Acceptance: request counters update without page reload every 3-5 seconds.
- [ ] Show standard and high-speed quota separately.
  - Acceptance: admin can see both five-hour remaining counts and burst remaining counts.
- [ ] Add admin override actions.
  - Reset standard window.
  - Reset all windows.
  - Lock or unlock a key.
  - Apply a token delta.
- [ ] Protect admin actions.
  - Acceptance: browser calls Next.js API routes; Next.js forwards `POIESIS_ADMIN_TOKEN` server-side.
- [ ] Keep the UI operational and dense.
  - Avoid marketing copy.
  - Prioritize scan speed, contrast, keyboard access, and fixed dimensions for controls.

## Milestone 8: Verification and Safety Tests

- [ ] Add local dry-run verification commands.
  - Acceptance: curl can demonstrate success and rate-limit block without contacting Minimax.
- [ ] Add spam test script.
  - Inputs: virtual key, count, tier, delay.
  - Acceptance: standard tier blocks on the third request within 60 seconds.
- [ ] Add pytest coverage for backend logic.
  - Auth failures.
  - Monthly cap lockout.
  - Redis five-hour window.
  - Burst window.
  - Token accounting.
  - Admin reset and token adjustment.
- [ ] Add dashboard build check.
  - Acceptance: `npm run build` completes inside `dashboard/`.
- [ ] Add Docker smoke test.
  - Acceptance: compose starts, seed job runs, dashboard loads, dry-run chat request succeeds, spam request blocks.
- [ ] Add reports directory.
  - Store validation notes under `reports/` after each release checkpoint.

## Milestone 9: Operator Runbook

- [ ] Document normal startup.
  - `cp .env.example .env`
  - Fill secrets.
  - `docker compose up --build`
- [ ] Document dry-run startup.
  - `DRY_RUN_UPSTREAM=true` for quota-safe demos.
- [ ] Document real upstream startup.
  - `DRY_RUN_UPSTREAM=false` and real `MINIMAX_API_KEY`.
- [ ] Document common incidents.
  - Student infinite loop.
  - Monthly budget exhausted.
  - Redis unavailable.
  - Portkey unavailable.
  - Minimax upstream error.
- [ ] Document dashboard overrides.
  - When to reset windows.
  - When to restore tokens.
  - When to disable a key.
- [ ] Document key rotation.
  - Disable old key.
  - Generate new key.
  - Send new key out-of-band.
  - Verify old key fails with HTTP 401.

## Milestone 10: Future Enhancements

- [ ] Add per-model policy controls.
  - Allow or block specific Minimax models per student.
- [ ] Add monthly reset job.
  - Reset token totals on a defined billing boundary with audit history retained.
- [ ] Add request body size limits.
  - Prevent accidental giant context submissions.
- [ ] Add semantic loop detection.
  - Flag repeated identical prompts from the same student.
- [ ] Add Prometheus-style metrics.
  - Export request counts, blocks, token spend, upstream latency, and error rates.
- [ ] Add teacher-friendly CSV export.
  - Export student usage and incidents for club administration.

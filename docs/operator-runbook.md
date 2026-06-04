# PoiesisPathfinder Operator Runbook

Date: 2026-06-04

This runbook is for the coding-club operator who starts, verifies, monitors,
resets, locks, rotates, and recovers student keys for PoiesisPathfinder.

## Operating Rules

- Use dry-run mode for demos, training, and local verification. Dry-run mode
  returns local OpenAI-shaped responses and does not contact Minimax.
- Do not distribute `MINIMAX_API_KEY`, Portkey credentials, `.env`, or the
  admin token to students.
- Student keys must start with `sk-poiesis-`; the Minimax master key must never
  be used as a student key.
- Keep `ALLOW_INSECURE_ADMIN=false` outside isolated development.
- Use `POIESIS_ALLOWED_MODELS`, `POIESIS_BLOCKED_MODELS`,
  `POIESIS_STUDENT_ALLOWED_MODELS`, and `POIESIS_STUDENT_BLOCKED_MODELS` to
  limit which models students can reach. A blocked model is rejected before
  rate-limit recording or upstream forwarding.
- Treat the dashboard as an operator console. Students should only receive
  their own bearer key and gateway base URL.

## Normal Startup

Use this path for a local dry-run class session or for the first step of a real
deployment rehearsal.

1. Create the environment file:

   ```bash
   cp .env.example .env
   ```

2. Fill at least these values in `.env`:

   ```dotenv
   POIESIS_ADMIN_TOKEN=replace-with-a-private-operator-token
   POIESIS_KEY_HASH_SECRET=replace-with-a-long-random-stable-secret
   DRY_RUN_UPSTREAM=true
   ALLOW_INSECURE_ADMIN=false
   POIESIS_ALLOWED_MODELS=dry-run-minimax
   ```

3. Confirm Compose can parse the final configuration:

   ```bash
   docker compose config
   ```

4. Start the stack:

   ```bash
   docker compose up --build
   ```

5. Open the dashboard:

   ```text
   http://localhost:3000
   ```

6. Stop the stack when the session ends:

   ```bash
   docker compose down
   ```

Use `docker compose down --volumes` only for a deliberate reset. It removes the
SQLite and Redis volumes for this Compose project.

## Dry-Run Verification

Run this before students connect.

1. Start from dry-run mode:

   ```dotenv
   DRY_RUN_UPSTREAM=true
   ```

2. Run the full Gate A smoke runner:

   ```bash
   python3 scripts/docker_smoke.py
   ```

   The runner forces `DRY_RUN_UPSTREAM=true`, starts an isolated Compose
   project, checks FastAPI health, confirms seven seeded dashboard-safe
   students, loads the dashboard, sends one dry-run chat request, verifies the
   burst block with `scripts/spam_test.py`, and removes its smoke-test volumes.

3. If the stack is already running and you only want to re-check it:

   ```bash
   python3 scripts/docker_smoke.py --skip-compose-up
   ```

4. Manual spot checks:

   ```bash
   curl -fsS http://localhost:8000/health
   curl -fsS http://localhost:3000
   ```

5. Confirm the dashboard shows exactly seven students and only `key_preview`
   values. It must not show raw `sk-poiesis-...` keys.

## Real Upstream Startup

Use this only after dry-run verification passes and the Portkey/Minimax probe
is intentionally scheduled.

1. Set real upstream mode in `.env`:

   ```dotenv
   DRY_RUN_UPSTREAM=false
   MINIMAX_API_KEY=replace-with-real-minimax-master-key
   PORTKEY_BASE_URL=http://portkey:8787
   PORTKEY_PROVIDER=minimax
   PORTKEY_CONFIG=
   POIESIS_ALLOWED_MODELS=dry-run-minimax,approved-minimax-model
   ```

2. Start the stack:

   ```bash
   docker compose up --build
   ```

3. Send exactly one controlled request through FastAPI with a known student key.

4. Verify all of the following before allowing club traffic:

   - Portkey accepts the configured Minimax provider or config.
   - The response includes OpenAI-style `usage.total_tokens`.
   - Student credentials are not forwarded to Portkey or Minimax.
   - The dashboard token total increases exactly once.
   - Upstream errors return 502 or 504-style responses without token changes.

5. If any item fails, set `DRY_RUN_UPSTREAM=true`, restart the stack, and keep
   the real provider/config TODOs open.

## Monitoring

Keep these views open during a class session:

- Dashboard: `http://localhost:3000`
- Gateway health: `http://localhost:8000/health`
- Container logs:

  ```bash
  docker compose logs -f gateway-brain dashboard redis portkey
  ```

- Recent audit feed in the dashboard, or directly:

  ```bash
  curl -fsS 'http://localhost:8000/admin/audit-log?limit=25' \
    -H "x-admin-token: $POIESIS_ADMIN_TOKEN"
  ```

Watch for fast burst depletion, repeated 429 responses, inactive students,
403 monthly budget responses, and upstream 502/504 responses.

## Dashboard Overrides

Use dashboard actions sparingly and read the recent audit feed first.

| Action | Use when | Do not use when |
| --- | --- | --- |
| Reset standard window | A local dry-run test or operator mistake consumed the standard short window. | A student is actively looping or trying to bypass limits. Lock first. |
| Reset all windows | A controlled class reset is needed after confirming no abuse is ongoing. | The monthly budget is exhausted. Window reset does not restore monthly tokens. |
| Lock key | A key leaks, a client loops, a student is done for the day, or traffic looks unsafe. | You merely need to clear a burst block for a known safe request. |
| Unlock key | You have confirmed the student can safely resume and the key is not leaked. | The key leaked publicly or the monthly cap was legitimately exhausted. Rotate instead. |
| Apply negative token delta | A false positive, dry-run rehearsal, or failed accounting test consumed tokens. | The student is asking for more budget after real usage. |
| Apply positive token delta | You need to correct undercounted usage from an operator-reviewed incident. | Normal successful requests are already counted automatically. |

Every lock, unlock, and token adjustment writes an audit event. Prefer adding a
short operator note outside the app for the class record until notes are added
to the product UI.

## Common Incidents

### Student Infinite Loop

1. Lock the affected student in the dashboard.
2. Confirm the audit feed shows repeated requests or 429 responses for that
   `student_id`.
3. Ask the student to stop the client process and show the prompt/code path.
4. Leave the key locked until the loop source is fixed.
5. Reset windows only after the loop is stopped and you intentionally want the
   student to continue.

### Monthly Budget Exhausted

1. Confirm the dashboard shows the student locked or near zero remaining tokens.
2. Inspect recent audit events for real successful requests.
3. If the usage is legitimate, keep the key locked and do not reset windows.
4. If the usage is a false positive or rehearsal artifact, apply a negative
   token delta with the reviewed amount, then unlock only if the key is safe.

### Redis Unavailable

1. Expect chat requests to fail closed with HTTP 503.
2. Do not bypass Redis or disable rate limits.
3. Check logs:

   ```bash
   docker compose logs redis gateway-brain
   ```

4. Restart Redis or the full stack:

   ```bash
   docker compose restart redis gateway-brain
   ```

5. Re-run `curl -fsS http://localhost:8000/health` before students resume.

### Portkey Unavailable

1. In dry-run mode, Portkey should not be contacted for chat completions.
2. In real upstream mode, expect clear 502/504-style errors and no token
   increment for failed upstream requests.
3. Check Portkey and gateway logs:

   ```bash
   docker compose logs portkey gateway-brain
   ```

4. Return to dry-run mode if the class can continue without real Minimax:

   ```dotenv
   DRY_RUN_UPSTREAM=true
   ```

### Minimax Upstream Error

1. Pause real traffic and keep the dashboard open.
2. Confirm failed upstream requests did not increase token totals.
3. Check whether the error is provider config, rate limit, timeout, or a
   Minimax service response.
4. Do not retry broad student traffic until one controlled real request passes.

## Key Rotation

Use rotation for leaked, lost, or compromised student keys.

1. Lock the affected student immediately in the dashboard.
2. Confirm the old key fails:

   ```bash
   curl -i http://localhost:8000/v1/chat/completions \
     -H 'Authorization: Bearer sk-poiesis-old-key' \
     -H 'Content-Type: application/json' \
     -d '{"model":"dry-run-minimax","messages":[{"role":"user","content":"test"}]}'
   ```

   Expected result: HTTP 401 for inactive or unknown key.

3. Generate replacement keys in a maintenance window:

   ```bash
   cd gateway-brain
   python seed_club.py --database ./data/database.db --random
   ```

   The current seed command regenerates all seven student keys. Treat this as
   an all-student rotation: distribute every changed key out-of-band before
   reopening the session.

4. Send each new key out-of-band to the correct student. Do not paste keys into
   Git, shared chat, slides, or the dashboard.
5. Verify the dashboard still shows only a safe `key_preview`.
6. Send one dry-run request with the new key.
7. Keep the old key out of circulation and record the rotation in the class
   operations log.

## Recovery Checklist

Before reopening traffic after any incident:

1. `curl -fsS http://localhost:8000/health` succeeds.
2. The dashboard loads.
3. The affected student status is intentional: locked or active.
4. Recent audit events explain the block, reset, adjustment, or rotation.
5. A single dry-run request succeeds for any key you plan to return to service.
6. `scripts/spam_test.py` still observes a burst block for the standard tier.

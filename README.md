# PoiesisPathfinder

PoiesisPathfinder is a local AI governance gateway for a coding-club Minimax
deployment. Student clients such as Hermes send OpenAI-compatible chat requests
to a FastAPI gateway, the gateway enforces identity, rate limits, and token
budgets, then forwards allowed requests through Portkey to Minimax.

The repository is still in a pre-release state. Dry-run mode is the safe local
path today. Real Minimax traffic should wait until the release gates in
`TODO.md` and `reports/engineering-hardening-plan.md` are satisfied.

## Network Flow

```text
Hermes or OpenAI-compatible client
  -> POST /v1/chat/completions with Authorization: Bearer sk-poiesis-...
  -> gateway-brain FastAPI service on port 8000
  -> SQLite user and token-budget state
  -> Redis rolling-window and burst quota state
  -> Portkey gateway at /v1/chat/completions
  -> Minimax API

Operator browser
  -> dashboard Next.js app on port 3000
  -> dashboard /api routes
  -> FastAPI /admin routes with POIESIS_ADMIN_TOKEN attached server-side
```

## Services

| Service | Path | Purpose | Default port |
| --- | --- | --- | --- |
| `gateway-brain` | `gateway-brain/` | FastAPI governance brain, student auth, quota checks, token accounting, Portkey forwarding | `8000` |
| `dashboard` | `dashboard/` | Next.js operator dashboard and admin proxy routes | `3000` |
| `redis` | `docker-compose.yml` | Rolling-window and burst counters | `6379` |
| `portkey` | `docker-compose.yml` | Provider routing to Minimax | `8787` |
| `seed_club.py` | `gateway-brain/seed_club.py` | Seeds seven coding-club student keys into SQLite | n/a |

The root `docker-compose.yml` starts Redis, Portkey, the FastAPI gateway, the
Next.js dashboard, and a one-shot seed job. SQLite data is persisted in the
`gateway-data` volume.

## Configuration

Start from the contributor-safe template:

```bash
cp .env.example .env
```

Important variables:

| Variable | Meaning |
| --- | --- |
| `MINIMAX_API_KEY` | Master upstream key used by FastAPI when forwarding through Portkey. Never use a student key here. |
| `PORTKEY_BASE_URL` | Portkey gateway URL. Use `http://localhost:8787` for manual local Portkey, or `http://portkey:8787` inside Docker. |
| `PORTKEY_IMAGE` | Optional Compose override for the Portkey container image. The PRD refers to the package name `@portkey-ai/gateway`; this Compose stack defaults to the public Docker image `portkeyai/gateway:latest`. |
| `PORTKEY_PROVIDER` | Provider header value currently assumed to be `minimax`. |
| `PORTKEY_CONFIG` | Optional Portkey config object/header value if provider-only routing is not enough. |
| `DATABASE_PATH` | SQLite database path for student records and token totals. |
| `REDIS_URL` | Redis connection URL. |
| `POIESIS_ADMIN_TOKEN` | Token required by dashboard server routes when calling FastAPI admin endpoints. Required unless `ALLOW_INSECURE_ADMIN=true`. |
| `POIESIS_KEY_HASH_SECRET` | Secret used to HMAC-hash student bearer keys before storage and identity lookups. Use a long random value and keep it stable for an environment. |
| `ALLOW_INSECURE_ADMIN` | Explicit development-only escape hatch. Leave `false` for shared or production-like environments. |
| `DRY_RUN_UPSTREAM` | `true` returns local OpenAI-shaped responses without contacting Portkey or Minimax. |
| `ALLOW_STREAMING` | Defaults to `false`; streaming is blocked because accounting needs the final `usage` block. |
| `MAX_REQUEST_BODY_BYTES`, `MAX_MESSAGES`, `MAX_MESSAGE_CONTENT_CHARS`, `MAX_TOTAL_MESSAGE_CONTENT_CHARS` | Chat request guardrails applied before rate-limit recording. |
| `DEFAULT_REQUEST_TIER`, `HIGH_SPEED_STUDENT_IDS` | Server-side tier policy. Student tier headers or payload fields are ignored. |
| `GATEWAY_BRAIN_PORT`, `DASHBOARD_PORT`, `PORTKEY_PORT`, `REDIS_PORT` | Optional host port overrides for `docker compose` when the defaults are already in use. |

## Local Startup

Use dry-run mode for local verification so Minimax is not contacted.

### Docker Compose

1. Create local environment:

   ```bash
   cp .env.example .env
   ```

   The Compose file has safe dry-run defaults, but a real `.env` keeps operator
   tokens and future upstream settings explicit. Change `POIESIS_ADMIN_TOKEN`
   before using the stack on a shared network, and leave
   `ALLOW_INSECURE_ADMIN=false`.

2. Start the stack:

   ```bash
   docker compose up --build
   ```

   If another local service already owns a default port, change the matching
   `*_PORT` value in `.env` before starting Compose.

3. Verify the services:

   ```bash
   curl -fsS http://localhost:8000/health
   curl -fsS http://localhost:3000
   ```

4. Open the dashboard at `http://localhost:3000`.

The PRD names Portkey as `@portkey-ai/gateway`, which is the package name rather
than the Docker image reference used by Compose. This stack defaults to the
public Docker image `portkeyai/gateway:latest`. If Portkey changes packaging or
your environment requires a pinned internal image, set `PORTKEY_IMAGE` in `.env`
and rerun `docker compose up --build`.

### Manual Host Mode

1. Create and edit local environment:

   ```bash
   cp .env.example .env
   cp .env gateway-brain/.env
   ```

   The backend loads `.env` from the directory where `uvicorn` is started. The
   second copy keeps manual `cd gateway-brain` commands aligned with the root
   template. For manual host-mode startup, set:

   ```dotenv
   DRY_RUN_UPSTREAM=true
   REDIS_URL=redis://localhost:6379/0
   PORTKEY_BASE_URL=http://localhost:8787
   GATEWAY_BRAIN_URL=http://localhost:8000
   POIESIS_ADMIN_TOKEN=replace-with-local-admin-token
   POIESIS_KEY_HASH_SECRET=replace-with-long-random-secret
   ```

2. Start Redis:

   ```bash
   docker run --rm -p 6379:6379 redis:7-alpine
   ```

3. Seed the seven local-demo student keys:

   ```bash
   cd gateway-brain
   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   python seed_club.py --database ./data/database.db
   ```

   For non-demo keys, run `python seed_club.py --random` and distribute the
   printed keys out-of-band.

4. Start the FastAPI gateway:

   ```bash
   cd gateway-brain
   . .venv/bin/activate
   DRY_RUN_UPSTREAM=true REDIS_URL=redis://localhost:6379/0 uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

5. Start the dashboard:

   ```bash
   cd dashboard
   npm ci
   GATEWAY_BRAIN_URL=http://localhost:8000 POIESIS_ADMIN_TOKEN=replace-with-local-admin-token npm run dev
   ```

6. Open the dashboard at `http://localhost:3000`.

## Student Key Distribution

Student bearer keys must start with `sk-poiesis-`. That prefix lets operators
and students distinguish local club gateway keys from the master Minimax or
Portkey credential, which must never be sent to students or clients.

Use the documented demo keys only for local dry-run checks. For a real club
session, generate fresh keys in the operator terminal:

```bash
cd gateway-brain
python seed_club.py --database ./data/database.db --random
```

The command prints the seven fresh student keys once for handoff, while SQLite
stores only HMAC hashes and short previews. Do not paste generated keys into
Git, shared chat, slides, or the dashboard. Send each student exactly one key
out-of-band, ask them to use it as `Authorization: Bearer sk-poiesis-...`, and
verify the dashboard shows only `key_preview` values.

If a student loses a key, run `python seed_club.py --random` in a controlled
maintenance window, send the replacement key out-of-band, and verify the old key
returns HTTP 401. If a key leaks, immediately lock the affected student in the
dashboard, rotate with `--random`, reset rate windows only after reviewing the
audit feed, and distribute the new key out-of-band.

## Dry-Run Chat Check

Use one of the seeded demo keys from `gateway-brain/seed_club.py`:

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H 'Authorization: Bearer sk-poiesis-ada-7f3c9d2a' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "dry-run-minimax",
    "messages": [
      { "role": "user", "content": "Say hello from dry-run mode." }
    ]
  }'
```

Expected result: an OpenAI-shaped JSON response whose message says it is a
PoiesisPathfinder dry-run response and whose `usage.total_tokens` is present.

The standard tier has a short burst limit of 2 requests per 60 seconds. A third
quick request with the same key should return HTTP 429.

## Real Minimax Mode

Real upstream traffic requires external credentials and should only be enabled
after the dry-run gate passes.

1. Set a real master upstream key:

   ```dotenv
   DRY_RUN_UPSTREAM=false
   MINIMAX_API_KEY=...
   PORTKEY_BASE_URL=http://localhost:8787
   PORTKEY_PROVIDER=minimax
   ```

2. Start Portkey so the gateway can reach
   `${PORTKEY_BASE_URL}/v1/chat/completions`.

3. Send one controlled request through FastAPI and inspect:

   - Portkey accepts the configured Minimax provider or config.
   - The upstream response includes OpenAI-style `usage`.
   - Student `Authorization` credentials are not forwarded upstream.
   - Non-2xx and timeout responses do not increment local token totals.

Do not run broad real-traffic tests until Gate B and Gate C from `TODO.md` are
implemented.

## Dashboard Access

The dashboard is a dense operator console for the seven seeded student records.
It polls `/api/users`, which proxies to FastAPI `/admin/users`. Mutation routes
reset rate windows, lock or unlock keys, and apply token deltas through the
dashboard server so `POIESIS_ADMIN_TOKEN` stays server-side.

Admin and dashboard surfaces identify students with stable `student_id` values
and short `key_preview` strings. They do not expose raw student bearer keys in
dashboard JSON, URLs, Redis rate-limit identities, or forwarding metadata.
Chat requests return an `x-request-id` response header. Operators may supply a
safe `x-request-id` or `x-correlation-id`; otherwise FastAPI generates one and
uses it in logs and Portkey metadata.

FastAPI admin routes fail closed when `POIESIS_ADMIN_TOKEN` is missing. The only
way to run admin routes without a token is to set `ALLOW_INSECURE_ADMIN=true`,
which is for isolated development only.

## Verification Commands

Run the checks that match the area you changed:

```bash
python -m py_compile \
  gateway-brain/app/__init__.py \
  gateway-brain/app/config.py \
  gateway-brain/app/database.py \
  gateway-brain/app/limiter.py \
  gateway-brain/app/main.py \
  gateway-brain/seed_club.py
```

```bash
cd gateway-brain
python -m pip install -r requirements-dev.txt
python -m pytest
```

```bash
cd dashboard
npm ci
npm run lint
npm run build
npm audit --omit=dev --audit-level=moderate
```

```bash
docker compose config
docker compose up --build
```

Full local Gate A readiness also requires a successful dry-run chat request and
a local spam check that proves the burst limiter blocks the third standard-tier
request inside 60 seconds.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `GET /health` fails or hangs | Confirm Redis is running and `REDIS_URL` points at the reachable host. |
| Dashboard shows a gateway alert | Confirm `GATEWAY_BRAIN_URL` points at FastAPI and `POIESIS_ADMIN_TOKEN` matches the backend. |
| Chat returns HTTP 401 | Confirm the client sends `Authorization: Bearer sk-poiesis-...` and that the key was seeded and is active. |
| Chat returns HTTP 403 | The monthly token ceiling has been reached and the key is locked. |
| Chat returns HTTP 429 | The five-hour or burst window is exhausted. Wait for the reset window or use an admin reset in local testing. |
| `stream: true` returns HTTP 400 | Streaming is disabled until streaming usage accounting is implemented. |
| Real upstream returns HTTP 503 | Set `MINIMAX_API_KEY` or `PORTKEY_UPSTREAM_API_KEY` and confirm Portkey is reachable. |
| Token totals do not move in real mode | Confirm the upstream response has OpenAI-compatible `usage.total_tokens`. Missing-usage handling is a pre-release hardening item. |

## Release Gates

Before real club traffic, complete the gates tracked in `TODO.md`:

- Gate A: dry-run local system with compose, seed job, dashboard, dry-run chat,
  and local spam block.
- Gate B: security baseline for admin auth, raw key handling, Redis key names,
  and request correlation IDs.
- Gate C: quota integrity for atomic multi-window limits, missing usage, races,
  and immediate monthly lockout.
- Gate D: one controlled real Portkey/Minimax probe.
- Gate E: operator runbook for start, verify, monitor, reset, lock, rotate, and
  recover procedures.

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app import database
from app.config import Settings, get_settings
from app.limiter import LimitStatus, SlidingWindowLimiter


STUDENT_KEY_PREFIX = "sk-poiesis-"
VALID_CHAT_ROLES = {"system", "user", "assistant", "tool"}
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
CHAT_COMPLETIONS_ROUTE = "/v1/chat/completions"
logger = logging.getLogger("poiesis.gateway")


class ResetWindowPayload(BaseModel):
    tier: Literal["standard", "high-speed", "all"] = "standard"
    include_burst: bool = True


class TokenAdjustmentPayload(BaseModel):
    delta_tokens: int = Field(..., description="Negative restores allowance; positive records extra spend.")


class ReactivatePayload(BaseModel):
    is_active: bool = True


def get_app_settings() -> Settings:
    return get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.poiesis_admin_token and not settings.allow_insecure_admin:
        raise RuntimeError(
            "POIESIS_ADMIN_TOKEN is required unless ALLOW_INSECURE_ADMIN=true."
        )
    database.initialize_database(settings.database_path, settings.poiesis_key_hash_secret)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.redis = redis
    app.state.limiter = SlidingWindowLimiter(redis)
    try:
        yield
    finally:
        await redis.aclose()


app = FastAPI(
    title="PoiesisPathfinder Gateway Brain",
    description="Local AI governance middleware for Portkey-backed Minimax access.",
    version="0.1.0",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def error_detail(message: str, *, key_preview: str | None = None, **fields: Any) -> dict[str, Any]:
    detail: dict[str, Any] = {"message": message}
    if key_preview:
        detail["key_preview"] = key_preview
    for key, value in fields.items():
        if value is not None:
            detail[key] = value
    return detail


def normalize_error_detail(detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        if "message" in detail:
            return detail
        return {"message": str(detail)}
    return error_detail(str(detail))


def request_id_from_headers(request: Request) -> str:
    for header_name in ("x-request-id", "x-correlation-id"):
        raw_request_id = request.headers.get(header_name)
        if raw_request_id:
            request_id = raw_request_id.strip()
            if REQUEST_ID_PATTERN.fullmatch(request_id):
                return request_id
    return f"req-{uuid.uuid4().hex}"


@app.exception_handler(HTTPException)
async def structured_http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    headers = dict(exc.headers or {})
    if hasattr(request.state, "request_id"):
        headers.setdefault("x-request-id", request.state.request_id)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": normalize_error_detail(exc.detail)},
        headers=headers,
    )


def limiter_from_state(request: Request) -> SlidingWindowLimiter:
    return request.app.state.limiter


def parse_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail=error_detail("Missing Authorization bearer token."))
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail=error_detail("Expected Authorization: Bearer <student_key>."))
    token = token.strip()
    if not token.startswith(STUDENT_KEY_PREFIX) or token == STUDENT_KEY_PREFIX:
        raise HTTPException(
            status_code=401,
            detail=error_detail("Expected Authorization: Bearer sk-poiesis-..."),
        )
    return token


def require_admin(
    x_admin_token: Annotated[str | None, Header(alias="x-admin-token")] = None,
    settings: Settings = Depends(get_app_settings),
) -> None:
    if settings.allow_insecure_admin and not settings.poiesis_admin_token:
        return
    if not settings.poiesis_admin_token:
        raise HTTPException(
            status_code=503,
            detail=error_detail("Admin authentication is not configured."),
        )
    if x_admin_token != settings.poiesis_admin_token:
        raise HTTPException(status_code=401, detail=error_detail("Missing or invalid admin token."))


def parse_usage_total_tokens(payload: dict[str, Any]) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    if isinstance(usage.get("total_tokens"), int):
        return max(int(usage["total_tokens"]), 0)
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return None
    return max(prompt_tokens + completion_tokens, 0)


def usage_total_tokens(payload: dict[str, Any]) -> int:
    token_total = parse_usage_total_tokens(payload)
    if token_total is None:
        return 0
    return token_total


def chat_model_from_payload(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    model = payload.get("model")
    if isinstance(model, str) and model.strip():
        return model
    return None


def audit_error_class(exc: HTTPException) -> str:
    detail = normalize_error_detail(exc.detail)
    error_class = detail.get("error_class")
    if isinstance(error_class, str) and error_class:
        return error_class
    scope = detail.get("scope")
    if isinstance(scope, str) and scope:
        return f"rate_limit_{scope}"
    message = str(detail.get("message", "")).lower()
    if "monthly token budget" in message:
        return "monthly_budget_spent"
    if "streaming is disabled" in message:
        return "streaming_disabled"
    if "rate limiter unavailable" in message:
        return "rate_limiter_unavailable"
    if "model is blocked" in message:
        return "model_blocked"
    if "model is not allowed" in message:
        return "model_not_allowed"
    if "request body exceeds" in message or "exceeds the limit" in message:
        return "request_too_large"
    if exc.status_code == 401:
        return "unauthorized"
    if exc.status_code == 400:
        return "bad_request"
    return f"http_{exc.status_code}"


def record_audit_event_safe(
    settings: Settings,
    *,
    request_id: str,
    student_id: str | None,
    key_preview: str | None,
    token_delta: int,
    route: str,
    model: str | None,
    status_code: int,
    error_class: str | None,
    upstream_latency_ms: int | None = None,
) -> None:
    try:
        database.record_audit_event(
            settings.database_path,
            request_id=request_id,
            student_id=student_id,
            key_preview=key_preview,
            token_delta=token_delta,
            route=route,
            model=model,
            status_code=status_code,
            error_class=error_class,
            upstream_latency_ms=upstream_latency_ms,
        )
    except Exception:
        logger.exception("audit log write failed request_id=%s student_id=%s", request_id, student_id)


async def read_limited_json_body(
    request: Request,
    settings: Settings,
    *,
    key_preview: str | None = None,
) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = 0
        if declared_size > settings.max_request_body_bytes:
            raise HTTPException(
                status_code=413,
                detail=error_detail(
                    f"Request body exceeds {settings.max_request_body_bytes} bytes.",
                    key_preview=key_preview,
                ),
            )

    body = await request.body()
    if len(body) > settings.max_request_body_bytes:
        raise HTTPException(
            status_code=413,
            detail=error_detail(
                f"Request body exceeds {settings.max_request_body_bytes} bytes.",
                key_preview=key_preview,
            ),
        )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=error_detail("Request body must be valid JSON.", key_preview=key_preview),
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail=error_detail("Request body must be a JSON object.", key_preview=key_preview),
        )
    validate_chat_payload(payload, settings, key_preview=key_preview)
    return payload


def validate_chat_payload(
    payload: dict[str, Any],
    settings: Settings,
    *,
    key_preview: str | None = None,
) -> None:
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(
            status_code=400,
            detail=error_detail(
                "Request body must include a non-empty string model.",
                key_preview=key_preview,
            ),
        )

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(
            status_code=400,
            detail=error_detail(
                "Request body must include a non-empty messages list.",
                key_preview=key_preview,
            ),
        )
    if len(messages) > settings.max_messages:
        raise HTTPException(
            status_code=413,
            detail=error_detail(
                f"messages exceeds the limit of {settings.max_messages}.",
                key_preview=key_preview,
                limit=settings.max_messages,
            ),
        )

    total_content_chars = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise HTTPException(
                status_code=400,
                detail=error_detail(f"messages[{index}] must be an object.", key_preview=key_preview),
            )

        role = message.get("role")
        if not isinstance(role, str) or role not in VALID_CHAT_ROLES:
            raise HTTPException(
                status_code=400,
                detail=error_detail(
                    f"messages[{index}].role must be one of: assistant, system, tool, user.",
                    key_preview=key_preview,
                ),
            )

        content = message.get("content")
        content_chars = chat_content_size(content, index, key_preview=key_preview)
        if content_chars > settings.max_message_content_chars:
            raise HTTPException(
                status_code=413,
                detail=error_detail(
                    f"messages[{index}].content exceeds {settings.max_message_content_chars} characters.",
                    key_preview=key_preview,
                    limit=settings.max_message_content_chars,
                ),
            )
        total_content_chars += content_chars

    if total_content_chars > settings.max_total_message_content_chars:
        raise HTTPException(
            status_code=413,
            detail=error_detail(
                f"messages content exceeds {settings.max_total_message_content_chars} total characters.",
                key_preview=key_preview,
                limit=settings.max_total_message_content_chars,
            ),
        )


def enforce_model_policy(
    *,
    settings: Settings,
    student_id: str,
    model: str,
    key_preview: str,
) -> None:
    blocked_models = settings.blocked_models_for_student(student_id)
    if model in blocked_models:
        raise HTTPException(
            status_code=403,
            detail=error_detail(
                "Model is blocked for this student.",
                key_preview=key_preview,
                model=model,
                error_class="model_blocked",
            ),
        )

    allowed_models = settings.allowed_models_for_student(student_id)
    if allowed_models and model not in allowed_models:
        raise HTTPException(
            status_code=403,
            detail=error_detail(
                "Model is not allowed for this student.",
                key_preview=key_preview,
                model=model,
                allowed_models=sorted(allowed_models),
                error_class="model_not_allowed",
            ),
        )


def prompt_fingerprint(payload: dict[str, Any]) -> str:
    prompt_material = json.dumps(
        payload.get("messages", []),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(prompt_material.encode("utf-8")).hexdigest()


def record_semantic_loop_signal(
    settings: Settings,
    *,
    request_id: str,
    student_id: str,
    key_preview: str,
    model: str | None,
    payload: dict[str, Any],
) -> None:
    if not settings.semantic_loop_detection_enabled:
        return
    threshold = max(settings.semantic_loop_repeat_threshold, 2)
    repeat_count = database.record_prompt_fingerprint(
        settings.database_path,
        student_id=student_id,
        prompt_hash=prompt_fingerprint(payload),
        request_id=request_id,
        model=model,
    )
    if repeat_count < threshold:
        return
    record_audit_event_safe(
        settings,
        request_id=request_id,
        student_id=student_id,
        key_preview=key_preview,
        token_delta=0,
        route=CHAT_COMPLETIONS_ROUTE,
        model=model,
        status_code=200,
        error_class="semantic_loop_detected",
    )


def prometheus_label_value(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metric_line(name: str, value: int | float, labels: dict[str, Any] | None = None) -> str:
    if not labels:
        return f"{name} {value}"
    label_text = ",".join(
        f'{key}="{prometheus_label_value(label_value)}"'
        for key, label_value in sorted(labels.items())
    )
    return f"{name}{{{label_text}}} {value}"


def render_prometheus_metrics(snapshot: dict[str, Any]) -> str:
    lines = [
        "# HELP poiesis_requests_total Audit-log-backed request count.",
        "# TYPE poiesis_requests_total counter",
    ]
    for row in snapshot["requests"]:
        lines.append(
            metric_line(
                "poiesis_requests_total",
                int(row["count"]),
                {
                    "route": row["route"],
                    "status_code": row["status_code"],
                    "error_class": row["error_class"] or "none",
                },
            )
        )

    lines.extend(
        [
            "# HELP poiesis_request_errors_total Audit-log-backed request error count.",
            "# TYPE poiesis_request_errors_total counter",
        ]
    )
    for row in snapshot["errors"]:
        lines.append(
            metric_line(
                "poiesis_request_errors_total",
                int(row["count"]),
                {"error_class": row["error_class"]},
            )
        )

    total_count = int(snapshot["total_audit_count"])
    error_count = int(snapshot["error_count"])
    error_ratio = round(error_count / total_count, 6) if total_count else 0
    lines.extend(
        [
            "# HELP poiesis_request_error_ratio Audit-log-backed error ratio.",
            "# TYPE poiesis_request_error_ratio gauge",
            metric_line("poiesis_request_error_ratio", error_ratio),
            "# HELP poiesis_tokens_total Positive token usage by student and model.",
            "# TYPE poiesis_tokens_total counter",
        ]
    )
    for row in snapshot["tokens"]:
        lines.append(
            metric_line(
                "poiesis_tokens_total",
                int(row["total_tokens"] or 0),
                {"student_id": row["student_id"] or "unknown", "model": row["model"] or "unknown"},
            )
        )

    lines.extend(
        [
            "# HELP poiesis_upstream_latency_seconds Upstream latency summary.",
            "# TYPE poiesis_upstream_latency_seconds summary",
        ]
    )
    for row in snapshot["latencies"]:
        labels = {"model": row["model"] or "unknown", "status_code": row["status_code"]}
        lines.append(
            metric_line(
                "poiesis_upstream_latency_seconds_count",
                int(row["count"]),
                labels,
            )
        )
        lines.append(
            metric_line(
                "poiesis_upstream_latency_seconds_sum",
                round(float(row["sum_ms"] or 0) / 1000, 6),
                labels,
            )
        )
        lines.append(
            metric_line(
                "poiesis_upstream_latency_seconds_max",
                round(float(row["max_ms"] or 0) / 1000, 6),
                labels,
            )
        )

    lines.extend(
        [
            "# HELP poiesis_students_active Active student key count.",
            "# TYPE poiesis_students_active gauge",
            metric_line("poiesis_students_active", int(snapshot["active_student_count"])),
            "# HELP poiesis_students_inactive Inactive student key count.",
            "# TYPE poiesis_students_inactive gauge",
            metric_line("poiesis_students_inactive", int(snapshot["inactive_student_count"])),
        ]
    )
    return "\n".join(lines) + "\n"


CSV_EXPORT_FIELDS = [
    "student_id",
    "student_name",
    "key_preview",
    "status",
    "total_tokens_consumed",
    "tokens_remaining",
    "audit_event_count",
    "successful_request_count",
    "incident_count",
    "audited_positive_tokens",
    "latest_incident",
    "last_activity_at",
]


def render_student_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def chat_content_size(content: Any, message_index: int, *, key_preview: str | None = None) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        for part_index, part in enumerate(content):
            if not isinstance(part, dict):
                raise HTTPException(
                    status_code=400,
                    detail=error_detail(
                        f"messages[{message_index}].content[{part_index}] must be an object.",
                        key_preview=key_preview,
                    ),
                )
        return len(json.dumps(content, separators=(",", ":"), ensure_ascii=False))
    raise HTTPException(
        status_code=400,
        detail=error_detail(
            f"messages[{message_index}].content must be a string, list, or null.",
            key_preview=key_preview,
        ),
    )


def estimate_prompt_tokens(payload: dict[str, Any]) -> int:
    messages = payload.get("messages", [])
    raw_text = json.dumps(messages, separators=(",", ":"))
    return max(len(raw_text) // 4, 1)


def dry_run_response(payload: dict[str, Any]) -> dict[str, Any]:
    prompt_tokens = estimate_prompt_tokens(payload)
    completion_tokens = 9
    return {
        "id": f"chatcmpl-dryrun-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model", "dry-run-minimax"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Dry-run response from PoiesisPathfinder.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def enforce_rate_limits(
    *,
    limiter: SlidingWindowLimiter,
    settings: Settings,
    identity: str,
    key_preview: str | None = None,
    tier: str,
) -> None:
    try:
        long_window, burst_window = await limiter.check_and_record_window_and_burst(
            identity=identity,
            tier=tier,
            window_seconds=settings.rate_window_seconds,
            window_limit=settings.long_window_limit_for(tier),
            burst_seconds=settings.burst_window_seconds,
            burst_limit=settings.burst_limit_for(tier),
        )
    except RedisError as exc:
        raise HTTPException(
            status_code=503,
            detail=error_detail(
                "Rate limiter unavailable; request was not forwarded.",
                key_preview=key_preview,
            ),
        ) from exc
    if not long_window.allowed:
        raise_rate_limit_error(long_window, key_preview=key_preview)

    if not burst_window.allowed:
        raise_rate_limit_error(burst_window, key_preview=key_preview)


def raise_rate_limit_error(status: LimitStatus, *, key_preview: str | None = None) -> None:
    label = "5-hour rolling window" if status.scope == "window" else "burst window"
    raise HTTPException(
        status_code=429,
        detail=error_detail(
            f"Rate limit exceeded. Too many requests in the {label}.",
            key_preview=key_preview,
            tier=status.tier,
            scope=status.scope,
            limit=status.limit,
            count=status.count,
            remaining=status.remaining,
            reset_after_seconds=status.reset_after_seconds,
        ),
    )


async def forward_to_portkey(
    *,
    payload: dict[str, Any],
    settings: Settings,
    student_id: str,
    key_preview: str,
    request_id: str,
    tier: str,
) -> httpx.Response:
    if not settings.portkey_upstream_api_key:
        raise HTTPException(
            status_code=503,
            detail=error_detail(
                "Missing MINIMAX_API_KEY or PORTKEY_UPSTREAM_API_KEY for Portkey forwarding.",
                key_preview=key_preview,
            ),
        )

    headers = {
        "Authorization": f"Bearer {settings.portkey_upstream_api_key}",
        "Content-Type": "application/json",
        "x-portkey-provider": settings.portkey_provider,
        "x-portkey-metadata": json.dumps(
            {
                "student_id": student_id,
                "key_preview": key_preview,
                "request_id": request_id,
                "tier": tier,
            },
            separators=(",", ":"),
        ),
    }
    if settings.portkey_config:
        headers["x-portkey-config"] = settings.portkey_config

    target = f"{settings.portkey_base_url.rstrip('/')}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        return await client.post(target, headers=headers, json=payload)


async def build_user_metrics(
    *,
    user: dict[str, Any],
    limiter: SlidingWindowLimiter,
    settings: Settings,
) -> dict[str, Any]:
    student_id = user["student_id"]
    standard_window = await limiter.peek(
        identity=student_id,
        tier="standard",
        scope="window",
        window_seconds=settings.rate_window_seconds,
        limit=settings.standard_window_limit,
    )
    high_speed_window = await limiter.peek(
        identity=student_id,
        tier="high-speed",
        scope="window",
        window_seconds=settings.rate_window_seconds,
        limit=settings.high_speed_window_limit,
    )
    standard_burst = await limiter.peek(
        identity=student_id,
        tier="standard",
        scope="burst",
        window_seconds=settings.burst_window_seconds,
        limit=settings.standard_burst_limit,
    )
    high_speed_burst = await limiter.peek(
        identity=student_id,
        tier="high-speed",
        scope="burst",
        window_seconds=settings.burst_window_seconds,
        limit=settings.high_speed_burst_limit,
    )
    consumed = int(user["total_tokens_consumed"])
    ceiling = settings.monthly_token_ceiling
    return {
        **user,
        "monthly_token_ceiling": ceiling,
        "tokens_remaining": max(ceiling - consumed, 0),
        "token_percent_used": round(min(consumed / ceiling, 1) * 100, 4) if ceiling else 0,
        "standard_window_count": standard_window.count,
        "standard_requests_remaining": standard_window.remaining,
        "standard_burst_remaining": standard_burst.remaining,
        "high_speed_window_count": high_speed_window.count,
        "high_speed_requests_remaining": high_speed_window.remaining,
        "high_speed_burst_remaining": high_speed_burst.remaining,
        "rate_window_seconds": settings.rate_window_seconds,
        "burst_window_seconds": settings.burst_window_seconds,
    }


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "PoiesisPathfinder gateway-brain", "status": "ready"}


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    redis: Redis = request.app.state.redis
    redis_ok = await redis.ping()
    return {"ok": True, "redis": bool(redis_ok)}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    settings: Settings = Depends(get_app_settings),
    limiter: SlidingWindowLimiter = Depends(limiter_from_state),
) -> Response:
    request_id = request_id_from_headers(request)
    request.state.request_id = request_id
    virtual_key = parse_bearer_token(authorization)
    user = database.get_user_by_virtual_key(
        settings.database_path,
        virtual_key,
        settings.poiesis_key_hash_secret,
    )
    if user is None:
        raise HTTPException(status_code=401, detail=error_detail("Unknown or inactive virtual key."))

    student_id = str(user["student_id"])
    key_preview = str(user["key_preview"])
    logger.info(
        "chat request accepted request_id=%s student_id=%s key_preview=%s",
        request_id,
        student_id,
        key_preview,
    )
    payload: dict[str, Any] | None = None
    model: str | None = None
    token_delta = 0
    upstream_latency_ms: int | None = None
    try:
        if not user["is_active"]:
            if int(user["total_tokens_consumed"]) >= settings.monthly_token_ceiling:
                raise HTTPException(
                    status_code=403,
                    detail=error_detail("Monthly token budget spent.", key_preview=key_preview),
                )
            raise HTTPException(
                status_code=401,
                detail=error_detail("Unknown or inactive virtual key.", key_preview=key_preview),
            )

        if database.deactivate_if_spent(settings.database_path, student_id, settings.monthly_token_ceiling):
            raise HTTPException(
                status_code=403,
                detail=error_detail("Monthly token budget spent.", key_preview=key_preview),
            )

        payload = await read_limited_json_body(request, settings, key_preview=key_preview)
        model = chat_model_from_payload(payload)
        enforce_model_policy(
            settings=settings,
            student_id=student_id,
            model=str(model),
            key_preview=key_preview,
        )
        if payload.get("stream") is True and not settings.allow_streaming:
            raise HTTPException(
                status_code=400,
                detail=error_detail(
                    "Streaming is disabled because usage accounting requires the final OpenAI usage block.",
                    key_preview=key_preview,
                ),
            )

        tier = settings.request_tier_for_student(student_id)
        payload = dict(payload)
        payload.pop("poiesis_tier", None)

        await enforce_rate_limits(
            limiter=limiter,
            settings=settings,
            identity=student_id,
            key_preview=key_preview,
            tier=tier,
        )
        record_semantic_loop_signal(
            settings,
            request_id=request_id,
            student_id=student_id,
            key_preview=key_preview,
            model=model,
            payload=payload,
        )

        if settings.dry_run_upstream:
            response_payload = dry_run_response(payload)
            token_delta = usage_total_tokens(response_payload)
            if token_delta:
                database.record_token_usage(
                    settings.database_path,
                    student_id,
                    token_delta,
                    settings.monthly_token_ceiling,
                )
            record_audit_event_safe(
                settings,
                request_id=request_id,
                student_id=student_id,
                key_preview=key_preview,
                token_delta=token_delta,
                route=CHAT_COMPLETIONS_ROUTE,
                model=model,
                status_code=200,
                error_class=None,
            )
            logger.info(
                "chat request completed request_id=%s student_id=%s status_code=200 dry_run=true",
                request_id,
                student_id,
            )
            return JSONResponse(response_payload, headers={"x-request-id": request_id})

        try:
            upstream_started_at = time.perf_counter()
            upstream_response = await forward_to_portkey(
                payload=payload,
                settings=settings,
                student_id=student_id,
                key_preview=key_preview,
                request_id=request_id,
                tier=tier,
            )
            upstream_latency_ms = max(int((time.perf_counter() - upstream_started_at) * 1000), 0)
        except httpx.TimeoutException as exc:
            upstream_latency_ms = max(int((time.perf_counter() - upstream_started_at) * 1000), 0)
            logger.warning(
                "upstream timeout request_id=%s student_id=%s",
                request_id,
                student_id,
            )
            raise HTTPException(
                status_code=504,
                detail=error_detail(
                    "Upstream request timed out; token usage was not updated.",
                    key_preview=key_preview,
                    error_class="upstream_timeout",
                ),
            ) from exc
        except httpx.RequestError as exc:
            upstream_latency_ms = max(int((time.perf_counter() - upstream_started_at) * 1000), 0)
            logger.warning(
                "upstream request error request_id=%s student_id=%s error=%s",
                request_id,
                student_id,
                exc.__class__.__name__,
            )
            raise HTTPException(
                status_code=502,
                detail=error_detail(
                    "Upstream request failed; token usage was not updated.",
                    key_preview=key_preview,
                    error_class="upstream_request_error",
                ),
            ) from exc

        content_type = upstream_response.headers.get("content-type", "application/json")
        error_class = None
        if upstream_response.status_code < 400:
            if "application/json" not in content_type:
                raise HTTPException(
                    status_code=502,
                    detail=error_detail(
                        "Upstream response did not include usable usage; token usage was not updated.",
                        key_preview=key_preview,
                        error_class="missing_usage",
                    ),
                )
            try:
                response_payload = upstream_response.json()
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=error_detail(
                        "Upstream response did not include usable usage; token usage was not updated.",
                        key_preview=key_preview,
                        error_class="missing_usage",
                    ),
                ) from exc
            parsed_delta = parse_usage_total_tokens(response_payload)
            if parsed_delta is None:
                raise HTTPException(
                    status_code=502,
                    detail=error_detail(
                        "Upstream response did not include usable usage; token usage was not updated.",
                        key_preview=key_preview,
                        error_class="missing_usage",
                    ),
                )
            token_delta = parsed_delta
            if token_delta:
                database.record_token_usage(
                    settings.database_path,
                    student_id,
                    token_delta,
                    settings.monthly_token_ceiling,
                )
        else:
            error_class = "upstream_http_error"

        record_audit_event_safe(
            settings,
            request_id=request_id,
            student_id=student_id,
            key_preview=key_preview,
            token_delta=token_delta,
            route=CHAT_COMPLETIONS_ROUTE,
            model=model,
            status_code=upstream_response.status_code,
            error_class=error_class,
            upstream_latency_ms=upstream_latency_ms,
        )
        logger.info(
            "chat request completed request_id=%s student_id=%s status_code=%s dry_run=false",
            request_id,
            student_id,
            upstream_response.status_code,
        )
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            media_type=content_type,
            headers={"x-request-id": request_id},
        )
    except HTTPException as exc:
        record_audit_event_safe(
            settings,
            request_id=request_id,
            student_id=student_id,
            key_preview=key_preview,
            token_delta=token_delta,
            route=CHAT_COMPLETIONS_ROUTE,
            model=model,
            status_code=exc.status_code,
            error_class=audit_error_class(exc),
            upstream_latency_ms=upstream_latency_ms,
        )
        raise


@app.get("/metrics", dependencies=[Depends(require_admin)])
async def prometheus_metrics(settings: Settings = Depends(get_app_settings)) -> Response:
    snapshot = database.metrics_snapshot(settings.database_path)
    return Response(
        content=render_prometheus_metrics(snapshot),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/admin/users", dependencies=[Depends(require_admin)])
async def admin_users(
    settings: Settings = Depends(get_app_settings),
    limiter: SlidingWindowLimiter = Depends(limiter_from_state),
) -> dict[str, Any]:
    users = database.list_users(settings.database_path)
    metrics = [
        await build_user_metrics(user=user, limiter=limiter, settings=settings)
        for user in users
    ]
    return {
        "users": metrics,
        "monthly_token_ceiling": settings.monthly_token_ceiling,
        "standard_window_limit": settings.standard_window_limit,
        "high_speed_window_limit": settings.high_speed_window_limit,
        "rate_window_seconds": settings.rate_window_seconds,
        "standard_burst_limit": settings.standard_burst_limit,
        "high_speed_burst_limit": settings.high_speed_burst_limit,
        "burst_window_seconds": settings.burst_window_seconds,
    }


@app.get("/admin/audit-log", dependencies=[Depends(require_admin)])
async def admin_audit_log(
    student_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    return {
        "events": database.list_audit_events(
            settings.database_path,
            student_id=student_id,
            limit=limit,
        )
    }


@app.get("/admin/export.csv", dependencies=[Depends(require_admin)])
async def admin_export_csv(settings: Settings = Depends(get_app_settings)) -> Response:
    rows = database.student_export_rows(
        settings.database_path,
        monthly_token_ceiling=settings.monthly_token_ceiling,
    )
    return Response(
        content=render_student_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="poiesispathfinder-students.csv"'},
    )


@app.post("/admin/users/{student_id}/reset-window", dependencies=[Depends(require_admin)])
async def reset_user_window(
    student_id: str,
    payload: ResetWindowPayload,
    settings: Settings = Depends(get_app_settings),
    limiter: SlidingWindowLimiter = Depends(limiter_from_state),
) -> dict[str, Any]:
    user = database.get_user_by_student_id(settings.database_path, student_id)
    if user is None:
        raise HTTPException(status_code=404, detail=error_detail("Unknown student ID."))
    tiers = ["standard", "high-speed"] if payload.tier == "all" else [payload.tier]
    deleted = 0
    for tier in tiers:
        deleted += await limiter.reset(
            identity=student_id,
            tier=tier,
            include_burst=payload.include_burst,
        )
    return {"ok": True, "student_id": student_id, "key_preview": user["key_preview"], "deleted_keys": deleted}


@app.post("/admin/users/{student_id}/adjust-tokens", dependencies=[Depends(require_admin)])
async def adjust_tokens(
    request: Request,
    student_id: str,
    payload: TokenAdjustmentPayload,
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    request_id = request_id_from_headers(request)
    request.state.request_id = request_id
    user = database.get_user_by_student_id(settings.database_path, student_id)
    if user is None:
        record_audit_event_safe(
            settings,
            request_id=request_id,
            student_id=student_id,
            key_preview=None,
            token_delta=0,
            route=f"/admin/users/{student_id}/adjust-tokens",
            model=None,
            status_code=404,
            error_class="unknown_student",
        )
        raise HTTPException(status_code=404, detail=error_detail("Unknown student ID."))
    total = database.increment_tokens(settings.database_path, student_id, payload.delta_tokens)
    record_audit_event_safe(
        settings,
        request_id=request_id,
        student_id=student_id,
        key_preview=str(user["key_preview"]),
        token_delta=payload.delta_tokens,
        route=f"/admin/users/{student_id}/adjust-tokens",
        model=None,
        status_code=200,
        error_class=None,
    )
    return {
        "ok": True,
        "student_id": student_id,
        "key_preview": user["key_preview"],
        "total_tokens_consumed": total,
        "tokens_remaining": max(settings.monthly_token_ceiling - total, 0),
    }


@app.post("/admin/users/{student_id}/active", dependencies=[Depends(require_admin)])
async def set_user_active(
    request: Request,
    student_id: str,
    payload: ReactivatePayload,
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    request_id = request_id_from_headers(request)
    request.state.request_id = request_id
    user = database.get_user_by_student_id(settings.database_path, student_id)
    if user is None:
        record_audit_event_safe(
            settings,
            request_id=request_id,
            student_id=student_id,
            key_preview=None,
            token_delta=0,
            route=f"/admin/users/{student_id}/active",
            model=None,
            status_code=404,
            error_class="unknown_student",
        )
        raise HTTPException(status_code=404, detail=error_detail("Unknown student ID."))
    database.set_active(settings.database_path, student_id, payload.is_active)
    record_audit_event_safe(
        settings,
        request_id=request_id,
        student_id=student_id,
        key_preview=str(user["key_preview"]),
        token_delta=0,
        route=f"/admin/users/{student_id}/active",
        model=None,
        status_code=200,
        error_class="admin_unlock" if payload.is_active else "admin_lock",
    )
    return {"ok": True, "student_id": student_id, "key_preview": user["key_preview"], "is_active": payload.is_active}

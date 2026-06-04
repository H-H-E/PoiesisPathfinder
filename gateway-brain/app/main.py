from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app import database
from app.config import Settings, get_settings
from app.limiter import LimitStatus, SlidingWindowLimiter


STUDENT_KEY_PREFIX = "sk-poiesis-"


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


def limiter_from_state(request: Request) -> SlidingWindowLimiter:
    return request.app.state.limiter


def parse_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization bearer token.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Expected Authorization: Bearer <student_key>.")
    token = token.strip()
    if not token.startswith(STUDENT_KEY_PREFIX) or token == STUDENT_KEY_PREFIX:
        raise HTTPException(
            status_code=401,
            detail="Expected Authorization: Bearer sk-poiesis-...",
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
            detail="Admin authentication is not configured.",
        )
    if x_admin_token != settings.poiesis_admin_token:
        raise HTTPException(status_code=401, detail="Missing or invalid admin token.")


def infer_request_tier(request: Request, payload: dict[str, Any]) -> Literal["standard", "high-speed"]:
    raw_tier = (
        request.headers.get("x-poiesis-tier")
        or request.headers.get("x-request-tier")
        or str(payload.get("poiesis_tier", "standard"))
    )
    normalized = raw_tier.strip().lower().replace("_", "-")
    if normalized in {"high-speed", "highspeed", "fast"}:
        return "high-speed"
    return "standard"


def usage_total_tokens(payload: dict[str, Any]) -> int:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0
    if isinstance(usage.get("total_tokens"), int):
        return max(int(usage["total_tokens"]), 0)
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        return 0
    return max(prompt_tokens + completion_tokens, 0)


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
            detail="Rate limiter unavailable; request was not forwarded.",
        ) from exc
    if not long_window.allowed:
        raise_rate_limit_error(long_window)

    if not burst_window.allowed:
        raise_rate_limit_error(burst_window)


def raise_rate_limit_error(status: LimitStatus) -> None:
    label = "5-hour rolling window" if status.scope == "window" else "burst window"
    raise HTTPException(
        status_code=429,
        detail={
            "message": f"Rate limit exceeded. Too many requests in the {label}.",
            "tier": status.tier,
            "scope": status.scope,
            "limit": status.limit,
            "count": status.count,
            "remaining": status.remaining,
            "reset_after_seconds": status.reset_after_seconds,
        },
    )


async def forward_to_portkey(
    *,
    payload: dict[str, Any],
    settings: Settings,
    student_id: str,
    key_preview: str,
    tier: str,
) -> httpx.Response:
    if not settings.portkey_upstream_api_key:
        raise HTTPException(
            status_code=503,
            detail="Missing MINIMAX_API_KEY or PORTKEY_UPSTREAM_API_KEY for Portkey forwarding.",
        )

    headers = {
        "Authorization": f"Bearer {settings.portkey_upstream_api_key}",
        "Content-Type": "application/json",
        "x-portkey-provider": settings.portkey_provider,
        "x-portkey-metadata": json.dumps(
            {
                "student_id": student_id,
                "key_preview": key_preview,
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
    virtual_key = parse_bearer_token(authorization)
    user = database.get_user_by_virtual_key(
        settings.database_path,
        virtual_key,
        settings.poiesis_key_hash_secret,
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Unknown or inactive virtual key.")

    student_id = str(user["student_id"])
    if not user["is_active"]:
        if int(user["total_tokens_consumed"]) >= settings.monthly_token_ceiling:
            raise HTTPException(status_code=403, detail="Monthly token budget spent.")
        raise HTTPException(status_code=401, detail="Unknown or inactive virtual key.")

    if database.deactivate_if_spent(settings.database_path, student_id, settings.monthly_token_ceiling):
        raise HTTPException(status_code=403, detail="Monthly token budget spent.")

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    if payload.get("stream") is True and not settings.allow_streaming:
        raise HTTPException(
            status_code=400,
            detail="Streaming is disabled because usage accounting requires the final OpenAI usage block.",
        )

    tier = infer_request_tier(request, payload)
    payload = dict(payload)
    payload.pop("poiesis_tier", None)

    await enforce_rate_limits(
        limiter=limiter,
        settings=settings,
        identity=student_id,
        tier=tier,
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
        return JSONResponse(response_payload)

    upstream_response = await forward_to_portkey(
        payload=payload,
        settings=settings,
        student_id=student_id,
        key_preview=str(user["key_preview"]),
        tier=tier,
    )

    content_type = upstream_response.headers.get("content-type", "application/json")
    if upstream_response.status_code < 400 and "application/json" in content_type:
        try:
            response_payload = upstream_response.json()
        except json.JSONDecodeError:
            response_payload = {}
        token_delta = usage_total_tokens(response_payload)
        if token_delta:
            database.record_token_usage(
                settings.database_path,
                student_id,
                token_delta,
                settings.monthly_token_ceiling,
            )

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=content_type,
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


@app.post("/admin/users/{student_id}/reset-window", dependencies=[Depends(require_admin)])
async def reset_user_window(
    student_id: str,
    payload: ResetWindowPayload,
    settings: Settings = Depends(get_app_settings),
    limiter: SlidingWindowLimiter = Depends(limiter_from_state),
) -> dict[str, Any]:
    user = database.get_user_by_student_id(settings.database_path, student_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Unknown student ID.")
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
    student_id: str,
    payload: TokenAdjustmentPayload,
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    user = database.get_user_by_student_id(settings.database_path, student_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Unknown student ID.")
    total = database.increment_tokens(settings.database_path, student_id, payload.delta_tokens)
    return {
        "ok": True,
        "student_id": student_id,
        "key_preview": user["key_preview"],
        "total_tokens_consumed": total,
        "tokens_remaining": max(settings.monthly_token_ceiling - total, 0),
    }


@app.post("/admin/users/{student_id}/active", dependencies=[Depends(require_admin)])
async def set_user_active(
    student_id: str,
    payload: ReactivatePayload,
    settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    user = database.get_user_by_student_id(settings.database_path, student_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Unknown student ID.")
    database.set_active(settings.database_path, student_id, payload.is_active)
    return {"ok": True, "student_id": student_id, "key_preview": user["key_preview"], "is_active": payload.is_active}

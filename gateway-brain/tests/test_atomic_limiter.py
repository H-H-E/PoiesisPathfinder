from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.limiter import SlidingWindowLimiter
from app.main import enforce_rate_limits


TEST_REDIS_URL = os.getenv("POIESIS_TEST_REDIS_URL", "redis://localhost:6379/15")


async def _redis_or_skip() -> Redis:
    redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await redis.ping()
    except Exception as exc:  # pragma: no cover - depends on local Redis availability.
        await redis.aclose()
        pytest.skip(f"Redis is not available at {TEST_REDIS_URL}: {exc}")
    return redis


async def _cleanup(limiter: SlidingWindowLimiter, identity: str) -> None:
    await limiter.reset(identity=identity, tier="standard", include_burst=True)
    await limiter.reset(identity=identity, tier="high-speed", include_burst=True)


async def _record_until_blocked(
    *,
    limiter: SlidingWindowLimiter,
    settings: Settings,
    identity: str,
    tier: str,
    attempts: int,
) -> list[tuple[int, str | None]]:
    results: list[tuple[int, str | None]] = []
    for _ in range(attempts):
        try:
            await enforce_rate_limits(
                limiter=limiter,
                settings=settings,
                identity=identity,
                tier=tier,
            )
        except HTTPException as exc:
            results.append((exc.status_code, exc.detail.get("scope")))
        else:
            results.append((200, None))
    return results


def test_burst_rejection_does_not_consume_long_window_quota() -> None:
    async def scenario() -> None:
        redis = await _redis_or_skip()
        limiter = SlidingWindowLimiter(redis)
        identity = f"stu-test-{uuid.uuid4().hex}"
        settings = Settings(
            _env_file=None,
            rate_window_seconds=300,
            standard_window_limit=10,
            burst_window_seconds=300,
            standard_burst_limit=2,
        )
        try:
            await enforce_rate_limits(
                limiter=limiter,
                settings=settings,
                identity=identity,
                tier="standard",
            )
            await enforce_rate_limits(
                limiter=limiter,
                settings=settings,
                identity=identity,
                tier="standard",
            )
            with pytest.raises(HTTPException) as exc_info:
                await enforce_rate_limits(
                    limiter=limiter,
                    settings=settings,
                    identity=identity,
                    tier="standard",
                )
            assert exc_info.value.status_code == 429
            assert exc_info.value.detail["scope"] == "burst"

            window = await limiter.peek(
                identity=identity,
                tier="standard",
                scope="window",
                window_seconds=settings.rate_window_seconds,
                limit=settings.standard_window_limit,
            )
            burst = await limiter.peek(
                identity=identity,
                tier="standard",
                scope="burst",
                window_seconds=settings.burst_window_seconds,
                limit=settings.standard_burst_limit,
            )
            assert window.count == 2
            assert burst.count == 2
            window_ttl = await redis.ttl(limiter._key(identity, "standard", "window"))
            burst_ttl = await redis.ttl(limiter._key(identity, "standard", "burst"))
            assert 0 < window_ttl <= settings.rate_window_seconds + 60
            assert 0 < burst_ttl <= settings.burst_window_seconds + 60
        finally:
            await _cleanup(limiter, identity)
            await redis.aclose()

    asyncio.run(scenario())


def test_redis_unavailability_fails_closed_with_503() -> None:
    class FailingLimiter:
        async def check_and_record_window_and_burst(self, **_: object) -> object:
            raise RedisError("simulated Redis outage")

    settings = Settings(_env_file=None)

    async def scenario() -> None:
        with pytest.raises(HTTPException) as exc_info:
            await enforce_rate_limits(
                limiter=FailingLimiter(),  # type: ignore[arg-type]
                settings=settings,
                identity="stu-test",
                tier="standard",
            )
        assert exc_info.value.status_code == 503
        assert "not forwarded" in exc_info.value.detail["message"]

    asyncio.run(scenario())


def test_standard_five_hour_window_blocks_643rd_default_request() -> None:
    async def scenario() -> None:
        redis = await _redis_or_skip()
        limiter = SlidingWindowLimiter(redis)
        identity = f"stu-test-{uuid.uuid4().hex}"
        settings = Settings(_env_file=None, standard_burst_limit=1_000)
        try:
            results = await _record_until_blocked(
                limiter=limiter,
                settings=settings,
                identity=identity,
                tier="standard",
                attempts=settings.standard_window_limit + 1,
            )

            assert results[: settings.standard_window_limit] == [(200, None)] * settings.standard_window_limit
            assert results[-1] == (429, "window")

            window = await limiter.peek(
                identity=identity,
                tier="standard",
                scope="window",
                window_seconds=settings.rate_window_seconds,
                limit=settings.standard_window_limit,
            )
            assert window.count == settings.standard_window_limit
            assert window.limit == 642
            assert window.window_seconds == 18_000
        finally:
            await _cleanup(limiter, identity)
            await redis.aclose()

    asyncio.run(scenario())


def test_high_speed_five_hour_window_blocks_322nd_default_request() -> None:
    async def scenario() -> None:
        redis = await _redis_or_skip()
        limiter = SlidingWindowLimiter(redis)
        identity = f"stu-test-{uuid.uuid4().hex}"
        settings = Settings(_env_file=None, high_speed_burst_limit=1_000)
        try:
            results = await _record_until_blocked(
                limiter=limiter,
                settings=settings,
                identity=identity,
                tier="high-speed",
                attempts=settings.high_speed_window_limit + 1,
            )

            assert results[: settings.high_speed_window_limit] == [(200, None)] * settings.high_speed_window_limit
            assert results[-1] == (429, "window")

            window = await limiter.peek(
                identity=identity,
                tier="high-speed",
                scope="window",
                window_seconds=settings.rate_window_seconds,
                limit=settings.high_speed_window_limit,
            )
            assert window.count == settings.high_speed_window_limit
            assert window.limit == 321
            assert window.window_seconds == 18_000
        finally:
            await _cleanup(limiter, identity)
            await redis.aclose()

    asyncio.run(scenario())


def test_default_burst_guardrails_block_standard_third_and_high_speed_second_requests() -> None:
    async def scenario() -> None:
        redis = await _redis_or_skip()
        limiter = SlidingWindowLimiter(redis)
        standard_identity = f"stu-test-standard-{uuid.uuid4().hex}"
        high_speed_identity = f"stu-test-high-speed-{uuid.uuid4().hex}"
        settings = Settings(_env_file=None)
        try:
            standard_results = await _record_until_blocked(
                limiter=limiter,
                settings=settings,
                identity=standard_identity,
                tier="standard",
                attempts=3,
            )
            high_speed_results = await _record_until_blocked(
                limiter=limiter,
                settings=settings,
                identity=high_speed_identity,
                tier="high-speed",
                attempts=2,
            )

            assert standard_results == [(200, None), (200, None), (429, "burst")]
            assert high_speed_results == [(200, None), (429, "burst")]

            standard_burst = await limiter.peek(
                identity=standard_identity,
                tier="standard",
                scope="burst",
                window_seconds=settings.burst_window_seconds,
                limit=settings.standard_burst_limit,
            )
            high_speed_burst = await limiter.peek(
                identity=high_speed_identity,
                tier="high-speed",
                scope="burst",
                window_seconds=settings.burst_window_seconds,
                limit=settings.high_speed_burst_limit,
            )
            assert standard_burst.limit == 2
            assert standard_burst.window_seconds == 60
            assert high_speed_burst.limit == 1
            assert high_speed_burst.window_seconds == 60
        finally:
            await _cleanup(limiter, standard_identity)
            await _cleanup(limiter, high_speed_identity)
            await redis.aclose()

    asyncio.run(scenario())


def test_concurrent_requests_cannot_race_past_long_window_quota() -> None:
    async def scenario() -> None:
        redis = await _redis_or_skip()
        limiter = SlidingWindowLimiter(redis)
        identity = f"stu-test-{uuid.uuid4().hex}"
        settings = Settings(
            _env_file=None,
            rate_window_seconds=300,
            standard_window_limit=2,
            burst_window_seconds=300,
            standard_burst_limit=20,
        )

        async def attempt() -> int:
            try:
                await enforce_rate_limits(
                    limiter=limiter,
                    settings=settings,
                    identity=identity,
                    tier="standard",
                )
            except HTTPException as exc:
                return exc.status_code
            return 200

        try:
            statuses = await asyncio.gather(*(attempt() for _ in range(8)))
            assert statuses.count(200) == 2
            assert statuses.count(429) == 6

            window = await limiter.peek(
                identity=identity,
                tier="standard",
                scope="window",
                window_seconds=settings.rate_window_seconds,
                limit=settings.standard_window_limit,
            )
            assert window.count == 2
        finally:
            await _cleanup(limiter, identity)
            await redis.aclose()

    asyncio.run(scenario())

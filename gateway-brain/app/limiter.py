from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis


CHECK_AND_RECORD_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)

if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local reset_after = window
    if oldest[2] then
        reset_after = math.max(0, window - (now - tonumber(oldest[2])))
    end
    return {0, count, limit, reset_after}
end

redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window + 60)
return {1, count + 1, limit, 0}
"""

CHECK_AND_RECORD_PAIR_SCRIPT = """
local window_key = KEYS[1]
local burst_key = KEYS[2]
local now = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local window_limit = tonumber(ARGV[3])
local window_member = ARGV[4]
local burst_seconds = tonumber(ARGV[5])
local burst_limit = tonumber(ARGV[6])
local burst_member = ARGV[7]

redis.call('ZREMRANGEBYSCORE', window_key, '-inf', now - window_seconds)
redis.call('ZREMRANGEBYSCORE', burst_key, '-inf', now - burst_seconds)

local window_count = redis.call('ZCARD', window_key)
local burst_count = redis.call('ZCARD', burst_key)

if window_count >= window_limit then
    local oldest = redis.call('ZRANGE', window_key, 0, 0, 'WITHSCORES')
    local reset_after = window_seconds
    if oldest[2] then
        reset_after = math.max(0, window_seconds - (now - tonumber(oldest[2])))
    end
    return {0, 1, window_count, window_limit, reset_after, burst_count, burst_limit, 0}
end

if burst_count >= burst_limit then
    local oldest = redis.call('ZRANGE', burst_key, 0, 0, 'WITHSCORES')
    local reset_after = burst_seconds
    if oldest[2] then
        reset_after = math.max(0, burst_seconds - (now - tonumber(oldest[2])))
    end
    return {0, 2, window_count, window_limit, 0, burst_count, burst_limit, reset_after}
end

redis.call('ZADD', window_key, now, window_member)
redis.call('ZADD', burst_key, now, burst_member)
redis.call('EXPIRE', window_key, window_seconds + 60)
redis.call('EXPIRE', burst_key, burst_seconds + 60)

return {1, 0, window_count + 1, window_limit, 0, burst_count + 1, burst_limit, 0}
"""


@dataclass(frozen=True)
class LimitStatus:
    scope: str
    tier: str
    allowed: bool
    count: int
    limit: int
    remaining: int
    reset_after_seconds: int
    window_seconds: int


class SlidingWindowLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @staticmethod
    def _key(identity: str, tier: str, scope: str) -> str:
        return f"poiesis:rate:{tier}:{scope}:{identity}"

    async def check_and_record(
        self,
        *,
        identity: str,
        tier: str,
        scope: str,
        window_seconds: int,
        limit: int,
    ) -> LimitStatus:
        now = time.time()
        member = f"{now:.6f}:{uuid.uuid4().hex}"
        result = await self.redis.eval(
            CHECK_AND_RECORD_SCRIPT,
            1,
            self._key(identity, tier, scope),
            now,
            window_seconds,
            limit,
            member,
        )
        allowed = bool(int(result[0]))
        count = int(result[1])
        reset_after = int(float(result[3]))
        return LimitStatus(
            scope=scope,
            tier=tier,
            allowed=allowed,
            count=count,
            limit=limit,
            remaining=max(limit - count, 0),
            reset_after_seconds=reset_after,
            window_seconds=window_seconds,
        )

    async def check_and_record_window_and_burst(
        self,
        *,
        identity: str,
        tier: str,
        window_seconds: int,
        window_limit: int,
        burst_seconds: int,
        burst_limit: int,
    ) -> tuple[LimitStatus, LimitStatus]:
        now = time.time()
        window_member = f"{now:.6f}:window:{uuid.uuid4().hex}"
        burst_member = f"{now:.6f}:burst:{uuid.uuid4().hex}"
        result = await self.redis.eval(
            CHECK_AND_RECORD_PAIR_SCRIPT,
            2,
            self._key(identity, tier, "window"),
            self._key(identity, tier, "burst"),
            now,
            window_seconds,
            window_limit,
            window_member,
            burst_seconds,
            burst_limit,
            burst_member,
        )
        allowed = bool(int(result[0]))
        blocked_scope = int(result[1])
        window_count = int(result[2])
        window_reset_after = int(float(result[4]))
        burst_count = int(result[5])
        burst_reset_after = int(float(result[7]))
        window_allowed = allowed or blocked_scope != 1
        burst_allowed = allowed or blocked_scope != 2
        return (
            LimitStatus(
                scope="window",
                tier=tier,
                allowed=window_allowed,
                count=window_count,
                limit=window_limit,
                remaining=max(window_limit - window_count, 0),
                reset_after_seconds=window_reset_after,
                window_seconds=window_seconds,
            ),
            LimitStatus(
                scope="burst",
                tier=tier,
                allowed=burst_allowed,
                count=burst_count,
                limit=burst_limit,
                remaining=max(burst_limit - burst_count, 0),
                reset_after_seconds=burst_reset_after,
                window_seconds=burst_seconds,
            ),
        )

    async def peek(
        self,
        *,
        identity: str,
        tier: str,
        scope: str,
        window_seconds: int,
        limit: int,
    ) -> LimitStatus:
        key = self._key(identity, tier, scope)
        now = time.time()
        await self.redis.zremrangebyscore(key, "-inf", now - window_seconds)
        count = int(await self.redis.zcard(key))
        reset_after = 0
        if count >= limit:
            oldest = await self.redis.zrange(key, 0, 0, withscores=True)
            if oldest:
                reset_after = max(0, int(window_seconds - (now - float(oldest[0][1]))))
        return LimitStatus(
            scope=scope,
            tier=tier,
            allowed=count < limit,
            count=count,
            limit=limit,
            remaining=max(limit - count, 0),
            reset_after_seconds=reset_after,
            window_seconds=window_seconds,
        )

    async def reset(self, *, identity: str, tier: str, include_burst: bool = True) -> int:
        scopes = ["window"]
        if include_burst:
            scopes.append("burst")
        keys = [self._key(identity, tier, scope) for scope in scopes]
        if not keys:
            return 0
        return int(await self.redis.delete(*keys))

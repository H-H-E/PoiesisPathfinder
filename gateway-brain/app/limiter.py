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
    def _key(virtual_key: str, tier: str, scope: str) -> str:
        return f"poiesis:rate:{tier}:{scope}:{virtual_key}"

    async def check_and_record(
        self,
        *,
        virtual_key: str,
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
            self._key(virtual_key, tier, scope),
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

    async def peek(
        self,
        *,
        virtual_key: str,
        tier: str,
        scope: str,
        window_seconds: int,
        limit: int,
    ) -> LimitStatus:
        key = self._key(virtual_key, tier, scope)
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

    async def reset(self, *, virtual_key: str, tier: str, include_burst: bool = True) -> int:
        scopes = ["window"]
        if include_burst:
            scopes.append("burst")
        keys = [self._key(virtual_key, tier, scope) for scope in scopes]
        if not keys:
            return 0
        return int(await self.redis.delete(*keys))

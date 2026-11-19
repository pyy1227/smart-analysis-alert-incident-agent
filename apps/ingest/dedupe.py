"""告警去重和限流。"""
from apps.common.redis_client import redis_client


async def is_duplicate(
        fingerprint: str,
        service_id: str,
        environment: str,
        status: str = "firing",
        window: int = 300,
) -> bool:
    # 触发和恢复分开去重。
    key = f"ingest:dedupe:{environment}:{service_id}:{fingerprint}:{status}"
    ok = await redis_client.set(key, "1", ex=window, nx=True)
    return not bool(ok)


async def rate_limit(service_id: str, window: int = 60, limit: int = 100) -> bool:
    key = f"ingest:rate:{service_id}:{window}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, window)
    return count <= limit

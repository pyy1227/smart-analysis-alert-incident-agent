"""异步 Redis 客户端。"""

import redis.asyncio as redis

from apps.common.config import settings

redis_client = redis.Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    db=0,
    protocol=2,
    decode_responses=True,
)

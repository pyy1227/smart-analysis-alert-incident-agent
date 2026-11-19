"""用 Redis 记录告警恢复和复发状态。"""

import json
from apps.common.redis_client import redis_client


LIFECYCLE_TTL_SECONDS = 24 * 60 * 60


async def enrich_alert_lifecycle(alert) -> dict:
    key = f"ingest:lifecycle:{alert.environment}:{alert.service_id}:{alert.fingerprint}"
    raw = await redis_client.get(key)
    try:
        state = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        state = {}

    previous = state.get("status")
    changed = bool(previous and previous != alert.status.value)
    recurrence = previous == "resolved" and alert.status.value == "firing"
    lifecycle = {
        "previous_status": previous,
        "status_changed": changed,
        "recurrence_after_recovery": recurrence,
    }
    await redis_client.set(
        key,
        json.dumps({"status": alert.status.value}),
        ex=LIFECYCLE_TTL_SECONDS,
    )
    alert.annotations = {**alert.annotations, "lifecycle": lifecycle}
    return lifecycle

"""审批动作执行后的简易验证与失败处理。"""

import math

from apps.worker.metrics import ACTION_VERIFICATIONS
from apps.worker.tools import query_service_metrics, query_service_status


def _metric_value(payload: dict, name: str) -> float | None:
    metric = payload.get("metrics", {}).get(name, {})
    rows = metric.get("data", {}).get("result", [])
    for row in rows:
        value = row.get("value") if isinstance(row, dict) else None
        try:
            number = float(value[1])
        except (IndexError, TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


async def verify_execution(action: dict, result: dict) -> dict:
    if result.get("status") != "success":
        verification = {
            "status": "execution_failed",
            "recovered": False,
            "next_step": "确认后重新创建操作记录；不要复用原幂等键",
        }
        ACTION_VERIFICATIONS.labels(status=verification["status"]).inc()
        return verification

    step = action.get("step")
    service_id = action.get("params", {}).get("service_id")
    if step == "restart_service" and service_id:
        status = await query_service_status(service_id)
        containers = status.get("containers", [])
        recovered = bool(containers) and all(
            item.get("running") is True and item.get("health_status") != "unhealthy"
            for item in containers
        )
        verification = {
            "status": "recovered" if recovered else "not_recovered",
            "recovered": recovered,
            "check": "container_health",
        }
    elif step == "scale_request_concurrency" and service_id:
        metrics = await query_service_metrics(service_id)
        waiting = _metric_value(metrics, "request_concurrency_waiting")
        recovered = waiting is not None and waiting <= 0
        verification = {
            "status": "recovered" if recovered else "not_recovered",
            "recovered": recovered,
            "check": "request_concurrency_waiting",
            "observed_value": waiting,
        }
    else:
        verification = {
            "status": "completed_unverified",
            "recovered": False,
            "check": "manual",
        }
    ACTION_VERIFICATIONS.labels(status=verification["status"]).inc()
    return verification


def status_after_execution(result: dict) -> str:
    if result.get("status") != "success":
        return "execution_failed"
    return "resolved" if result.get("verification", {}).get("recovered") else "executed"

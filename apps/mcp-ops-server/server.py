"""MCP 执行层：提供只读诊断工具，以及需要审批与幂等键的受控写操作。

只读工具直接查询本地 Docker Compose、Prometheus 和 PostgreSQL；写操作只允许
`restart_service` 与 `scale_request_concurrency` 两类步骤，且必须携带
approval_id 与 idempotency_key，服务号还要在 MANAGED_SERVICES 白名单内。
"""

from collections.abc import Callable
import json
from mcp.server.fastmcp import FastMCP
from apps.common.config import settings
from apps.common.db import Approval, Incident, active_db
from apps.common.redis_client import redis_client
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import desc, select
from apps.common.docker_adapters import (
    DockerQueryError,
    query_service_logs as docker_query_service_logs,
    query_service_status as docker_query_service_status,
)
from apps.common.prometheus_adapter import (
    PrometheusQueryError,
    query_database_health as prometheus_query_database_health,
    query_service_metrics as prometheus_query_service_metrics,
)
import httpx
import os
import subprocess
from pathlib import Path
import time
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[2]

mcp = FastMCP(
    "incident-ops",
    host=os.getenv("MCP_BIND_HOST", "127.0.0.1"),
    port=8931,
)

ALLOWED_STEPS = {
    "scale_request_concurrency",
    "restart_service",
}

ALLOWED_SERVICES = {service.strip() for service in settings.managed_services.split(",") if service.strip()}

SERVICE_DOCKER_MAP = {
    "payment-api": {
        "compose_service": "payment-api",
    },
    "incident-api": {
        "compose_service": "incident-api",
    },
    "prometheus": {
        "compose_service": "prometheus",
    },
    "alertmanager": {
        "compose_service": "alertmanager",
    },
}

_IDEMPOTENCY_PREFIX = "mcp:write:idempotency:"
_IDEMPOTENCY_PROCESSING_TTL_SECONDS = 300
_COMPLETE_IDEMPOTENCY_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('set', KEYS[1], ARGV[2])
end
return false
"""
_RELEASE_IDEMPOTENCY_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _redis_idempotency_key(idempotency_key: str) -> str:
    return f"{_IDEMPOTENCY_PREFIX}{idempotency_key}"


async def _begin_idempotent_operation(
        idempotency_key: str,
) -> tuple[str | None, dict | None]:
    """通过 Redis SET NX 原子占用幂等键。"""
    if not idempotency_key:
        return None, {"status": "rejected", "reason": "缺失幂等键值"}

    token = uuid.uuid4().hex
    try:
        acquired = await redis_client.set(
            _redis_idempotency_key(idempotency_key),
            token,
            nx=True,
            ex=_IDEMPOTENCY_PROCESSING_TTL_SECONDS,
        )
    except RedisError:
        return None, {
            "status": "rejected",
            "reason": "Redis 幂等存储不可用，拒绝执行写操作",
            "idempotency_key": idempotency_key,
        }

    if not acquired:
        return None, {
            "status": "rejected",
            "reason": "幂等键已存在，拒绝重复执行",
            "idempotency_key": idempotency_key,
        }
    return token, None


async def _store_idempotency_result(
        idempotency_key: str,
        token: str,
        result: dict,
) -> bool:
    payload = json.dumps(
        {"state": "completed", "result": result},
        ensure_ascii=False,
        default=str,
    )
    stored = await redis_client.eval(
        _COMPLETE_IDEMPOTENCY_SCRIPT,
        1,
        _redis_idempotency_key(idempotency_key),
        token,
        payload,
    )
    return bool(stored)


async def _release_idempotency_key(idempotency_key: str, token: str) -> None:
    await redis_client.eval(
        _RELEASE_IDEMPOTENCY_SCRIPT,
        1,
        _redis_idempotency_key(idempotency_key),
        token,
    )


async def _run_idempotent_operation(
        idempotency_key: str,
        operation: Callable[[], dict],
) -> dict:
    token, rejected = await _begin_idempotent_operation(idempotency_key)
    if rejected:
        return rejected
    assert token is not None

    try:
        result = operation()
    except Exception as error:
        # 无法确认副作用时保留幂等键，避免重复执行。
        unknown = {
            "status": "failed",
            "reason": f"操作结果未知：{type(error).__name__}",
            "idempotency_key": idempotency_key,
        }
        try:
            await _store_idempotency_result(idempotency_key, token, unknown)
        except RedisError:
            pass
        raise

    result = {**result, "idempotency_key": idempotency_key}
    try:
        if result.get("status") == "rejected":
            await _release_idempotency_key(idempotency_key, token)
        elif not await _store_idempotency_result(idempotency_key, token, result):
            result["idempotency_warning"] = "幂等占用已失效，执行结果未能持久化"
    except RedisError:
        result["idempotency_warning"] = "执行完成，但 Redis 幂等结果写入失败"
    return result


async def _validate_execution(
        db: AsyncSession,
        step: str,
        params: dict,
        service_id: str,
        approval_id: str,
        idempotency_key: str,
) -> dict | None:
    if service_id not in ALLOWED_SERVICES:
        return {"status": "rejected", "reason": "服务ID不在允许列表中"}

    if not approval_id:
        return {"status": "rejected", "reason": "缺少操作确认 ID"}

    if not idempotency_key:
        return {"status": "rejected", "reason": "缺失幂等键值"}

    approval = await db.get(Approval, approval_id)
    if not approval:
        return {"status": "rejected", "reason": "操作确认记录不存在"}

    incident = await db.get(Incident, approval.incident_id)
    if not incident:
        return {"status": "rejected", "reason": "事件不存在"}

    if approval.status != "approved":
        return {"status": "rejected", "reason": "操作尚未确认"}

    if incident.service_id != service_id:
        return {"status": "rejected", "reason": "服务ID不匹配"}

    approved_action = approval.action or {}
    if approved_action.get("type") != "execute_runbook_step":
        return {"status": "rejected", "reason": "确认记录不是运行手册操作"}
    if approved_action.get("step") != step:
        return {"status": "rejected", "reason": "确认步骤不匹配"}

    # 执行参数必须和确认时一致。
    approved_params = approved_action.get("params")
    if not isinstance(approved_params, dict) or approved_params != params:
        return {"status": "rejected", "reason": "确认参数不匹配"}

    return None


@mcp.tool()
def query_service_status(service_id: str) -> dict:
    """只读查询 Docker Compose 服务的所有容器。ok 表示采集成功，不代表业务健康。"""
    try:
        if service_id not in ALLOWED_SERVICES:
            raise DockerQueryError(f"不允许查询服务：{service_id}")
        return docker_query_service_status(service_id)
    except DockerQueryError as error:
        return {"service_id": service_id, "status": "unavailable", "source": "docker", "error": str(error), "containers": []}


@mcp.tool()
def query_service_metrics(service_id: str) -> dict:
    """查询本地 Prometheus 中的请求量、错误率、P99 和请求并发指标。"""
    try:
        return prometheus_query_service_metrics(service_id)
    except PrometheusQueryError as error:
        return {"service_id": service_id, "source": "prometheus", "error": str(error)}


@mcp.tool()
def query_database_health(service_id: str) -> dict:
    """查询数据库导出的连接数和慢查询监控指标，不直接连接数据库。"""
    try:
        return prometheus_query_database_health(service_id)
    except PrometheusQueryError as error:
        return {"service_id": service_id, "source": "prometheus/application-db-pool",
                "status": "unavailable", "error": str(error)}


@mcp.tool()
def query_logs(service_id: str, query: str = "") -> dict:
    """只读查询本地 Compose 服务最近 30 分钟的 Docker 日志。"""
    try:
        if service_id not in ALLOWED_SERVICES:
            raise DockerQueryError(f"不允许查询服务：{service_id}")
        return docker_query_service_logs(service_id, query)
    except DockerQueryError as error:
        return {"service_id": service_id, "source": "docker-logs", "status": "unavailable",
                "error": str(error), "results": []}


@mcp.tool()
async def search_incident_history(
        service_id: str,
        environment: str,
        classification: str,
        exclude_incident_id: str,
        tenant_id: str,
        query: str = "",
) -> dict:
    """查询同租户、同服务、同环境和同分类的真实历史事件。"""
    if service_id not in ALLOWED_SERVICES:
        return {"service_id": service_id, "source": "postgres/incidents",
                "status": "rejected", "error": "服务ID不在允许列表中", "results": []}
    if classification not in {"database", "network", "application", "unknown"}:
        return {"service_id": service_id, "source": "postgres/incidents",
                "status": "rejected", "error": "无效的事件分类", "results": []}
    if not environment or not exclude_incident_id or not tenant_id:
        return {"service_id": service_id, "source": "postgres/incidents",
                "status": "rejected", "error": "历史范围不完整", "results": []}
    async with active_db.session() as session:
        rows = (await session.scalars(
            select(Incident).where(
                Incident.tenant_id == tenant_id,
                Incident.service_id == service_id,
                Incident.environment == environment,
                Incident.classification == classification,
                Incident.id != exclude_incident_id,
                Incident.status.in_(("analyzed", "awaiting_approval", "executed", "execution_failed")),
            ).order_by(desc(Incident.updated_at), desc(Incident.created_at)).limit(5)
        )).all()
    results = [{
        "incident_id": incident.id,
        "title": incident.title,
        "classification": incident.classification,
        "root_cause_analysis": (incident.report or "")[:4000],
        "proposed_action": incident.proposed_action,
        "approval_status": incident.approval_status,
        "execution_result": incident.execution_result,
        "final_status": incident.status,
        "occurred_at": incident.created_at.isoformat() if incident.created_at else None,
    } for incident in rows]
    return {
        "service_id": service_id,
        "source": "postgres/incidents",
        "status": "returned" if results else "no_data",
        "scope": {"tenant_id": tenant_id, "environment": environment,
                  "classification": classification, "excluded_incident_id": exclude_incident_id},
        "query": query[:1000],
        "results": results,
    }


def _execute_runbook_operation(
        step: str,
        params: dict,
) -> dict:
    """执行已确认的运行手册操作。"""
    service_id = params["service_id"]
    if step == "scale_request_concurrency":
        if service_id not in {"payment-api", "payment_api"}:
            return {
                "step": step,
                "status": "rejected",
                "reason": "当前仅支持 payment-api 服务",
            }

        try:
            target_limit = int(params["concurrency_limit"])
        except (KeyError, TypeError, ValueError):
            return {
                "step": step,
                "status": "rejected",
                "reason": "concurrency_limit 必须是一个整数，范围在 1 到 200 之间",
            }

        if not 1 <= target_limit <= 200:
            return {
                "step": step,
                "status": "rejected",
                "reason": "concurrency_limit 必须是一个整数，范围在 1 到 200 之间",
            }

        payment_api_url = settings.payment_api_url.rstrip("/")
        try:
            with httpx.Client(timeout=5.0, trust_env=False) as client:
                current_response = client.get(f"{payment_api_url}/concurrency")
                current_response.raise_for_status()
                current_limit = int(current_response.json()["current_limit"])

                if target_limit == current_limit:
                    return {
                        "step": step,
                        "service_id": "payment-api",
                        "status": "success",
                        "executor": "mcp",
                        "action": "no_change",
                        "api_result": {"current_limit": current_limit},
                    }

                action = "expand" if target_limit > current_limit else "shrink"
                endpoint = (
                    "/concurrency/expand"
                    if action == "expand"
                    else "/concurrency/shrink"
                )
                response = client.post(
                    f"{payment_api_url}{endpoint}",
                    json={"concurrency_limit": target_limit},
                )
                response.raise_for_status()
                api_result = response.json()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            return {
                "step": step,
                "service_id": "payment-api",
                "status": "failed",
                "reason": str(error),
            }

        return {
            "step": step,
            "service_id": "payment-api",
            "status": "success",
            "executor": "mcp",
            "action": action,
            "api_result": api_result,
        }

    if step == "restart_service":
        result = _restart_service_docker(service_id)
        result["step"] = step
        return result

    return {"step": step, "status": "unknown_step", "reason": "未知执行步骤"}


@mcp.tool()
async def execute_runbook_step(
        step: str,
        params: dict,
        approval_id: str,
        idempotency_key: str,
) -> dict:
    """执行已确认的运行手册步骤。"""
    if step not in ALLOWED_STEPS:
        return {"status": "rejected", "reason": f"不允许执行步骤：{step}"}

    if not isinstance(params, dict):
        return {"status": "rejected", "reason": "参数必须是对象类型"}

    service_id = params.get("service_id")
    if not isinstance(service_id, str) or not service_id:
        return {"status": "rejected", "reason": "缺少服务ID"}

    async with active_db.session() as session:
        rejected = await _validate_execution(
            session,
            step,
            params,
            service_id,
            approval_id,
            idempotency_key,
        )
    if rejected:
        return rejected

    return await _run_idempotent_operation(
        idempotency_key,
        lambda: _execute_runbook_operation(step, params),
    )


def _restart_service_docker(service_id):
    config = SERVICE_DOCKER_MAP.get(service_id)

    if not config:
        return {
            "status": "rejected",
            "reason": f"未配置服务的 Docker 映射：{service_id}",
        }

    compose_file = PROJECT_ROOT / "docker-compose.yml"
    compose_service = config["compose_service"]

    try:
        result = subprocess.run(
            [
                "docker", "compose",
                "-f", str(compose_file),
                "restart", compose_service,
            ],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {
            "service_id": service_id,
            "status": "failed",
            "reason": "Docker Compose 重启超时",
        }
    except subprocess.CalledProcessError as error:
        return {
            "service_id": service_id,
            "status": "failed",
            "reason": error.stderr.strip() or error.stdout.strip(),
        }

    container_health_urls = {
        "payment-api": "http://payment-api:8001/status",
        "incident-api": "http://incident-api:8000/health",
        "prometheus": "http://prometheus:9090/-/ready",
        "alertmanager": "http://alertmanager:9093/-/ready",
    }
    health_url = container_health_urls[service_id]
    last_error = ""

    for _ in range(15):
        try:
            response = httpx.get(
                health_url,
                timeout=3.0,
                trust_env=False,
            )
            if 200 <= response.status_code < 300:
                return {
                    "service_id": service_id,
                    "status": "success",
                    "executor": "docker-compose",
                    "health_url": health_url,
                    "docker_output": result.stdout.strip(),
                }
            last_error = f"健康检查返回状态码 {response.status_code}"

        except httpx.HTTPError as error:
            last_error = str(error)

        time.sleep(2)



    return {
        "service_id": service_id,
        "status": "failed",
        "reason": f"容器重启后健康检查失败：{last_error}",
        "health_url": health_url,
    }



if __name__ == "__main__":
    mcp.run(transport="streamable-http")

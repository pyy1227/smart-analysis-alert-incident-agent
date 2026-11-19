"""读取本地 Prometheus 指标。"""

import httpx

from apps.common.config import settings


class PrometheusQueryError(RuntimeError):
    """Prometheus 未配置、不可用或返回错误。"""

#获取每一个待管理的服务
def _managed_services() -> set[str]:
    return {
        service.strip()
        for service in settings.managed_services.split(",")
        if service.strip()
    }


def assert_managed_service(service_id: str) -> None:
    if service_id not in _managed_services():
        raise PrometheusQueryError(f"不允许查询服务：{service_id}")


def _prometheus_query(expression: str) -> dict:
    if not settings.prometheus_url:
        raise PrometheusQueryError("未配置 PROMETHEUS_URL")

    endpoint = f"{settings.prometheus_url.rstrip('/')}/api/v1/query"
    try:
        # trust_env=False：本地 Prometheus 流量不经过系统代理，与 MCP 客户端保持一致。
        with httpx.Client(timeout=8, trust_env=False) as client:
            response = client.get(endpoint, params={"query": expression}) #query?query=expression
    except httpx.HTTPError as error:
        # Prometheus 停掉时必须转成 PrometheusQueryError：否则异常会穿过 MCP 工具边界，
        # 让整次工具调用失败，模型反而拿不到"指标不可用"这条证据。
        raise PrometheusQueryError(f"Prometheus 不可达：{type(error).__name__}") from error

    if response.status_code >= 400:
        raise PrometheusQueryError(
            f"Prometheus 查询失败：HTTP {response.status_code}"
        )

    payload = response.json()
    if payload.get("status") != "success":
        error = payload.get("error", "未知错误")
        raise PrometheusQueryError(f"Prometheus 查询失败：{error}")
    return payload.get("data", {})



def _query_metrics(service_id: str, expressions: dict[str, str]) -> dict:
    assert_managed_service(service_id)
    return {
        name: {"expression": expression, "data": _prometheus_query(expression)}
        for name, expression in expressions.items()
    }


def query_service_metrics(service_id: str) -> dict:
    """查询服务请求量、错误率、延迟和请求并发指标。"""
    label = settings.prometheus_service_label
    selector = f'{{{label}="{service_id}"}}' #f格式化字符串想要输出大括号必须写两个{{}}，否则会报错
    error_selector = f'{{{label}="{service_id}",status=~"5.."}}'
    expressions = {
        "request_rate_per_second": (
            f"sum(rate(http_server_requests_seconds_count{selector}[5m]))"
        ),
        "error_rate": (
            f"(sum(rate(http_server_requests_seconds_count{error_selector}[5m])) "
            f"or vector(0)) / "
            f"clamp_min(sum(rate(http_server_requests_seconds_count{selector}[5m])), 1)"
        ),
        "p99_latency_seconds": (
            "histogram_quantile(0.99, "
            f"sum(rate(http_server_requests_seconds_bucket{selector}[5m])) by (le))"
        ),
        "request_concurrency_active": (
            f"max(payment_request_concurrency_active{selector})"
        ),
        "request_concurrency_waiting": (
            f"max(payment_request_concurrency_waiting{selector})"
        ),
    }
    return {
        "service_id": service_id,
        "source": "prometheus",
        "metrics": _query_metrics(service_id, expressions),
    }


def query_database_health(service_id: str) -> dict:
    """查询当前服务的 SQLAlchemy/MySQL 连接池指标。"""
    label = settings.prometheus_service_label
    selector = f'{{{label}="{service_id}"}}'
    expressions = {
        "pool_in_use": f"max(payment_db_pool_in_use{selector})",
        "pool_capacity": f"max(payment_db_pool_capacity{selector})",
        "pool_available": f"max(payment_db_pool_available{selector})",
        "pool_overflow": f"max(payment_db_pool_overflow{selector})",
        "pool_operation_p99_seconds": (
            "histogram_quantile(0.99, "
            f"sum(rate(payment_db_operation_seconds_bucket{selector}[5m])) by (le))"
        ),
    }
    return {
        "service_id": service_id,
        "source": "prometheus/application-db-pool",
        "limitations": [
            "指标来自当前服务的 SQLAlchemy 连接池和数据库操作耗时。",
            "指标仅反映 Prometheus 最近一次成功抓取到的样本。",
        ],
        "metrics": _query_metrics(service_id, expressions),
    }

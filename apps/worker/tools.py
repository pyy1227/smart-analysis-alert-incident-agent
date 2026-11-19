"""MCP 工具的统一入口：把工作流选定的动作名映射为具体的 MCP 调用。

- `TOOLS` 是唯一允许调用的工具表，未注册的名字直接抛错，模型无法凭空发明工具；
- `WRITE_TOOLS` 标记有副作用的工具，`execute_tool` 会把审批 ID 与幂等键注入参数，
  调用方不能自行省略；
- 服务白名单、步骤白名单和参数范围在 mcp-ops-server 侧还会再校验一次。
"""

from apps.worker.mcp_client import call_mcp_tool
from apps.common.web_search import search_web_solutions as run_web_search


async def query_service_status(service_id: str, **kwargs):
    """查询服务的容器状态。"""
    return await call_mcp_tool("query_service_status", {"service_id": service_id})


async def query_service_metrics(service_id: str, **kwargs):
    """查询服务的 Prometheus 指标。"""
    return await call_mcp_tool("query_service_metrics", {"service_id": service_id})


async def query_database_health(service_id: str, **kwargs):
    """查询数据库连接池和操作耗时指标。"""
    return await call_mcp_tool("query_database_health", {"service_id": service_id})


async def query_logs(service_id: str, query: str = "", **kwargs):
    """查询服务最近 30 分钟的容器日志。"""
    return await call_mcp_tool("query_logs", {"service_id": service_id, "query": query})


async def search_incident_history(
    service_id: str,
    query: str = "",
    environment: str = "",
    classification: str = "unknown",
    exclude_incident_id: str = "",
    tenant_id: str = "",
    **kwargs,
):
    """检索同一范围内最近五条历史事件。"""
    return await call_mcp_tool(
        "search_incident_history",
        {"service_id": service_id, "query": query, "environment": environment,
         "classification": classification, "exclude_incident_id": exclude_incident_id,
         "tenant_id": tenant_id},
    )


async def search_web_solutions(service_id: str, query: str = "", **kwargs):
    """搜索公开排障资料，结果只作参考。"""
    return await run_web_search(service_id, query)


async def execute_runbook_step(
    step: str,
    params: dict | None = None,
    approval_id: str = "",
    idempotency_key: str = "",
    **kwargs,
):
    """执行已确认的 Runbook 步骤；写操作，必须带审批 ID 与幂等键。"""
    return await call_mcp_tool(
        "execute_runbook_step",
        {
            "step": step,
            "params": params or {},
            "approval_id": approval_id,
            "idempotency_key": idempotency_key,
        },
    )


TOOLS = {
    "query_service_status": query_service_status,
    "query_service_metrics": query_service_metrics,
    "query_database_health": query_database_health,
    "query_logs": query_logs,
    "search_incident_history": search_incident_history,
    "search_web_solutions": search_web_solutions,
    "execute_runbook_step": execute_runbook_step,
}

WRITE_TOOLS = {"execute_runbook_step"}


async def execute_tool(action: dict, approval_id: str, idempotency_key: str):
    tool_name = action.get("type")
    if tool_name not in TOOLS:
        raise ValueError(f"未知工具：{tool_name}")

    params = {k: v for k, v in action.items() if k not in ("type", "requires_approval")}

    if tool_name in WRITE_TOOLS:
        params.update(
            {
                "approval_id": approval_id,
                "idempotency_key": idempotency_key,
            }
        )

    return await TOOLS[tool_name](**params)

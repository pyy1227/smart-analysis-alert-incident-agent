"""评估证据是否充分以及操作风险。"""

import math

from apps.worker.action_proposals import ActionSuggestion


SOURCE_RULES = {
    "query_service_status": ("docker", "direct", "Docker 容器状态", "容器状态不能证明业务健康。"),
    "query_service_metrics": ("prometheus", "monitoring", "Prometheus 指标", "只反映已埋点且已采集的指标；空指标不等于零。"),
    "query_database_health": (
        ("prometheus/application-db-pool", "prometheus/database-exporter"),
        "monitoring", "数据库监控指标",
        "应用连接池指标不能代替 MySQL 实例级健康检查。",
    ),
    "query_logs": (
        ("log-gateway", "docker-logs"), "gateway", "日志查询",
        "只覆盖日志网关或本地容器返回的有限时间范围，不代表全部日志。",
    ),
    "search_incident_history": (
        "postgres/incidents", "historical", "PostgreSQL 历史事件",
        "真实历史事件仅用于类比分析，不能证明当前事件状态或直接支持写操作。",
    ),
    "search_web_solutions": (
        "tavily/web", "reference", "联网解决方案",
        "公开网页结果未经当前环境验证，只能作为排障参考。",
    ),
}


def _metric_has_values(metric):
    if not isinstance(metric, dict) or not isinstance(metric.get("data"), dict):
        return False
    rows = metric["data"].get("result", [])
    if not isinstance(rows, list):
        return False
    for row in rows:
        value = row.get("value") if isinstance(row, dict) else None
        if not isinstance(value, list) or len(value) != 2 or isinstance(value[1], bool):
            continue
        try:
            if math.isfinite(float(value[1])):
                return True
        except (TypeError, ValueError, OverflowError):
            pass
    return False


def describe_evidence(item: dict, service_id: str) -> dict:
    """根据工具登记信息判断证据来源。"""
    tool = item.get("tool")
    expected, kind, label, limitation = SOURCE_RULES.get(
        tool, (None, "unknown", "未知数据源", "来源未验证。"),
    )
    result = item.get("result")
    result = result if isinstance(result, dict) else {}
    gaps = []
    usable = (
        kind not in {"historical", "reference", "unknown"} and item.get("status") == "returned"
        and result.get("source") in (expected if isinstance(expected, tuple) else (expected,))
        and result.get("service_id") == service_id
        and item.get("arguments", {}).get("service_id") == service_id
        and not result.get("error") and not result.get("truncated")
        and result.get("status") not in {"failed", "unavailable", "rejected", "not_found"}
    )
    if kind == "historical":
        scoped = (
            result.get("source") in (expected if isinstance(expected, tuple) else (expected,))
            and result.get("service_id") == service_id
            and item.get("arguments", {}).get("service_id") == service_id
            and isinstance(result.get("results"), list)
        )
        gaps.append("历史事件来自真实数据库，但仅作类比，不能证明当前事件状态" if scoped
                    else "历史事件结果来源不符或服务不匹配")
    elif kind == "reference":
        scoped = (
            result.get("source") == expected
            and result.get("service_id") == service_id
            and item.get("arguments", {}).get("service_id") == service_id
            and isinstance(result.get("results"), list)
        )
        gaps.append(
            "联网结果只作参考，不能单独支持修改操作"
            if scoped else "联网搜索结果来源不符或服务不匹配"
        )
        usable = False
    elif not usable:
        gaps.append(f"{tool}：结果不可用、来源不符或服务不匹配")
    elif kind == "monitoring":
        metrics = result.get("metrics")
        # 数据库全局指标不能证明当前服务的状态。
        relevant = {key: value for key, value in metrics.items()
                    if tool != "query_database_health" or key.startswith("pool_")} if isinstance(metrics, dict) else {}
        missing = [key for key, value in relevant.items() if not _metric_has_values(value)]
        usable = any(_metric_has_values(value) for value in relevant.values())
        if missing:
            gaps.append(f"{tool}：以下指标无有效样本：{', '.join(missing)}")
        if not usable:
            gaps.append(f"{tool}：没有当前服务的有效指标样本")
    elif tool == "query_logs":
        logs = result.get("results")
        # 日志网关格式不固定，无法识别时按证据不足处理。
        if isinstance(logs, dict):
            logs = next((logs[key] for key in ("logs", "entries", "results") if isinstance(logs.get(key), list)), None)
        usable = isinstance(logs, list) and bool(logs)
        if not usable:
            gaps.append("日志为空或网关响应结构未识别，不能据此断言没有异常")
    elif tool == "query_service_status":
        containers = result.get("containers")
        usable = isinstance(containers, list) and any(
            isinstance(container, dict) and container.get("service_id") == service_id
            and type(container.get("running")) is bool for container in containers
        )
        if not usable:
            gaps.append("未获得当前服务有效的容器状态")
    return {"kind": kind, "label": label, "usable": bool(usable),
            "limitations": [limitation], "gaps": gaps}


def assess_action_risk(state: dict) -> dict:
    """评估建议操作的风险。"""
    if state.get("recommendation_error"):
        return {"level": "unknown", "action": None, "reason": "结构化建议生成失败，操作风险尚未评估。"}
    try:
        suggestion = ActionSuggestion.model_validate(state.get("suggested_action"))
    except ValueError:
        return {"level": "unknown", "action": None, "reason": "没有有效的结构化操作建议。"}
    levels = {
        "none": ("none", "未建议写操作；这不代表事件或业务没有风险。"),
        "restart_service": ("high", "重启可能中断请求或影响可用性，需要你确认。"),
        "scale_request_concurrency": ("high", "请求并发上限调整会改变应用负载，需要你确认。"),
    }
    level, reason = levels[suggestion.action]
    return {"level": level, "action": suggestion.action, "reason": reason}


def assess_evidence(state: dict) -> dict:
    sources = [{"evidence_id": i, "tool": item.get("tool"),
                "collected_at": item.get("collected_at"),
                **describe_evidence(item, state["service_id"])}
               for i, item in enumerate(state.get("evidence", []))]
    usable_ids = {source["evidence_id"] for source in sources if source["usable"]}
    gaps = [gap for source in sources for gap in source["gaps"]]
    for key in ("agent_error", "recommendation_error", "action_validation_error"):
        if state.get(key):
            gaps.append(state[key])
    try:
        suggestion = ActionSuggestion.model_validate(state.get("suggested_action"))
        gaps.extend(suggestion.missing_information)
        refs = suggestion.evidence_refs
    except ValueError:
        refs = []
        gaps.append("缺少有效的模型操作建议")
    if not usable_ids:
        gaps.append("没有可用于当前事件的真实查询结果")
    proposal_supported = (
        state.get("proposed_action", {}).get("type") == "execute_runbook_step"
        and not state.get("action_validation_error") and not state.get("agent_error")
        and not state.get("recommendation_error") and bool(refs) and set(refs) <= usable_ids
    )
    level = "sufficient_for_proposal" if proposal_supported else "partial" if usable_ids else "insufficient"
    limitations = list(dict.fromkeys(limit for source in sources for limit in source["limitations"]))
    limitations.append("采集时间仅表示本次查询时间；本评估不验证底层样本新鲜度，也不证明根因已查明。")
    if not any(citation.get("kind") != "web" for citation in state.get("citations", [])):
        limitations.append("未检索到运行手册；文档、实时证据和操作风险分别记录。")
    if state.get("retrieval_error"):
        limitations.append(state["retrieval_error"])
    next_step = (
        "重新采集指标、日志和容器状态；仍不足时转人工处理"
        if level == "insufficient" else
        "补齐缺失证据后再决定是否执行" if level == "partial" else
        "确认后执行操作"
    )
    return {"level": level, "scope": "当前事件证据及操作提案校验，不是根因置信度",
            "usable_count": len(usable_ids), "total_count": len(sources),
            "gaps": list(dict.fromkeys(gaps)), "limitations": limitations, "sources": sources,
            "recommended_next_step": next_step}


def render_assessment(evidence: dict, action: dict) -> str:
    evidence_labels = {"insufficient": "证据不足", "partial": "已有部分证据", "sufficient_for_proposal": "满足操作提案校验"}
    risk_labels = {"none": "无建议操作", "unknown": "未评估", "low": "低", "medium": "中", "high": "高"}
    return (
        "\n### 证据与风险\n"
        f"- 证据：{evidence_labels[evidence['level']]}（可用 {evidence['usable_count']}/{evidence['total_count']}）。\n"
        f"- 建议操作风险：{risk_labels[action['level']]}。{action['reason']}\n"
    )

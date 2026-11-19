"""模型只提出建议；可执行提案由后端按已有证据和允许的参数构造。"""

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.common.docker_adapters import SERVICE_DOCKER_TARGETS


NonEmptyText = Annotated[str, Field(min_length=1, max_length=4000)]
EvidenceRef = Annotated[int, Field(strict=True, ge=0)]


class ActionSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    action: Literal["none", "restart_service", "scale_request_concurrency"]
    params: dict
    reason: NonEmptyText
    evidence_refs: list[EvidenceRef] = Field(max_length=6)
    missing_information: list[NonEmptyText] = Field(max_length=10)

    @model_validator(mode="after")
    def validate_params(self):
        expected = {
            "none": set(),
            "restart_service": {"service_id"},
            "scale_request_concurrency": {"service_id", "concurrency_limit"},
        }[self.action]
        if set(self.params) != expected:
            raise ValueError("建议动作的参数缺失或包含未允许的字段")
        if self.action != "none":
            service_id = self.params["service_id"]
            if not isinstance(service_id, str) or not service_id.strip():
                raise ValueError("service_id 必须是非空字符串")
        if self.action == "scale_request_concurrency":
            size = self.params["concurrency_limit"]
            if type(size) is not int or not 1 <= size <= 200:
                raise ValueError("concurrency_limit 必须是 1 到 200 之间的整数")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("证据编号不能重复")
        return self


class AnalysisSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    summary_text: Annotated[str, Field(min_length=1, max_length=12000)]
    suggested_action: ActionSuggestion


ACTION_PROMPT = (
    "仅输出符合以下 JSON Schema 的 JSON 对象，不要 Markdown 代码块，也不要调用工具。"
    "summary_text 是给人阅读的总结，控制在 300 个汉字以内，第一句直接给出结论，"
    "随后只保留影响、关键依据和下一步；suggested_action 是明确的操作建议，不是工具调用。"
    "action 只能是 none、restart_service、scale_request_concurrency。"
    "none 的 params 必须是 {}；restart_service 的 params 只能含 service_id；"
    "scale_request_concurrency 的 params 必须且只能含 service_id 和整数 concurrency_limit（1 到 200）。"
    "service_id 必须与当前事件完全一致。reason 说明依据；evidence_refs 引用证据数组的零起始编号；"
    "missing_information 列出尚缺失且影响决策的信息，不得捏造证据或参数。"
    "明确不建议某操作、条件尚未满足或无法确定参数时，选择 none；"
    "不得因为文本提到重启就选择 restart_service，必须有 Docker 容器已停止或 unhealthy 的证据。"
    "scale_request_concurrency 目前只支持 payment-api 的应用请求并发限制器，"
    "必须有 Prometheus 请求等待指标；它不会调整 SQLAlchemy/MySQL 连接池。"
    "不确定目标 concurrency_limit 时应列出缺失信息并选择 none。"
    "所有写操作都要等待项目所有者确认，不得声称已执行；历史事件、空指标和失败结果不能作为当前操作依据。"
)


def _has_request_waiting(item: dict) -> bool:
    if item.get("tool") != "query_service_metrics":
        return False
    result = item["result"]
    if result.get("source") != "prometheus":
        return False
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return False
    metric = metrics.get("request_concurrency_waiting")
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
            waiting = float(value[1])
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(waiting) and waiting > 0:
            return True
    return False


def _is_usable_evidence(item: dict, service_id: str) -> bool:
    """只允许使用本次真实返回、且属于当前服务的证据。"""
    result = item.get("result")
    return (
        item.get("status") == "returned"
        and isinstance(result, dict)
        and not result.get("error")
        and not result.get("truncated")
        and result.get("status")
        not in {"unavailable", "failed", "rejected", "not_found"}
        and item.get("tool") not in {"search_incident_history", "search_web_solutions"} #排除历史事件和网页搜索，因为它们只能作为参考，不能作为当前环境的直接证据。
        and item.get("arguments", {}).get("service_id") == service_id
        and result.get("service_id") == service_id
    )


def _has_abnormal_container(item: dict, service_id: str) -> bool:
    """判断状态证据中是否存在停止或不健康的容器。"""
    result = item["result"]
    if item.get("tool") != "query_service_status" or result.get("source") != "docker":
        return False
    containers = result.get("containers")
    if not isinstance(containers, list):
        return False
    return any(
        isinstance(container, dict)
        and container.get("service_id") == service_id
        and (
            container.get("running") is False
            or container.get("health_status") == "unhealthy"
        )
        for container in containers
    )


class ProposalRejected(ValueError):
    """操作提案校验失败。

    code 标识失败类别，供调用方判断能否"按证据重建"：
    引用错证据属于机械性错误，可以重建；缺少信息、越权服务等属于实质性
    拒绝，必须保持拒绝。
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def suggestion_from_container_evidence(
        state: dict, managed_services: set[str],
) -> ActionSuggestion | None:
    """模型没有给出可执行建议时，用 Docker 证据兜底构造重启建议。

    模型在证据不全时倾向返回 none，但"容器已退出 / 不健康"本身就是可执行的
    重启依据。这里只把证据翻译成建议，不执行任何操作；提案仍要经过
    build_proposed_action 校验，并由所有者确认后才执行。
    """
    service_id = state.get("service_id")
    if not isinstance(service_id, str) or service_id not in managed_services:
        return None
    if service_id not in SERVICE_DOCKER_TARGETS:
        return None

    for index, item in enumerate(state.get("evidence", [])):
        if item.get("tool") != "query_service_status":
            continue
        if not _is_usable_evidence(item, service_id) or not _has_abnormal_container(item, service_id):
            continue
        container = next(
            (
                entry for entry in item["result"].get("containers", [])
                if isinstance(entry, dict) and entry.get("service_id") == service_id
            ),
            {},
        )
        reason = (
            f"Docker 证据显示 {service_id} 容器未在运行"
            f"（running={container.get('running')}, health={container.get('health_status')}, "
            f"exit_code={container.get('exit_code')}），按运行手册建议重启该容器。"
            "执行前仍需要所有者确认。"
        )[:4000]
        return ActionSuggestion(
            action="restart_service",
            params={"service_id": service_id},
            reason=reason,
            evidence_refs=[index],
            missing_information=[],
        )
    return None


def build_proposed_action(suggestion: ActionSuggestion, state: dict, managed_services: set[str]) -> dict:
    """只接受当前事件中实际返回的引用证据，不接受模型自行提供的证据内容。"""
    if suggestion.action == "none":
        return {"type": "none", "requires_approval": False}
    service_id = state["service_id"]
    if suggestion.params["service_id"] != service_id or service_id not in managed_services:
        raise ProposalRejected("建议目标必须是当前事件中允许管理的服务", "service_scope")
    evidence = state.get("evidence", [])
    # evidence_refs 是证据索引。
    selected = []
    for ref in suggestion.evidence_refs:
        if ref >= len(evidence):
            raise ProposalRejected("建议引用了不存在的证据编号", "evidence_index")
        item = evidence[ref]
        if not _is_usable_evidence(item, service_id):
            raise ProposalRejected("建议引用的证据不可用或不属于当前服务", "evidence_unusable")
        selected.append(item)

    if state.get("agent_error") or state.get("recommendation_error"):
        raise ProposalRejected("自动分析失败，操作建议需要手动检查", "model_failure")
    if suggestion.missing_information:
        missing = "；".join(suggestion.missing_information)
        raise ProposalRejected(f"操作建议仍缺少：{missing}", "missing_information")
    if not selected:
        raise ProposalRejected("操作建议没有引用实际采集证据", "no_evidence")

    if suggestion.action == "restart_service":
        abnormal = any(_has_abnormal_container(item, service_id) for item in selected)
        if service_id not in SERVICE_DOCKER_TARGETS or not abnormal:
            raise ProposalRejected("重启提案缺少受管服务的 Docker 容器异常证据", "docker_evidence")
    elif suggestion.action == "scale_request_concurrency":
        if service_id != "payment-api" or not any(_has_request_waiting(item) for item in selected):
            raise ProposalRejected(
                "请求并发调整仅支持有实际等待指标的 payment-api", "concurrency_evidence",
            )

    return {
        "type": "execute_runbook_step",
        "step": suggestion.action,
        "params": dict(suggestion.params),
        "requires_approval": True,
    }

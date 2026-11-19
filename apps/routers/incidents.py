from fastapi import APIRouter, Depends, HTTPException, Query, Request, Path
from fastapi.responses import StreamingResponse
from apps.crud import incidents
import json
import logging
import uuid

from apps.agent.copilot import (
    answer_incident_question,
    fallback_answer,
    prepare_incident_answer,
    stream_prepared_answer,
)
from apps.common.db import AsyncSession, get_db, active_db, Approval, AuditLog, AgentMessage, Incident
from apps.api.serialize import serialize
from pydantic import BaseModel
from sqlalchemy import select, desc
from apps.worker.tools import (
    execute_tool,
    query_database_health,
    query_logs,
    query_service_metrics,
    query_service_status,
    search_incident_history,
)
from apps.worker.outcomes import status_after_execution, verify_execution
from apps.worker.dead_letters import publish_dead_letter
from apps.common.identity import OWNER_ID


router = APIRouter(prefix="", tags=["incidents"])
logger = logging.getLogger(__name__)
EXECUTABLE_STEPS = {"restart_service", "scale_request_concurrency"}


class ApprovalIn(BaseModel):
    approved: bool
    reason: str | None = None


class IncidentIn(BaseModel):
    title: str
    description: str = ""
    service_id: str
    severity: str = "warning"
    environment: str = "prod"
    tenant_id: str = "demo"


class AgentMessageIn(BaseModel):
    content: str


class ToolInvocationIn(BaseModel):
    """只读工具参数。"""
    query: str = ""


class ConcurrencyLimitIn(BaseModel):
    concurrency_limit: int


def _sse(event: str, data: dict) -> str:
    """将一个事件编码为浏览器可增量解析的 SSE 数据块。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.get("/api/incidents")
async def get_incidents(
        db: AsyncSession = Depends(get_db),
        limit: int = Query(50, ge=1, le=100, description="返回的事件数量"),
):
    result = await incidents.get_incidents_by_count(db, limit)
    return [serialize(row) for row in result]


@router.get("/api/incidents/{incident_id}")
async def search_incident(
        db: AsyncSession = Depends(get_db),
        incident_id: str = Path(..., description="事件ID"),
):
    result = await incidents.get_incident_by_incident_id(db, incident_id)
    if not result:
        raise HTTPException(status_code=404, detail="事件不存在")

    approvals = (await db.scalars(
        select(Approval).where(Approval.incident_id == incident_id)
    )).all()
    audits = (await db.scalars(
        select(AuditLog)
        .where(AuditLog.incident_id == incident_id)
        .order_by(AuditLog.created_at)
    )).all()
    messages = (await db.scalars(
        select(AgentMessage)
        .where(AgentMessage.incident_id == incident_id)
        .order_by(AgentMessage.created_at)
    )).all()
    data = serialize(result)
    data["approvals"] = [
        {"id": item.id, "status": item.status, "action": item.action}
        for item in approvals
    ]
    data["audits"] = [
        {
            "action": item.action,
            "detail": item.detail,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in audits
    ]
    data["messages"] = [
        {
            "id": item.id,
            "role": item.role,
            "content": item.content,
            "tool_calls": item.tool_calls,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in messages
    ]
    return data


@router.post("/api/incidents", status_code=201)
async def create_incident(body: IncidentIn, request: Request):
    """保存事件并提交异步分析。"""
    producer = request.app.state.producer
    if not producer:
        raise HTTPException(status_code=503, detail="分析队列不可用")
    incident_id = f"inc-{uuid.uuid4().hex[:12]}"
    event_id = uuid.uuid4().hex
    trace_id = uuid.uuid4().hex
    alert = {
        "event_id": event_id,
        "incident_id": incident_id,
        "title": body.title,
        "description": body.description,
        "service_id": body.service_id,
        "severity": body.severity,
        "environment": body.environment,
        "tenant_id": body.tenant_id,
        "user_id": OWNER_ID,
        "trace_id": trace_id,
    }
    async with active_db.session() as session:
        incident = Incident(
            id=incident_id, event_id=event_id, status="queued_for_analysis",
            severity=body.severity, service_id=body.service_id,
            environment=body.environment, tenant_id=body.tenant_id,
            title=body.title, alert=alert, trace_id=trace_id,
        )
        session.add(incident)
        session.add(AuditLog(
            incident_id=incident_id, user_id=OWNER_ID, action="incident.queued_for_analysis",
            detail={"description": body.description}, trace_id=trace_id,
        ))
        await session.commit()

    try:
        await producer.send_and_wait(
            "incident.analysis.requested",
            {"incident_id": incident_id, "event_id": event_id, "trace_id": trace_id},
            key=incident_id.encode(),
        )
    except Exception as error:
        async with active_db.session() as session:
            incident = await session.get(Incident, incident_id)
            if incident:
                incident.status = "analysis_queue_failed"
                session.add(AuditLog(
                    incident_id=incident_id, user_id=OWNER_ID, action="incident.analysis_enqueue_failed",
                    detail={"error": str(error)}, trace_id=trace_id,
                ))
                await session.commit()
        raise HTTPException(status_code=503, detail="事件无法加入分析队列") from error
    async with active_db.session() as session:
        incident = await session.get(Incident, incident_id)
        return serialize(incident)


@router.post("/api/incidents/{incident_id}/reanalyze")
async def reanalyze_incident(incident_id: str, request: Request):
    """重新提交事件分析。"""
    producer = request.app.state.producer
    if not producer:
        raise HTTPException(status_code=503, detail="分析队列不可用")
    async with active_db.session() as session:
        incident = await session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在")
        revision_event_id = f"{incident.event_id}:revision:{uuid.uuid4().hex[:8]}"
        session.add(AuditLog(
            incident_id=incident_id, user_id=OWNER_ID, action="agent.reanalysis_requested",
            detail={}, trace_id=incident.trace_id,
        ))
        incident.status = "queued_for_analysis"
        await session.commit()
    await producer.send_and_wait(
        "incident.analysis.requested",
        {"incident_id": incident_id, "event_id": revision_event_id, "trace_id": incident.trace_id},
        key=incident_id.encode(),
    )
    return {"ok": True, "status": "queued_for_analysis"}


@router.post("/api/incidents/{incident_id}/messages")
async def message_agent(incident_id: str, body: AgentMessageIn):
    async with active_db.session() as session:
        incident = await session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在")
        session.add(AgentMessage(
            id=f"msg-{uuid.uuid4().hex}", incident_id=incident_id, role="user", content=body.content,
        ))
        await session.commit()

        answer, evidence = await answer_incident_question(incident, body.content)
        assistant_message = AgentMessage(
            id=f"msg-{uuid.uuid4().hex}", incident_id=incident_id, role="assistant",
            content=answer, tool_calls=evidence,
        )
        session.add(assistant_message)
        session.add(AuditLog(
            incident_id=incident_id, user_id=OWNER_ID, action="agent.question_answered",
            detail={"tool_calls": evidence}, trace_id=incident.trace_id,
        ))
        await session.commit()
        return {
            "id": assistant_message.id, "role": "assistant", "content": answer,
            "tool_calls": evidence,
            "created_at": assistant_message.created_at.isoformat() if assistant_message.created_at else None,
        }


@router.post("/api/incidents/{incident_id}/messages/stream")
async def stream_agent_message(incident_id: str, body: AgentMessageIn):
    """流式返回回答，完成后保存消息。"""
    async with active_db.session() as session:
        incident = await session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在")
        user_message = AgentMessage(
            id=f"msg-{uuid.uuid4().hex}",
            incident_id=incident_id,
            role="user",
            content=body.content,
        )
        session.add(user_message)
        await session.commit()

    assistant_id = f"msg-{uuid.uuid4().hex}"

    async def event_stream():
        evidence: list[dict] = []
        parts: list[str] = []
        try:
            yield _sse("status", {"message": "正在查询事件证据…"})
            prompt, evidence, fallback = await prepare_incident_answer(incident, body.content)
            yield _sse("tools", {"tool_calls": evidence})
            yield _sse("status", {"message": "系统正在生成回答…"})

            async for delta in stream_prepared_answer(prompt, fallback):
                parts.append(delta)
                yield _sse("delta", {"content": delta})

            answer = "".join(parts).strip() or fallback_answer(incident)
            async with active_db.session() as session:
                assistant_message = AgentMessage(
                    id=assistant_id,
                    incident_id=incident_id,
                    role="assistant",
                    content=answer,
                    tool_calls=evidence,
                )
                session.add(assistant_message)
                session.add(AuditLog(
                    incident_id=incident_id,
                    user_id=OWNER_ID,
                    action="agent.question_answered",
                    detail={"tool_calls": evidence, "streamed": True},
                    trace_id=incident.trace_id,
                ))
                await session.commit()
            yield _sse("done", {
                "message": {
                    "id": assistant_id,
                    "role": "assistant",
                    "content": answer,
                    "tool_calls": evidence,
                }
            })
        except Exception as error:
            logger.exception("事件 %s 的流式回答失败", incident_id)
            yield _sse("error", {"detail": f"流式回答失败：{type(error).__name__}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/api/incidents/{incident_id}/tools/{tool_name}")
async def invoke_read_tool(
    incident_id: str,
    tool_name: str,
    body: ToolInvocationIn,
):
    """调用事件页面允许使用的只读工具。"""
    async with active_db.session() as session:
        incident = await session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在")

        try:
            if tool_name == "query_service_status":
                result = await query_service_status(incident.service_id)
            elif tool_name == "query_service_metrics":
                result = await query_service_metrics(incident.service_id)
            elif tool_name == "query_database_health":
                result = await query_database_health(incident.service_id)
            elif tool_name == "query_logs":
                result = await query_logs(incident.service_id, body.query)
            elif tool_name == "search_incident_history":
                result = await search_incident_history(
                    incident.service_id, body.query,
                    environment=incident.environment,
                    classification=incident.classification or "unknown",
                    exclude_incident_id=incident.id,
                    tenant_id=incident.tenant_id,
                )
            else:
                raise HTTPException(status_code=404, detail="该工具不可用")
        except HTTPException:
            raise
        except Exception as error:
            logger.exception("事件 %s 调用工具 %s 失败", incident_id, tool_name)
            raise HTTPException(status_code=502, detail=str(error)) from error

        session.add(AuditLog(
            incident_id=incident_id,
            user_id=OWNER_ID,
            action="mcp.read_tool_invoked",
            detail={"tool": tool_name, "result": result},
            trace_id=incident.trace_id,
        ))
        await session.commit()

    return {"tool": tool_name, "result": result}


@router.post("/api/incidents/{incident_id}/concurrency-limit")
async def request_concurrency_limit(incident_id: str, body: ConcurrencyLimitIn):
    """创建 payment-api 请求并发调整确认。"""
    if not 1 <= body.concurrency_limit <= 200:
        raise HTTPException(status_code=422, detail="concurrency_limit 必须在 1 到 200 之间")

    async with active_db.session() as session:
        incident = await session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在，无法调整并发上限")
        if incident.service_id not in {"payment-api", "payment_api"}:
            raise HTTPException(
                status_code=409,
                detail="仅支持 payment-api 事件并发上限调整",
            )
        pending = await session.scalar(
            select(Approval.id).where(Approval.incident_id == incident_id, Approval.status == "pending")
        )
        if pending:
            raise HTTPException(status_code=409, detail="请先处理当前待确认操作，再调整并发上限")

        action = {
            "type": "execute_runbook_step",
            "step": "scale_request_concurrency",
            "params": {"service_id": "payment-api", "concurrency_limit": body.concurrency_limit},
            "requires_approval": True,
        }
        approval_id = f"appr-{uuid.uuid4().hex[:12]}"
        incident.proposed_action = action
        incident.action_risk = {
            "level": "high", "action": "scale_request_concurrency",
            "reason": "调整请求并发会改变应用负载，需要你确认。",
        }
        incident.risk_level = "high"
        if incident.evidence_assessment:
            previous = incident.evidence_assessment
            incident.evidence_assessment = {
                **previous,
                "level": "partial" if previous.get("usable_count", 0) else "insufficient",
                "gaps": [*previous.get("gaps", []), "这是新的并发上限提案，现有分析没有验证本次目标值。"],
            }
        incident.approval_status = "pending"
        incident.status = "awaiting_approval"
        session.add(Approval(id=approval_id, incident_id=incident_id, action=action, status="pending"))
        session.add(AuditLog(
            incident_id=incident_id,
            user_id=OWNER_ID,
            action="payment.request_concurrency_change_requested",
            detail={"approval_id": approval_id, "concurrency_limit": body.concurrency_limit},
            trace_id=incident.trace_id,
        ))
        await session.commit()

    return {"approval_id": approval_id, "approval_status": "pending", "proposed_action": action}



@router.post("/api/incidents/{incident_id}/approval")
async def approval(incident_id: str, body: ApprovalIn, request: Request):
    producer = request.app.state.producer
    async with active_db.session() as session:
        incident  = await session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在，无法确认操作")

        # 检查是否已审批
        approval = await session.scalar(
            select(Approval).where(Approval.incident_id == incident_id, Approval.status == "pending")
            .order_by(desc(Approval.created_at))
        )

        if not approval:
            raise HTTPException(status_code=404, detail="当前事件没有待确认操作")
        if body.approved and (
                not isinstance(approval.action, dict)
                or approval.action.get("type") != "execute_runbook_step"
                or approval.action.get("step") not in EXECUTABLE_STEPS
        ):
            raise HTTPException(status_code=409, detail="该操作不受支持，可以取消确认记录")

        approval.status = "approved" if body.approved else "rejected"
        approval.reason = body.reason
        incident.approval_status = approval.status

        session.add(
            AuditLog(
                incident_id=incident_id,
                user_id=OWNER_ID,
                action="approval_decided",
                detail={
                    "approved": body.approved,
                    "approval_id": approval.id,
                    "reason": body.reason,
                }
            )
        )

        await session.commit()

        if body.approved:
            idempotency_key = f"{incident_id}:{approval.id}"
            action_message = {
                "incident_id": incident_id,
                "approval_id": approval.id,
                "action": approval.action,
                "idempotency_key": idempotency_key,
                "trace_id": incident.trace_id,
            }

            if producer:
                await producer.send_and_wait("incident.action.requested", action_message)
                return {"ok": True, "approval_status": approval.status, "execution": "queued"}

            # 没有 Kafka Worker 时直接执行。
            try:
                execution_result = await execute_tool(
                    action=approval.action,
                    approval_id=approval.id,
                    idempotency_key=idempotency_key,
                )
            except Exception as error:
                execution_result = {"status": "failed", "reason": str(error)}

            execution_result["verification"] = await verify_execution(
                approval.action, execution_result,
            )
            if execution_result.get("status") != "success":
                await publish_dead_letter(
                    producer,
                    "incident.action.dlq",
                    "action",
                    action_message,
                    RuntimeError(execution_result.get("reason") or "操作执行失败"),
                )

            incident.execution_result = execution_result
            incident.status = status_after_execution(execution_result)
            session.add(AuditLog(
                incident_id=incident_id,
                user_id=OWNER_ID,
                action="action.executed_inline",
                detail={"action": approval.action, "result": execution_result},
                trace_id=incident.trace_id,
            ))
            await session.commit()
            return {
                "ok": True,
                "approval_status": approval.status,
                "execution": "completed",
                "execution_result": execution_result,
            }

        return {"ok": True, "approval_status": approval.status}


@router.post("/api/incidents/{incident_id}/retry-action")
async def retry_failed_action(incident_id: str):
    """为失败动作创建一次新的待审批请求，不复用旧幂等键。"""
    async with active_db.session() as session:
        incident = await session.get(Incident, incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="事件不存在")
        if incident.status != "execution_failed" or not incident.proposed_action:
            raise HTTPException(status_code=409, detail="当前事件没有可重试的失败动作")
        if (
                not isinstance(incident.proposed_action, dict)
                or incident.proposed_action.get("type") != "execute_runbook_step"
                or incident.proposed_action.get("step") not in EXECUTABLE_STEPS
        ):
            raise HTTPException(status_code=409, detail="该操作不支持重试")
        pending = await session.scalar(
            select(Approval.id).where(
                Approval.incident_id == incident_id,
                Approval.status == "pending",
            )
        )
        if pending:
            raise HTTPException(status_code=409, detail="当前已有待确认操作")

        approval_id = f"appr-{uuid.uuid4().hex[:12]}"
        session.add(Approval(
            id=approval_id,
            incident_id=incident_id,
            action=incident.proposed_action,
            status="pending",
        ))
        session.add(AuditLog(
            incident_id=incident_id,
            user_id=OWNER_ID,
            action="action.retry_requested",
            detail={"approval_id": approval_id},
            trace_id=incident.trace_id,
        ))
        incident.approval_status = "pending"
        incident.status = "awaiting_approval"
        await session.commit()
        return {"approval_id": approval_id, "approval_status": "pending"}

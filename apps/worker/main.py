"""处理告警接收、Agent 分析和审批后操作的 Kafka Worker。"""

import asyncio
import logging
import uuid

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from prometheus_client import start_http_server
from sqlalchemy import select

from apps.common.config import settings
from apps.common.db import AuditLog, Incident, active_db
from apps.common.kafka_client import create_consumer, create_producer
from apps.common.identity import OWNER_ID
from apps.worker.dead_letters import publish_dead_letter
from apps.worker.graph import build_graph, graph
from apps.worker.metrics import ACTION_EXECUTIONS
from apps.worker.outcomes import status_after_execution, verify_execution
from apps.worker.tools import execute_tool
from apps.seed.seed_docs import main as seed_knowledge_base


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ALERT_TOPIC = "alert.triggered"
ANALYSIS_TOPIC = "incident.analysis.requested"
ANALYZED_TOPIC = "incident.analyzed"
ACTION_TOPIC = "incident.action.requested"
ACTION_COMPLETED_TOPIC = "incident.action.completed"
ANALYSIS_DLQ_TOPIC = "incident.analysis.dlq"
ACTION_DLQ_TOPIC = "incident.action.dlq"


async def create_incident_from_alert(data: dict) -> dict | None:
    """快速保存告警，并返回供异步分析使用的精简消息。"""
    event_id = data.get("event_id") or uuid.uuid4().hex
    incident_id = data.get("incident_id") or f"inc-{event_id[:8]}"

    async with active_db.session() as session:
        fingerprint = data.get("fingerprint")
        recent = (await session.scalars(
            select(Incident).where(
                Incident.service_id == data.get("service_id", "unknown"),
                Incident.environment == data.get("environment", "prod"),
            ).order_by(Incident.created_at.desc()).limit(20)
        )).all()
        related = next(
            (item for item in recent if item.alert.get("fingerprint") == fingerprint),
            None,
        )
        if data.get("status") == "resolved":
            if related:
                related.status = "resolved"
                session.add(AuditLog(
                    incident_id=related.id,
                    user_id="system",
                    action="incident.alert_resolved",
                    detail={"event_id": event_id, "fingerprint": fingerprint},
                    trace_id=related.trace_id,
                ))
                await session.commit()
            return None

        lifecycle = data.get("annotations", {}).get("lifecycle", {})
        if lifecycle.get("recurrence_after_recovery") and related:
            data = {
                **data,
                "annotations": {
                    **data.get("annotations", {}),
                    "related_incident_id": related.id,
                },
            }

        existing = await session.scalar(select(Incident).where(Incident.event_id == event_id))
        if existing:
            logging.info("duplicate alert skipped: %s", event_id)
            return None

        trace_id = data.get("trace_id") or uuid.uuid4().hex
        incident = Incident(
            id=incident_id,
            event_id=event_id,
            status="queued_for_analysis",
            severity=data.get("severity", "warning"),
            service_id=data.get("service_id", "unknown"),
            environment=data.get("environment", "prod"),
            tenant_id=data.get("tenant_id", "demo"),
            title=data.get("title", "告警事件"),
            alert=data,
            trace_id=trace_id,
        )
        session.add(incident)
        session.add(AuditLog(
            incident_id=incident_id,
            user_id=data.get("user_id", "system"),
            action="incident.queued_for_analysis",
            detail={"event_id": event_id},
            trace_id=trace_id,
        ))
        await session.commit()

    return {"incident_id": incident_id, "event_id": event_id, "trace_id": trace_id}


async def analyse_incident(message: dict, workflow=graph) -> dict:
    """在告警接收流程之外运行耗时的 Agent 工作流。"""
    incident_id = message["incident_id"]
    async with active_db.session() as session:
        incident = await session.get(Incident, incident_id)
        if not incident:
            raise ValueError(f"incident {incident_id} not found")
        incident.status = "analysing"
        session.add(AuditLog(
            incident_id=incident_id,
            user_id="system",
            action="incident.analysis_started",
            detail={},
            trace_id=incident.trace_id,
        ))
        await session.commit()
        state = {
            "incident_id": incident.id,
            "event_id": message.get("event_id", incident.event_id),
            "user_id": OWNER_ID,
            "tenant_id": incident.tenant_id,
            "alert": incident.alert,
            "service_id": incident.service_id,
            "environment": incident.environment,
            "severity": incident.severity,
            "trace_id": incident.trace_id,
        }

    result = await workflow.ainvoke(
        state,
        config={"configurable": {"thread_id": message.get("event_id") or incident_id}},
    )
    logging.info("analysis completed: %s risk=%s", incident_id, result.get("risk_level"))
    return result


async def mark_analysis_failed(message: dict, error: Exception) -> None:
    async with active_db.session() as session:
        incident = await session.get(Incident, message.get("incident_id"))
        if incident:
            incident.status = "analysis_failed"
            session.add(AuditLog(
                incident_id=incident.id,
                user_id="system",
                action="incident.analysis_failed",
                detail={"error": str(error)},
                trace_id=incident.trace_id,
            ))
            await session.commit()


async def consume_alert_intake(producer):
    """快速流程：保存、去重告警，并提交异步分析任务。"""
    consumer = create_consumer([ALERT_TOPIC], "incident-intake-worker")
    await consumer.start()
    logging.info("incident intake consumer started")
    try:
        async for msg in consumer:
            try:
                analysis_message = await create_incident_from_alert(msg.value)
                if analysis_message:
                    await producer.send_and_wait(ANALYSIS_TOPIC, analysis_message, key=analysis_message["incident_id"].encode())
                await consumer.commit()
            except Exception as error:
                logging.exception("alert intake failed")
                await publish_dead_letter(
                    producer, ANALYSIS_DLQ_TOPIC, "intake", msg.value, error,
                )
                await consumer.commit()
    finally:
        await consumer.stop()


async def consume_analysis(producer, workflow=graph):
    """分析流程：在独立消费组中运行 RAG、LLM 和 MCP。"""
    consumer = create_consumer([ANALYSIS_TOPIC], "incident-analysis-worker")
    await consumer.start()
    logging.info("incident analysis consumer started")
    try:
        async for msg in consumer:
            try:
                result = await analyse_incident(msg.value, workflow)
                await producer.send_and_wait(
                    ANALYZED_TOPIC,
                    {"incident_id": msg.value["incident_id"], "status": "analyzed",
                     "risk_level": result.get("risk_level"), "action_risk": result.get("action_risk"),
                     "evidence_assessment": result.get("evidence_assessment")},
                )
                await consumer.commit()
            except Exception as error:
                logging.exception("incident analysis failed")
                await mark_analysis_failed(msg.value, error)
                await publish_dead_letter(
                    producer, ANALYSIS_DLQ_TOPIC, "analysis", msg.value, error,
                )
                await consumer.commit()
    finally:
        await consumer.stop()


async def consume_actions(producer):
    consumer = create_consumer([ACTION_TOPIC], "incident-action-worker")
    await consumer.start()
    logging.info("incident action consumer started")
    try:
        async for msg in consumer:
            data = msg.value
            try:
                result = await execute_tool(
                    action=data["action"],
                    approval_id=data["approval_id"],
                    idempotency_key=data["idempotency_key"],
                )
                result["verification"] = await verify_execution(data["action"], result)
                ACTION_EXECUTIONS.labels(status=result.get("status", "unknown")).inc()
                async with active_db.session() as session:
                    incident = await session.get(Incident, data["incident_id"])
                    if incident:
                        incident.execution_result = result
                        incident.status = status_after_execution(result)
                    session.add(AuditLog(
                        incident_id=data["incident_id"], user_id=OWNER_ID, action="action.executed",
                        detail={"action": data["action"], "result": result}, trace_id=data.get("trace_id"),
                    ))
                    await session.commit()
                if result.get("status") != "success":
                    await publish_dead_letter(
                        producer,
                        ACTION_DLQ_TOPIC,
                        "action",
                        data,
                        RuntimeError(result.get("reason") or "操作执行失败"),
                    )
                await producer.send_and_wait(ACTION_COMPLETED_TOPIC, {"incident_id": data["incident_id"], "result": result})
                await consumer.commit()
            except Exception as error:
                logging.exception("incident action failed")
                ACTION_EXECUTIONS.labels(status="failed").inc()
                failure = {
                    "status": "failed",
                    "reason": str(error),
                    "verification": {
                        "status": "execution_failed",
                        "recovered": False,
                        "next_step": "确认后重新创建操作记录",
                    },
                }
                async with active_db.session() as session:
                    incident = await session.get(Incident, data["incident_id"])
                    if incident:
                        incident.execution_result = failure
                        incident.status = "execution_failed"
                    session.add(AuditLog(
                        incident_id=data["incident_id"],
                        user_id=OWNER_ID,
                        action="action.execution_failed",
                        detail={"action": data.get("action"), "result": failure},
                        trace_id=data.get("trace_id"),
                    ))
                    await session.commit()
                await publish_dead_letter(
                    producer, ACTION_DLQ_TOPIC, "action", data, error,
                )
                await consumer.commit()
    finally:
        await consumer.stop()


async def main():
    await active_db.init_db()
    start_http_server(settings.worker_metrics_port)
    # 启动时同步内置 Runbook；固定文档 ID 可避免产生重复记录。
    await asyncio.to_thread(seed_knowledge_base)
    producer = await create_producer()
    try:
        async with AsyncPostgresSaver.from_conn_string(settings.graph_url) as checkpointer:
            await checkpointer.setup()
            workflow = build_graph(checkpointer=checkpointer)
            await asyncio.gather(
                consume_alert_intake(producer),
                consume_analysis(producer, workflow),
                consume_actions(producer),
            )
    finally:
        await producer.stop()


if __name__ == "__main__":
    asyncio.run(main())

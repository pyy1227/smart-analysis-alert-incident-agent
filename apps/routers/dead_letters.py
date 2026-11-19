"""死信消息查看和处理接口。"""

from datetime import datetime
import uuid
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from apps.common.db import Approval, AuditLog, DeadLetter, Incident, active_db
from apps.common.identity import OWNER_ID


router = APIRouter(prefix="/api/dead-letters", tags=["dead-letters"])

CHINA_TIME = ZoneInfo("Asia/Shanghai")

ANALYSIS_TOPIC = "incident.analysis.requested"
ACTION_TOPIC = "incident.action.requested"
ALERT_TOPIC = "alert.triggered"
ACTIVE_STATUSES = {"pending", "publish_failed", "replaying"}
EXECUTABLE_STEPS = {"restart_service", "scale_request_concurrency"}


class ReplayIn(BaseModel):
    confirmed: bool


def _serialize(item: DeadLetter) -> dict:
    return {
        "id": item.id,
        "incident_id": item.incident_id,
        "stage": item.stage,
        "error": item.error,
        "payload": item.payload,
        "status": item.status,
        "kafka_topic": item.kafka_topic,
        "kafka_partition": item.kafka_partition,
        "kafka_offset": item.kafka_offset,
        "publish_error": item.publish_error,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "handled_at": item.handled_at.isoformat() if item.handled_at else None,
    }


@router.get("")
async def list_dead_letters(
        limit: int = Query(100, ge=1, le=200),
        active_only: bool = False,
):
    async with active_db.session() as session:
        statement = select(DeadLetter).order_by(DeadLetter.created_at.desc()).limit(limit)
        if active_only:
            statement = statement.where(DeadLetter.status.in_(ACTIVE_STATUSES))
        rows = (await session.scalars(statement)).all()
        return [_serialize(item) for item in rows]


@router.post("/{dead_letter_id}/ignore")
async def ignore_dead_letter(dead_letter_id: str):
    async with active_db.session() as session:
        item = await session.get(DeadLetter, dead_letter_id)
        if not item:
            raise HTTPException(status_code=404, detail="死信消息不存在")
        if item.status not in {"pending", "publish_failed"}:
            raise HTTPException(status_code=409, detail="这条死信消息已经处理")
        item.status = "ignored"
        item.handled_at = datetime.now(CHINA_TIME)
        session.add(AuditLog(
            incident_id=item.incident_id,
            user_id=OWNER_ID,
            action="dead_letter.ignored",
            detail={"dead_letter_id": item.id, "stage": item.stage},
        ))
        await session.commit()
        return _serialize(item)


async def _prepare_replay(session, item: DeadLetter) -> tuple[str, dict, str | None]:
    payload = dict(item.payload or {})
    incident = await session.get(Incident, item.incident_id) if item.incident_id else None
    if not incident and payload.get("event_id"):
        incident = await session.scalar(
            select(Incident).where(Incident.event_id == payload["event_id"])
        )

    if item.stage == "action":
        if not incident:
            raise HTTPException(status_code=409, detail="找不到死信关联的事件")
        action = payload.get("action")
        if not isinstance(action, dict):
            raise HTTPException(status_code=409, detail="死信消息缺少可重试的操作")
        if (
                action.get("type") != "execute_runbook_step"
                or action.get("step") not in EXECUTABLE_STEPS
        ):
            raise HTTPException(status_code=409, detail="该操作不支持重新投递")
        approval_id = f"appr-{uuid.uuid4().hex[:12]}"
        idempotency_key = f"{incident.id}:{approval_id}"
        session.add(Approval(
            id=approval_id,
            incident_id=incident.id,
            action=action,
            status="approved",
            reason="本人确认重新投递死信消息",
        ))
        incident.approval_status = "approved"
        incident.status = "action_queued"
        return ACTION_TOPIC, {
            **payload,
            "incident_id": incident.id,
            "approval_id": approval_id,
            "idempotency_key": idempotency_key,
            "trace_id": incident.trace_id,
        }, approval_id

    if item.stage == "analysis":
        if not incident:
            raise HTTPException(status_code=409, detail="找不到死信关联的事件")
        incident.status = "queued_for_analysis"
        return ANALYSIS_TOPIC, {
            "incident_id": incident.id,
            "event_id": incident.event_id,
            "trace_id": incident.trace_id,
        }, None

    if item.stage == "intake":
        if incident:
            incident.status = "queued_for_analysis"
            return ANALYSIS_TOPIC, {
                "incident_id": incident.id,
                "event_id": incident.event_id,
                "trace_id": incident.trace_id,
            }, None
        return ALERT_TOPIC, payload, None

    raise HTTPException(status_code=409, detail=f"不支持重新投递阶段：{item.stage}")


@router.post("/{dead_letter_id}/replay")
async def replay_dead_letter(dead_letter_id: str, body: ReplayIn, request: Request):
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="重新投递前需要本人确认")
    producer = request.app.state.producer
    if not producer:
        raise HTTPException(status_code=503, detail="Kafka 暂时不可用")

    approval_id = None
    replay_stage = None
    async with active_db.session() as session:
        item = await session.get(DeadLetter, dead_letter_id)
        if not item:
            raise HTTPException(status_code=404, detail="死信消息不存在")
        if item.status not in {"pending", "publish_failed"}:
            raise HTTPException(status_code=409, detail="这条死信消息不能重新投递")
        topic, payload, approval_id = await _prepare_replay(session, item)
        replay_stage = item.stage
        item.status = "replaying"
        session.add(AuditLog(
            incident_id=item.incident_id,
            user_id=OWNER_ID,
            action="dead_letter.replay_requested",
            detail={"dead_letter_id": item.id, "target_topic": topic},
        ))
        await session.commit()
        incident_id = item.incident_id

    try:
        key_value = incident_id or dead_letter_id
        await producer.send_and_wait(topic, payload, key=key_value.encode())
    except Exception as error:
        async with active_db.session() as session:
            item = await session.get(DeadLetter, dead_letter_id)
            item.status = "pending"
            item.publish_error = f"重新投递失败：{error}"
            if approval_id:
                approval = await session.get(Approval, approval_id)
                if approval:
                    approval.status = "failed"
            incident = await session.get(Incident, incident_id) if incident_id else None
            if incident:
                if replay_stage == "action":
                    incident.status = "execution_failed"
                    incident.approval_status = "failed"
                elif replay_stage == "analysis":
                    incident.status = "analysis_failed"
                else:
                    incident.status = "analysis_queue_failed"
            session.add(AuditLog(
                incident_id=incident_id,
                user_id=OWNER_ID,
                action="dead_letter.replay_failed",
                detail={"dead_letter_id": dead_letter_id, "error": str(error)},
            ))
            await session.commit()
        raise HTTPException(status_code=503, detail="死信消息重新投递失败") from error

    async with active_db.session() as session:
        item = await session.get(DeadLetter, dead_letter_id)
        item.status = "replayed"
        item.publish_error = None
        item.handled_at = datetime.now(CHINA_TIME)
        session.add(AuditLog(
            incident_id=incident_id,
            user_id=OWNER_ID,
            action="dead_letter.replayed",
            detail={"dead_letter_id": dead_letter_id, "target_topic": topic},
        ))
        await session.commit()
        return _serialize(item)

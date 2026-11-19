"""记录并投递死信消息。"""

from datetime import datetime
import uuid
from zoneinfo import ZoneInfo

from apps.common.db import AuditLog, DeadLetter, active_db


CHINA_TIME = ZoneInfo("Asia/Shanghai")


def _incident_id(payload: dict) -> str | None:
    value = payload.get("incident_id") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


async def publish_dead_letter(
        producer,
        topic: str,
        stage: str,
        payload: dict,
        error: Exception,
) -> dict:
    """先保存失败消息，再尝试写入 Kafka DLQ。"""
    dead_letter_id = f"dlq-{uuid.uuid4().hex[:16]}"
    incident_id = _incident_id(payload)
    error_text = str(error) or error.__class__.__name__
    created_at = datetime.now(CHINA_TIME)
    envelope = {
        "dead_letter_id": dead_letter_id,
        "incident_id": incident_id,
        "error": error_text,
        "stage": stage,
        "payload": payload,
        "created_at": created_at.isoformat(),
    }

    async with active_db.session() as session:
        session.add(DeadLetter(
            id=dead_letter_id,
            incident_id=incident_id,
            stage=stage,
            error=error_text,
            payload=payload,
            status="publishing",
            kafka_topic=topic,
            created_at=created_at,
        ))
        session.add(AuditLog(
            incident_id=incident_id,
            user_id="system",
            action="dead_letter.recorded",
            detail={"dead_letter_id": dead_letter_id, "stage": stage, "topic": topic},
        ))
        await session.commit()

    try:
        metadata = await producer.send_and_wait(topic, envelope)
    except Exception as publish_error:
        async with active_db.session() as session:
            item = await session.get(DeadLetter, dead_letter_id)
            item.status = "publish_failed"
            item.publish_error = str(publish_error)
            session.add(AuditLog(
                incident_id=incident_id,
                user_id="system",
                action="dead_letter.publish_failed",
                detail={"dead_letter_id": dead_letter_id, "error": str(publish_error)},
            ))
            await session.commit()
        return {"id": dead_letter_id, "status": "publish_failed"}

    async with active_db.session() as session:
        item = await session.get(DeadLetter, dead_letter_id)
        item.status = "pending"
        item.kafka_partition = getattr(metadata, "partition", None)
        item.kafka_offset = getattr(metadata, "offset", None)
        session.add(AuditLog(
            incident_id=incident_id,
            user_id="system",
            action="dead_letter.published",
            detail={
                "dead_letter_id": dead_letter_id,
                "topic": topic,
                "partition": item.kafka_partition,
                "offset": item.kafka_offset,
            },
        ))
        await session.commit()
    return {"id": dead_letter_id, "status": "pending"}

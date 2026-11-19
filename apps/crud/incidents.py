"""事件查询的只读 CRUD：只做单表读取，审计与关联数据的组装放在路由层。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.db import Incident


async def get_incident_by_incident_id(
        db: AsyncSession,
        incident_id: str,
):
    return await db.scalar(select(Incident).where(Incident.id == incident_id))


async def get_incidents_by_count(db: AsyncSession, limit: int = 50):
    return (await db.scalars(select(Incident).limit(limit))).all()

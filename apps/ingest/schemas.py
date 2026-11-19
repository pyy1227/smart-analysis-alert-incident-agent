#统一告警事件模型
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class AlertStatus(str, Enum):
    FIRING = "firing"
    RESOLVED = "resolved"
    ACKNOWLEDGED = "acknowledged"
    SILENCED = "silenced"


class UnifiedAlert(BaseModel):
    event_id: str
    tenant_id: str = "demo"
    service_id: str
    environment: str = "prod"
    severity: str = "warning"
    status: AlertStatus = AlertStatus.FIRING
    title: str
    description: str = ""
    trace_id: str
    fingerprint: str
    labels: dict = Field(default_factory=dict)
    annotations: dict = Field(default_factory=dict)
    started_at: datetime
    raw: dict = Field(default_factory=dict)
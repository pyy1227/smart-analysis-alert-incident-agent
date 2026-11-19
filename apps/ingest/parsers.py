"""将本地 Alertmanager 告警转换为统一事件格式。"""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from apps.ingest.schemas import AlertStatus, UnifiedAlert

CHINA_TIME = ZoneInfo("Asia/Shanghai")

def parse_prometheus(payload: dict) -> list[UnifiedAlert]:
    alerts = []
    for item in payload.get("alerts", []):
        labels = item.get("labels", {})
        annotations = item.get("annotations", {})
        service_id = labels.get("service", "unknown")
        status = (
            AlertStatus.FIRING
            if item.get("status") == "firing"
            else AlertStatus.RESOLVED
        )
        alerts.append(UnifiedAlert(
            event_id=uuid.uuid4().hex,
            tenant_id="demo",
            service_id=service_id,
            environment=labels.get("environment", "prod"),
            severity=labels.get("severity", "warning"),
            status=status,
            title=labels.get("alertname", "Prometheus Alert"),
            description=annotations.get("description", ""),
            trace_id=uuid.uuid4().hex,
            fingerprint=f"prom:{service_id}:{labels.get('alertname')}",
            labels=labels,
            annotations=annotations,
            started_at=item.get("startsAt") or datetime.now(CHINA_TIME).isoformat(),
            raw=item,
        ))
    return alerts


PARSERS = {
    "prometheus": parse_prometheus,
}

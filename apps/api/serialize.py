from apps.common.db import Incident


"""
数据库查出来的是 Incident ORM 对象，FastAPI 不能直接把它序列化成 JSON（尤其 created_at 是 datetime，alert 可能是 JSON 字段）。

这个函数手动挑选要返回的字段，组成一个 dict，FastAPI 就能自动转成 JSON。

created_at.isoformat() 把 datetime 转成字符串（如 2026-10-03T11:50:21），空值则返回 None
"""
def serialize(incident: Incident):
    return {
        "id": incident.id,
        "event_id": incident.event_id,
        "status": incident.status,
        "severity": incident.severity,
        "service_id": incident.service_id,
        "environment": incident.environment,
        "classification": incident.classification,
        "tenant_id": incident.tenant_id,
        "title": incident.title,
        "alert": incident.alert,
        "report": incident.report,
        "citations": incident.citations,
        "risk_level": incident.risk_level,
        "evidence_assessment": incident.evidence_assessment,
        "action_risk": incident.action_risk,
        "proposed_action": incident.proposed_action,
        "approval_status": incident.approval_status,
        "execution_result": incident.execution_result,
        "trace_id": incident.trace_id,
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
        "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
    }

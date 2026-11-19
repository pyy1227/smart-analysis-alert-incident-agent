"""确保事件表包含分析字段。"""

from sqlalchemy import inspect


def ensure_assessment_columns(connection):
    columns = {column["name"] for column in inspect(connection).get_columns("incidents")}
    definitions = {
        "evidence_assessment": "JSON",
        "action_risk": "JSON",
        "classification": "VARCHAR(32)",
    }
    for name, sql_type in definitions.items():
        if name not in columns:
            # API 和 Worker 可能同时启动，PostgreSQL 会处理并发加列。
            guard = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
            connection.exec_driver_sql(f"ALTER TABLE incidents ADD COLUMN {guard}{name} {sql_type}")

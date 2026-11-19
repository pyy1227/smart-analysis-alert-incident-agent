"""Worker 自监控指标。"""

from prometheus_client import Counter
ACTION_EXECUTIONS = Counter(
    "incident_action_execution_total",
    "Approved action execution results",
    ["status"],
)
ACTION_VERIFICATIONS = Counter(
    "incident_action_verification_total",
    "Post-action verification results",
    ["status"],
)

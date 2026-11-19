from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import yaml

from apps.ingest import lifecycle
from apps.worker import outcomes


ROOT = Path(__file__).resolve().parents[1]


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value
        return True


def alert(status="firing"):
    return SimpleNamespace(
        environment="staging",
        service_id="payment-api",
        fingerprint="prom:payment-api:HighErrorRate",
        status=SimpleNamespace(value=status),
        title="PaymentApiHighErrorRate",
        annotations={},
    )


class AlertLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_recurrence_is_recorded(self):
        fake = FakeRedis()
        with patch.object(lifecycle, "redis_client", fake):
            first = alert("firing")
            await lifecycle.enrich_alert_lifecycle(first)
            recovered = alert("resolved")
            await lifecycle.enrich_alert_lifecycle(recovered)
            recurring = alert("firing")
            recurrence = await lifecycle.enrich_alert_lifecycle(recurring)

        self.assertTrue(recurrence["recurrence_after_recovery"])


class ActionOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_requires_new_approved_retry(self):
        result = await outcomes.verify_execution(
            {"step": "restart_service", "params": {"service_id": "payment-api"}},
            {"status": "failed"},
        )
        self.assertEqual(result["status"], "execution_failed")
        self.assertIn("重新创建", result["next_step"])

    async def test_zero_waiting_marks_incident_recovered(self):
        metrics = {
            "metrics": {"request_concurrency_waiting": {
                "data": {"result": [{"value": [0, "0"]}]},
            }},
        }
        action = {
            "step": "scale_request_concurrency",
            "params": {"service_id": "payment-api", "concurrency_limit": 10},
        }
        with patch.object(outcomes, "query_service_metrics", AsyncMock(return_value=metrics)):
            verification = await outcomes.verify_execution(action, {"status": "success"})
        execution = {"status": "success", "verification": verification}
        self.assertTrue(verification["recovered"])
        self.assertEqual(outcomes.status_after_execution(execution), "resolved")


class AlertRuleTests(unittest.TestCase):
    def test_requested_alert_rules_exist(self):
        rules = yaml.safe_load(
            (ROOT / "infra/prometheus/alert-rules.yml").read_text(encoding="utf-8")
        )
        names = {
            rule["alert"]
            for group in rules["groups"]
            for rule in group["rules"]
        }
        self.assertTrue({
            "PaymentRequestConcurrencySaturated",
            "PaymentDbPoolSaturated",
            "PaymentApiHighErrorRate",
            "IncidentActionExecutionFailed",
        } <= names)
        self.assertNotIn("AlertmanagerDeliveryFailed", names)
        self.assertNotIn("KafkaConsumerLagHigh", names)


if __name__ == "__main__":
    unittest.main()

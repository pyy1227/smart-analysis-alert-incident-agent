import unittest
from unittest.mock import patch

from apps.common import prometheus_adapter


class PaymentRequestConcurrencyMetricTests(unittest.TestCase):
    @patch.object(prometheus_adapter, "_prometheus_query", return_value={"result": []})
    @patch.object(prometheus_adapter, "assert_managed_service")
    def test_service_metrics_query_the_root_payment_api_wait_gauge(self, check_service, query):
        with patch.object(prometheus_adapter.settings, "prometheus_service_label", "service"):
            result = prometheus_adapter.query_service_metrics("payment-api")

        expression = result["metrics"]["request_concurrency_waiting"]["expression"]
        self.assertEqual(expression, 'max(payment_request_concurrency_waiting{service="payment-api"})')
        self.assertIn("or vector(0)", result["metrics"]["error_rate"]["expression"])
        query.assert_any_call(expression)

    @patch.object(prometheus_adapter, "_prometheus_query", return_value={"result": []})
    @patch.object(prometheus_adapter, "assert_managed_service")
    def test_database_health_uses_the_deployed_payment_api_pool_metrics(self, check_service, query):
        with patch.object(prometheus_adapter.settings, "prometheus_service_label", "service"):
            result = prometheus_adapter.query_database_health("payment-api")

        self.assertEqual(result["source"], "prometheus/application-db-pool")
        self.assertEqual(set(result["metrics"]), {
            "pool_in_use", "pool_capacity", "pool_available", "pool_overflow",
            "pool_operation_p99_seconds",
        })
        expressions = [call.args[0] for call in query.call_args_list]
        self.assertFalse(any("payment_request_concurrency" in expression for expression in expressions))
        self.assertTrue(any("payment_db_pool_in_use" in expression for expression in expressions))


if __name__ == "__main__":
    unittest.main()

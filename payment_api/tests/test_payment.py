"""当前 Compose 使用的 Payment API 契约测试。"""

import unittest

from prometheus_client import generate_latest

from payment_api.app.main import app


class PaymentApiContractTests(unittest.TestCase):
    def test_required_routes_are_registered(self):
        routes = set(app.openapi()["paths"])
        required_routes = {
            "/metrics",
            "/api/pay",
            "/status",
            "/concurrency",
            "/concurrency/expand",
            "/concurrency/shrink",
        }
        self.assertTrue(required_routes <= routes)

    def test_agent_metrics_are_registered(self):
        metrics = generate_latest().decode("utf-8")
        required_metrics = {
            "payment_request_concurrency_active",
            "payment_request_concurrency_waiting",
            "payment_failed_total",
            "http_server_requests_seconds",
            "payment_db_pool_in_use",
            "payment_db_pool_capacity",
            "payment_db_pool_available",
            "payment_db_pool_overflow",
            "payment_db_operation_seconds",
        }
        for metric in required_metrics:
            self.assertIn(metric, metrics)


if __name__ == "__main__":
    unittest.main()

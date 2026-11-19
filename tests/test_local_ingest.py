import unittest

from apps.ingest.parsers import PARSERS, parse_prometheus


class LocalIngestTests(unittest.TestCase):
    def test_only_prometheus_webhook_is_registered(self):
        self.assertEqual(PARSERS, {"prometheus": parse_prometheus})

    def test_prometheus_alert_is_normalized(self):
        alerts = parse_prometheus({
            "alerts": [{
                "status": "firing",
                "labels": {
                    "service": "payment-api",
                    "environment": "local",
                    "severity": "warning",
                    "alertname": "PaymentApiDown",
                },
                "annotations": {"description": "服务不可用"},
            }]
        })

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].service_id, "payment-api")
        self.assertEqual(alerts[0].environment, "local")
        self.assertEqual(alerts[0].title, "PaymentApiDown")


if __name__ == "__main__":
    unittest.main()

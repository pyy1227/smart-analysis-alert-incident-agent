import importlib
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ComposeConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / "docker-compose.yml").open(encoding="utf-8") as stream:
            cls.compose = yaml.safe_load(stream)

    def test_services_and_internal_connections(self):
        services = self.compose["services"]
        self.assertEqual(set(services), {
            "web", "incident-api", "worker", "mcp-ops-server", "ingest",
            "payment-api", "postgres", "mysql", "redis", "kafka",
            "prometheus", "alertmanager",
        })
        self.assertEqual(self.compose["name"], "incident-agent")
        self.assertEqual(services["worker"]["environment"]["REDIS_HOST"], "redis")
        self.assertEqual(services["worker"]["environment"]["KAFKA_BOOTSTRAP"], "kafka:9092")
        self.assertEqual(services["worker"]["environment"]["MCP_SERVER_URL"], "http://mcp-ops-server:8931/mcp")
        self.assertEqual(services["kafka"]["environment"]["KAFKA_ADVERTISED_LISTENERS"], "INTERNAL://kafka:9092")
        self.assertEqual(services["payment-api"]["build"]["dockerfile"], "dockerfiles/payment-api.Dockerfile")

    def test_alert_chain_and_service_label(self):
        with (ROOT / "infra/prometheus/alertmanager.yml").open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        target = config["receivers"][0]["webhook_configs"][0]["url"]
        self.assertEqual(target, "http://ingest:8100/webhook/prometheus")
        with (ROOT / "infra/prometheus/alert-rules.yml").open(encoding="utf-8") as stream:
            rules = yaml.safe_load(stream)
        self.assertEqual(rules["groups"][0]["rules"][0]["labels"]["service"], "payment-api")

    def test_compose_restart_uses_unified_project_and_container_health(self):
        server = importlib.import_module("apps.mcp-ops-server.server")
        result = Mock(stdout="restarted")
        response = Mock(status_code=200)
        with patch.object(server.subprocess, "run", return_value=result) as run, \
                patch.object(server.httpx, "get", return_value=response) as get:
            actual = server._restart_service_docker("prometheus")
        self.assertEqual(actual["status"], "success")
        self.assertEqual(run.call_args.args[0][-2:], ["restart", "prometheus"])
        self.assertEqual(Path(run.call_args.args[0][3]).name, "docker-compose.yml")
        self.assertEqual(Path(run.call_args.args[0][3]).parent, server.PROJECT_ROOT)
        self.assertEqual(get.call_args.args[0], "http://prometheus:9090/-/ready")


if __name__ == "__main__":
    unittest.main()

import importlib
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from apps.common import docker_adapters as docker


CONTAINER_ID = "a" * 64
SECOND_ID = "b" * 64


def container_data(container_id=CONTAINER_ID, **overrides):
    data = {
        "container_id": container_id,
        "container_name": "/incident-agent-payment-api-1",
        "container_status": "running",
        "running": True,
        "health_status": "healthy",
        "started_at": "2026-10-22T01:00:00Z",
        "finished_at": "0001-01-01T00:00:00Z",
        "exit_code": 0,
        "oom_killed": False,
        "restart_count": 2,
        "project": "incident-agent",
        "service": "payment-api",
    }
    data.update(overrides)
    return data


class DockerServiceStatusTests(unittest.TestCase):
    def setUp(self):
        self.run = patch.object(docker.subprocess, "run").start()
        self.addCleanup(patch.stopall)

    def responses(self, *containers):
        self.run.side_effect = [
            Mock(stdout="\n".join(item["container_id"] for item in containers)),
            Mock(stdout="\n".join(json.dumps(item) for item in containers)),
        ]

    def test_status_uses_labels_and_returns_only_selected_fields(self):
        self.responses(container_data(secret="must-not-leak"))
        result = docker.query_service_status("payment-api")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source"], "docker")
        self.assertIn("observed_at", result)
        row = result["containers"][0]
        self.assertEqual(row["container_name"], "incident-agent-payment-api-1")
        self.assertEqual(row["health_status"], "healthy")
        self.assertEqual(row["restart_count"], 2)
        self.assertNotIn("secret", row)
        self.assertNotIn("project", row)
        listing = self.run.call_args_list[0].args[0]
        self.assertIn("--all", listing)
        self.assertIn("label=com.docker.compose.project=incident-agent", listing)
        self.assertIn("label=com.docker.compose.service=payment-api", listing)
        inspect = self.run.call_args_list[1].args[0]
        self.assertEqual(inspect[:4], ["docker", "container", "inspect", "--format"])
        self.assertEqual(inspect[-1], CONTAINER_ID)
        self.assertNotIn(".Config.Env", inspect[4])
        for call in self.run.call_args_list:
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertTrue(call.kwargs["check"])

    def test_stopped_container_is_successful_evidence(self):
        self.responses(container_data(
            running=False, container_status="exited", exit_code=137,
            oom_killed=True, finished_at="2026-10-22T02:00:00Z",
        ))
        result = docker.query_service_status("payment-api")
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("error", result)
        self.assertFalse(result["containers"][0]["running"])
        self.assertEqual(result["containers"][0]["exit_code"], 137)
        self.assertTrue(result["containers"][0]["oom_killed"])

    def test_health_unknown_or_unhealthy_does_not_become_healthy(self):
        for health in (None, "unknown", "unhealthy", "starting"):
            with self.subTest(health=health):
                self.responses(container_data(health_status=health))
                result = docker.query_service_status("payment-api")
                self.assertEqual(result["containers"][0]["health_status"], health or "unknown")
        self.assertIn('{{else}}"unknown"{{end}}', docker._INSPECT_FORMAT)

    def test_all_instances_are_returned_in_one_inspect(self):
        self.responses(container_data(), container_data(SECOND_ID, running=False))
        result = docker.query_service_status("payment-api")
        self.assertEqual(len(result["containers"]), 2)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args.args[0][-2:], [CONTAINER_ID, SECOND_ID])

    def test_no_matching_container_is_not_found(self):
        self.run.return_value = Mock(stdout="\n")
        result = docker.query_service_status("payment-api")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["containers"], [])
        self.run.assert_called_once()

    def test_unknown_service_rejected_before_docker(self):
        for service in ("unknown", "payment-api; docker restart mysql", "--help"):
            with self.subTest(service=service), self.assertRaises(docker.DockerQueryError):
                docker.query_service_status(service)
        self.run.assert_not_called()

    def test_docker_failures_are_normalized_without_stderr(self):
        errors = [
            FileNotFoundError(), PermissionError(),
            subprocess.TimeoutExpired("docker", 5),
            subprocess.CalledProcessError(1, "docker", stderr="secret-token"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.run.side_effect = error
                with self.assertRaises(docker.DockerQueryError) as raised:
                    docker.query_service_status("payment-api")
                self.assertNotIn("secret-token", str(raised.exception))

    def test_invalid_or_excessive_ids_do_not_reach_inspect(self):
        for output in ("--help", "not-an-id", "\n".join(f"{i:064x}" for i in range(51))):
            self.run.reset_mock()
            self.run.return_value = Mock(stdout=output)
            with self.subTest(output=output[:20]), self.assertRaises(docker.DockerQueryError):
                docker.query_service_status("payment-api")
            self.run.assert_called_once()

    def test_malformed_inspect_response_is_not_success(self):
        for output in ("not-json", "[]", "{}", "", json.dumps(container_data(running="false"))):
            self.run.side_effect = [Mock(stdout=CONTAINER_ID), Mock(stdout=output)]
            with self.subTest(output=output), self.assertRaises(docker.DockerQueryError):
                docker.query_service_status("payment-api")

    def test_wrong_project_service_or_id_is_rejected(self):
        for override in ({"project": "other"}, {"service": "mysql"}, {"container_id": SECOND_ID}):
            self.run.side_effect = [
                Mock(stdout=CONTAINER_ID),
                Mock(stdout=json.dumps(container_data(**override))),
            ]
            with self.subTest(override=override), self.assertRaises(docker.DockerQueryError):
                docker.query_service_status("payment-api")

    def test_container_removed_during_inspect_is_unavailable(self):
        self.run.side_effect = [
            Mock(stdout=CONTAINER_ID),
            subprocess.CalledProcessError(1, "docker"),
        ]
        with self.assertRaises(docker.DockerQueryError):
            docker.query_service_status("payment-api")

    def test_logs_are_bounded_filtered_and_never_pass_query_to_docker(self):
        self.run.side_effect = [
            Mock(stdout=CONTAINER_ID),
            Mock(stdout=json.dumps(container_data())),
            Mock(stdout=(
                "2026-10-25T10:00:00Z request completed\n"
                "2026-10-25T10:00:01Z upstream timeout for order\n"
            )),
        ]

        result = docker.query_service_logs("payment-api", "timeout; docker rm", limit=10)

        self.assertEqual(result["source"], "docker-logs")
        self.assertEqual(result["status"], "returned")
        self.assertEqual(len(result["results"]), 1)
        self.assertIn("upstream timeout", result["results"][0]["message"])
        log_command = self.run.call_args_list[2].args[0]
        self.assertEqual(log_command[:2], ["docker", "logs"])
        self.assertNotIn("timeout; docker rm", log_command)
        self.assertFalse(self.run.call_args_list[2].kwargs["shell"])

    def test_logs_reject_unmanaged_service_and_oversized_query(self):
        with self.assertRaises(docker.DockerQueryError):
            docker.query_service_logs("unknown", "error")
        with self.assertRaises(docker.DockerQueryError):
            docker.query_service_logs("payment-api", "x" * 501)

class MCPDockerStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = importlib.import_module("apps.mcp-ops-server.server")

    def test_mcp_tool_calls_docker_adapter(self):
        expected = {"service_id": "payment-api", "source": "docker", "status": "ok", "containers": []}
        with patch.object(self.server, "ALLOWED_SERVICES", {"payment-api"}), \
                patch.object(self.server, "docker_query_service_status", return_value=expected) as query:
            self.assertEqual(self.server.query_service_status("payment-api"), expected)
            query.assert_called_once_with("payment-api")

    def test_mcp_error_matches_existing_worker_unavailable_contract(self):
        with patch.object(self.server, "ALLOWED_SERVICES", {"payment-api"}), \
                patch.object(self.server, "docker_query_service_status", side_effect=docker.DockerQueryError("Docker unavailable")):
            result = self.server.query_service_status("payment-api")
        self.assertEqual(result["source"], "docker")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"], "Docker unavailable")

    def test_managed_service_restriction_is_preserved(self):
        with patch.object(self.server, "ALLOWED_SERVICES", set()), \
                patch.object(self.server, "docker_query_service_status") as query:
            result = self.server.query_service_status("payment-api")
            query.assert_not_called()
        self.assertEqual(result["status"], "unavailable")

    def test_query_logs_uses_docker_fallback_without_gateway(self):
        expected = {"service_id": "payment-api", "source": "docker-logs", "results": []}
        with patch.object(self.server, "ALLOWED_SERVICES", {"payment-api"}), \
                patch.object(self.server, "docker_query_service_logs", return_value=expected) as query:
            self.assertEqual(self.server.query_logs("payment-api", "timeout"), expected)
            query.assert_called_once_with("payment-api", "timeout")

if __name__ == "__main__":
    unittest.main()

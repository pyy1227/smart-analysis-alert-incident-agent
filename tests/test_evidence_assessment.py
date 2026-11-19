import importlib
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import create_engine, inspect

from apps.api.serialize import serialize
from apps.common.db import Base, Incident
from apps.common.schema_updates import ensure_assessment_columns
from apps.worker.assessment import assess_action_risk, assess_evidence, describe_evidence

from test_action_proposals import concurrency_evidence, docker_evidence, incident_state, suggestion


workflow = importlib.import_module("apps.worker.graph")
CHINA_TIME = ZoneInfo("Asia/Shanghai")


class EvidenceAssessmentTests(unittest.TestCase):
    def test_action_risk_depends_on_action_not_keywords_or_citations(self):
        expected_levels = {
            "none": "none",
            "restart_service": "high", "scale_request_concurrency": "high",
        }
        for action, expected in expected_levels.items():
            for evidence in ([], [docker_evidence()]):
                for citations in ([], [{"source": "runbook"}]):
                    with self.subTest(action=action, evidence=bool(evidence), citations=bool(citations)):
                        state = incident_state(
                            alert={"title": "不要重启，禁止删除"}, evidence=evidence,
                            citations=citations, suggested_action=suggestion(action),
                        )
                        self.assertEqual(assess_action_risk(state)["level"], expected)

    def test_missing_or_failed_structured_suggestion_has_unknown_risk(self):
        cases = [
            incident_state(suggested_action={}),
            incident_state(suggested_action=suggestion("none"), recommendation_error="model failed"),
        ]
        for state in cases:
            with self.subTest(state=state):
                self.assertEqual(assess_action_risk(state)["level"], "unknown")

    def test_no_evidence_is_insufficient_but_not_high_action_risk(self):
        state = incident_state(evidence=[], suggested_action=suggestion("none"))
        self.assertEqual(assess_action_risk(state)["level"], "none")
        result = assess_evidence(state)
        self.assertEqual(result["level"], "insufficient")
        self.assertEqual(result["usable_count"], 0)
        self.assertTrue(result["gaps"])

    def test_real_history_is_context_but_not_current_event_evidence(self):
        item = docker_evidence()
        item["tool"] = "search_incident_history"
        item["result"] = {
            "service_id": "payment-api", "source": "postgres/incidents",
            "status": "returned", "results": [{"incident_id": "older"}],
        }
        result = assess_evidence(incident_state(evidence=[item], suggested_action=suggestion("none")))
        self.assertEqual(result["usable_count"], 0)
        self.assertEqual(result["sources"][0]["kind"], "historical")
        self.assertIn("仅作类比", " ".join(result["gaps"]))

    def test_monitoring_requires_finite_samples_and_zero_is_valid(self):
        for value, usable in (("0", True), ("8", True), ("NaN", False), ("+Inf", False),
                              ("bad", False), (None, False), (True, False)):
            with self.subTest(value=value):
                result = describe_evidence(concurrency_evidence(value), "payment-api")
                self.assertEqual(result["usable"], usable)
                self.assertEqual(result["kind"], "monitoring")
                self.assertIn("空指标不等于零", " ".join(result["limitations"]))

    def test_global_database_metrics_do_not_establish_service_evidence(self):
        item = concurrency_evidence()
        metric = item["result"]["metrics"].pop("request_concurrency_waiting")
        item["tool"] = "query_database_health"
        item["result"]["source"] = "prometheus/database-exporter"
        item["result"]["metrics"]["database_connections"] = metric
        self.assertFalse(describe_evidence(item, "payment-api")["usable"])

    def test_application_database_pool_metrics_are_service_evidence(self):
        item = concurrency_evidence()
        metric = item["result"]["metrics"].pop("request_concurrency_waiting")
        item["tool"] = "query_database_health"
        item["result"]["source"] = "prometheus/application-db-pool"
        item["result"]["metrics"] = {"pool_in_use": metric}
        self.assertTrue(describe_evidence(item, "payment-api")["usable"])

    def test_partial_and_empty_metrics_are_distinguished(self):
        item = concurrency_evidence()
        item["result"]["metrics"]["request_concurrency_active"] = {"data": {"result": []}}
        result = describe_evidence(item, "payment-api")
        self.assertTrue(result["usable"])
        self.assertIn("request_concurrency_active", " ".join(result["gaps"]))
        item["result"]["metrics"]["request_concurrency_waiting"]["data"]["result"] = []
        self.assertFalse(describe_evidence(item, "payment-api")["usable"])

    def test_logs_require_a_known_nonempty_result_list(self):
        cases = [
            ([], False), ({}, False), ({"unrecognized": [1]}, False),
            ([{"message": "timeout"}], True), ({"logs": [{"message": "timeout"}]}, True),
        ]
        for payload, usable in cases:
            item = {
                "tool": "query_logs", "status": "returned",
                "arguments": {"service_id": "payment-api"},
                "result": {"service_id": "payment-api", "source": "log-gateway", "results": payload},
            }
            with self.subTest(payload=payload):
                result = describe_evidence(item, "payment-api")
                self.assertEqual(result["usable"], usable)
                self.assertEqual(result["kind"], "gateway")

    def test_local_docker_logs_are_accepted_as_bounded_log_evidence(self):
        item = {
            "tool": "query_logs", "status": "returned",
            "arguments": {"service_id": "payment-api"},
            "result": {
                "service_id": "payment-api", "source": "docker-logs",
                "status": "returned", "results": [{"message": "upstream timeout"}],
            },
        }
        self.assertTrue(describe_evidence(item, "payment-api")["usable"])

    def test_unusable_status_wrong_service_and_unknown_source_are_excluded(self):
        items = []
        for status in ("failed", "unavailable", "no_data", "truncated", "not_found"):
            item = docker_evidence()
            item["status"] = status
            items.append(item)
        for field, value in (("service_id", "incident-api"), ("source", "invented"), ("truncated", True)):
            item = docker_evidence()
            item["result"][field] = value
            items.append(item)
        for item in items:
            with self.subTest(item=item):
                self.assertFalse(describe_evidence(item, "payment-api")["usable"])

    def test_sufficient_means_only_validated_proposal_not_root_cause(self):
        state = incident_state(suggested_action=suggestion())
        self.assertEqual(assess_evidence(state)["level"], "partial")
        state["proposed_action"] = {"type": "execute_runbook_step", "step": "restart_service"}
        result = assess_evidence(state)
        self.assertEqual(result["level"], "sufficient_for_proposal")
        self.assertIn("不证明根因", " ".join(result["limitations"]))
        state["action_validation_error"] = "提案未通过校验"
        self.assertEqual(assess_evidence(state)["level"], "partial")


class AssessmentPersistenceTests(unittest.TestCase):
    def test_assessment_columns_are_added_repeatably(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE incidents (id TEXT PRIMARY KEY, risk_level TEXT)")
            connection.exec_driver_sql("INSERT INTO incidents VALUES ('old', 'high')")
            ensure_assessment_columns(connection)
            ensure_assessment_columns(connection)
            row = connection.exec_driver_sql("SELECT * FROM incidents").mappings().one()
        self.assertEqual(row["risk_level"], "high")
        self.assertIsNone(row["action_risk"])
        self.assertIsNone(row["evidence_assessment"])
        self.assertIsNone(row["classification"])

    def test_new_database_contains_both_assessment_fields(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
            ensure_assessment_columns(connection)
            columns = {row["name"] for row in inspect(connection).get_columns("incidents")}
        self.assertTrue({"evidence_assessment", "action_risk", "classification"} <= columns)

    def test_api_keeps_assessment_fields_explicit(self):
        incident = Incident(id="test", risk_level="high")
        result = serialize(incident)
        self.assertIsNone(result["action_risk"])
        self.assertIsNone(result["evidence_assessment"])
        self.assertIsNone(result["classification"])
        incident.action_risk = {"level": "none", "reason": "未建议操作"}
        incident.evidence_assessment = {"level": "insufficient"}
        result = serialize(incident)
        self.assertEqual(result["action_risk"]["level"], "none")
        self.assertEqual(result["evidence_assessment"]["level"], "insufficient")


class AssessmentWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_keeps_noop_and_evidence_separate(self):
        state = incident_state(suggested_action=suggestion("none"), evidence=[])
        with patch.object(workflow.settings, "managed_services", "payment-api"):
            state.update(await workflow.plan_action(state))
            state.update(await workflow.verify(state))
        self.assertEqual(state["approval_status"], "not_required")
        self.assertEqual(state["action_risk"]["level"], "none")
        self.assertEqual(state["evidence_assessment"]["level"], "insufficient")
        self.assertEqual(state["proposed_action"]["type"], "none")

    async def test_collect_marks_history_as_context_and_handles_non_object_json(self):
        cases = [
            ("search_incident_history", '{"source":"postgres/incidents","service_id":"payment-api","status":"no_data","results":[]}', "historical"),
            ("query_logs", "[]", "gateway"),
        ]
        for name, content, expected_kind in cases:
            messages = [
                AIMessage(content="", tool_calls=[{
                    "name": name, "args": {"service_id": "payment-api"}, "id": "call-0",
                }]),
                ToolMessage(content=content, tool_call_id="call-0"),
            ]
            with self.subTest(name=name):
                result = await workflow.collect_evidence(incident_state(evidence=[], messages=messages))
                provenance = result["evidence"][0]["provenance"]
                self.assertEqual(provenance["kind"], expected_kind)
                self.assertFalse(provenance["usable"])

    async def test_history_tool_returns_multiple_real_incidents(self):
        server = importlib.import_module("apps.mcp-ops-server.server")
        rows = [
            SimpleNamespace(
                id="old-2", title="较新事件", classification="database", report="连接等待增多",
                proposed_action={"type": "none"}, approval_status="not_required",
                execution_result=None, status="analyzed",
                created_at=datetime(2026, 10, 23, tzinfo=CHINA_TIME),
            ),
            SimpleNamespace(
                id="old-1", title="较早事件", classification="database", report="数据库慢查询",
                proposed_action={"type": "execute_runbook_step", "step": "restart_service"}, approval_status="approved",
                execution_result={"status": "success"}, status="executed",
                created_at=datetime(2026, 10, 22, tzinfo=CHINA_TIME),
            ),
        ]
        scalars = Mock()
        scalars.all.return_value = rows
        session = SimpleNamespace(scalars=AsyncMock(return_value=scalars))
        session_context = AsyncMock()
        session_context.__aenter__.return_value = session
        with patch.object(server, "active_db", SimpleNamespace(session=Mock(return_value=session_context))):
            result = await server.search_incident_history(
                "payment-api", "prod", "database", "current", "tenant-a", "timeout",
            )
        self.assertEqual(result["source"], "postgres/incidents")
        self.assertEqual(result["status"], "returned")
        self.assertEqual([row["incident_id"] for row in result["results"]], ["old-2", "old-1"])
        self.assertEqual(result["results"][1]["execution_result"], {"status": "success"})
        self.assertEqual(result["scope"]["excluded_incident_id"], "current")
        statement = session.scalars.await_args.args[0]
        compiled = statement.compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled)
        for expected in ("tenant-a", "payment-api", "prod", "database", "current"):
            self.assertIn(expected, sql)
        self.assertIn("LIMIT 5", sql)


if __name__ == "__main__":
    unittest.main()

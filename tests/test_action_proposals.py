import copy
import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage

from apps.worker.action_proposals import ActionSuggestion, build_proposed_action


workflow = importlib.import_module("apps.worker.graph")
MANAGED_SERVICES = {"payment-api", "incident-api", "prometheus", "alertmanager"}


def suggestion(action="restart_service", **overrides):
    params = {} if action == "none" else {"service_id": "payment-api"}
    if action == "scale_request_concurrency":
        params["concurrency_limit"] = 20
    return {
        "action": action, "params": params, "reason": "依据当前事件证据提出建议",
        "evidence_refs": [] if action == "none" else [0],
        "missing_information": [], **overrides,
    }


def docker_evidence():
    return {
        "tool": "query_service_status", "arguments": {"service_id": "payment-api"},
        "status": "returned", "collected_at": "2026-10-24T01:00:00Z",
        "result": {
            "service_id": "payment-api", "source": "docker", "status": "ok",
            "containers": [{"service_id": "payment-api", "running": False, "health_status": "unknown"}],
        },
    }


def concurrency_evidence(waiting="8"):
    return {
        "tool": "query_service_metrics", "arguments": {"service_id": "payment-api"},
        "status": "returned", "collected_at": "2026-10-24T01:00:00Z",
        "result": {
            "service_id": "payment-api", "source": "prometheus",
            "metrics": {"request_concurrency_waiting": {
                "expression": 'max(payment_request_concurrency_waiting{service="payment-api"})',
                "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1792803600, waiting]}]},
            }},
        },
    }


def incident_state(**overrides):
    return {
        "incident_id": "incident-test", "service_id": "payment-api", "risk_level": "low",
        "alert": {"title": "服务超时", "description": "需要排查"},
        "report": "已有报告", "evidence": [docker_evidence()],
        "messages": [AIMessage(content="已收集证据")], **overrides,
    }


class ProposalValidationTests(unittest.TestCase):
    def build(self, proposed, state=None):
        return build_proposed_action(
            ActionSuggestion.model_validate(proposed), state or incident_state(), MANAGED_SERVICES,
        )

    def test_schema_rejects_unknown_actions_parameters_and_non_integer_sizes(self):
        invalid = [
            suggestion(params={"service_id": "payment-api", "command": "restart"}),
            suggestion(params={}), suggestion(params={"service_id": 123}),
            suggestion(requires_approval=False), suggestion(approval_id="invented"),
            suggestion(reason=" "), suggestion(evidence_refs=[True]),
            suggestion(evidence_refs=[-1]), suggestion(evidence_refs=[0, 0]),
            suggestion("none", params={"service_id": "payment-api"}),
        ]
        invalid.extend(suggestion("scale_request_concurrency", params={"service_id": "payment-api", "concurrency_limit": size})
                       for size in (0, 201, True, "20", 20.0, None))
        for proposed in invalid:
            with self.subTest(proposed=proposed), self.assertRaises(ValueError):
                ActionSuggestion.model_validate(proposed)

    def test_requires_actual_referenced_evidence_for_same_service(self):
        base = incident_state()
        cases = []
        for status in ("failed", "unavailable", "not_found", "no_data", "sample", "truncated"):
            state = copy.deepcopy(base)
            state["evidence"][0]["status"] = status
            cases.append(state)
        for field, value in (("source", "unknown"), ("service_id", "incident-api"),
                             ("error", "failed"), ("truncated", True)):
            state = copy.deepcopy(base)
            state["evidence"][0]["result"][field] = value
            cases.append(state)
        for field, value in (("arguments", {"service_id": "incident-api"}),
                             ("tool", "search_incident_history")):
            state = copy.deepcopy(base)
            state["evidence"][0][field] = value
            cases.append(state)
        for state in cases:
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.build(suggestion(), state)
        for refs in ([], [1], [100]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.build(suggestion(evidence_refs=refs))
    def test_cross_service_and_unmanaged_targets_are_rejected(self):
        with self.assertRaises(ValueError):
            self.build(suggestion(params={"service_id": "incident-api"}))
        with self.assertRaises(ValueError):
            build_proposed_action(ActionSuggestion.model_validate(suggestion()), incident_state(), set())

    def test_restart_requires_stopped_or_unhealthy_container(self):
        for running, health, allowed in ((True, "healthy", False), (True, "unknown", False),
                                         (True, "starting", False), (True, "unhealthy", True),
                                         (False, "unknown", True)):
            state = incident_state()
            state["evidence"][0]["result"]["containers"][0].update(running=running, health_status=health)
            with self.subTest(running=running, health=health):
                if allowed:
                    self.assertEqual(self.build(suggestion(), state)["step"], "restart_service")
                else:
                    with self.assertRaises(ValueError):
                        self.build(suggestion(), state)

    def test_web_reference_cannot_support_restart(self):
        web_evidence = {
            "tool": "search_web_solutions",
            "arguments": {"service_id": "payment-api", "query": "timeout"},
            "status": "returned",
            "result": {
                "service_id": "payment-api",
                "source": "tavily/web",
                "status": "returned",
                "results": [{"title": "建议重启", "url": "https://example.com"}],
            },
        }
        with self.assertRaises(ValueError):
            self.build(suggestion(), incident_state(evidence=[web_evidence]))

    def test_runbook_rejected_on_model_failure_or_missing_information(self):
        for error in ("agent_error", "recommendation_error"):
            with self.subTest(error=error), self.assertRaises(ValueError):
                self.build(suggestion(), incident_state(**{error: "model failed"}))
        with self.assertRaises(ValueError):
            self.build(suggestion(missing_information=["需要确认业务影响"]))

    def test_concurrency_action_uses_positive_waiting_metric_and_checked_target(self):
        for size in (1, 20, 200):
            action = self.build(
                suggestion("scale_request_concurrency", params={"service_id": "payment-api", "concurrency_limit": size}),
                incident_state(evidence=[concurrency_evidence()]),
            )
            self.assertEqual(action, {
                "type": "execute_runbook_step", "step": "scale_request_concurrency",
                "params": {"service_id": "payment-api", "concurrency_limit": size}, "requires_approval": True,
            })
        for value in ("0", "-1", "NaN", "Inf", "-Inf", "bad", None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.build(suggestion("scale_request_concurrency"), incident_state(evidence=[concurrency_evidence(value)]))
        with self.assertRaises(ValueError):
            self.build(suggestion("scale_request_concurrency"))  # Docker 异常不能替代请求等待指标。

    def test_concurrency_empty_or_other_service_metrics_are_rejected(self):
        item = concurrency_evidence()
        item["result"]["metrics"]["request_concurrency_waiting"]["data"]["result"] = []
        with self.assertRaises(ValueError):
            self.build(suggestion("scale_request_concurrency"), incident_state(evidence=[item]))
        item = concurrency_evidence()
        item["arguments"]["service_id"] = item["result"]["service_id"] = "incident-api"
        with self.assertRaises(ValueError):
            self.build(suggestion("scale_request_concurrency", params={"service_id": "incident-api", "concurrency_limit": 20}),
                       incident_state(service_id="incident-api", evidence=[item]))


class ClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_id_db_substring_does_not_change_application_classification(self):
        result = await workflow.classify({"alert": {
            "title": "PaymentRequestConcurrencySaturated",
            "description": "请求并发限制器饱和",
            "trace_id": "trace-12db34",
            "raw": {"generatorURL": "http://example/abcdb123"},
        }})
        self.assertEqual(result["classification"], "application")

    async def test_explicit_database_signal_is_classified_as_database(self):
        result = await workflow.classify({"alert": {
            "title": "DatabasePoolExhausted",
            "description": "MySQL 连接池耗尽",
        }})
        self.assertEqual(result["classification"], "database")


class ProposalGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_none_never_triggers_keyword_action(self):
        for phrase in ("不要重启", "暂不建议 restart", "需要重启"):
            state = incident_state(
                alert={"title": phrase}, summary_text=phrase, risk_level="medium",
                suggested_action=suggestion("none", reason="暂不建议重启"), approval_id="old-approval",
            )
            # 容器健康时，文本里出现"重启"也不应生成操作提案。
            state["evidence"][0]["result"]["containers"][0].update(running=True, health_status="healthy")
            with self.subTest(phrase=phrase), patch.object(workflow, "get_agent_model") as get_model:
                result = await workflow.plan_action(state)
            get_model.assert_not_called()
            self.assertEqual(result["proposed_action"]["type"], "none")
            self.assertEqual(result["approval_status"], "not_required")
            self.assertEqual(result["approval_id"], "")

    async def test_structured_restart_works_without_restart_keyword(self):
        with patch.object(workflow.settings, "managed_services", "payment-api"):
            result = await workflow.plan_action(incident_state(suggested_action=suggestion()))
        self.assertEqual(result["proposed_action"]["step"], "restart_service")
        self.assertEqual(result["approval_status"], "pending")
        self.assertTrue(result["approval_id"].startswith("appr-"))
        self.assertEqual(result["risk_level"], "high")
        self.assertIn("尚未执行", result["report"])

    async def test_rejected_proposal_is_visible_and_does_not_create_approval(self):
        with patch.object(workflow.settings, "managed_services", "payment-api"):
            result = await workflow.plan_action(incident_state(
                suggested_action=suggestion(missing_information=["缺少业务影响确认"]),
            ))
        self.assertEqual(result["approval_status"], "not_required")
        self.assertIn("缺少业务影响确认", result["report"])
        self.assertIn("未生成操作确认", result["report"])
        self.assertTrue(result["action_validation_error"])
        self.assertEqual(result["risk_level"], "high")

    async def test_summary_returns_structured_state_and_evidence_ids(self):
        payload = {"summary_text": "建议经人工审批后处理", "suggested_action": suggestion()}
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content=json.dumps(payload))))
        with patch.object(workflow, "get_agent_model", return_value=model) as get_model:
            result = await workflow.analyze(incident_state())
        get_model.assert_called_once_with(allow_tools=False)
        self.assertEqual(result["suggested_action"], payload["suggested_action"])
        self.assertEqual(result["summary_text"], payload["summary_text"])
        self.assertEqual(result["recommendation_error"], "")
        context = json.loads(model.ainvoke.call_args.args[0][-1].content)
        self.assertEqual(context["evidence"][0]["evidence_id"], 0)
        self.assertEqual(context["service_id"], "payment-api")
        self.assertIn("[证据 0]", result["report"])

    async def test_invalid_summary_or_timeout_falls_back_without_approval(self):
        for response in (
            "不要重启", "```json\n{}\n```", "{}", "null",
            json.dumps({"summary_text": "重启", "suggested_action": suggestion(evidence_refs=["0"])}),
            TimeoutError(),
        ):
            model = SimpleNamespace(ainvoke=AsyncMock())
            if isinstance(response, Exception):
                model.ainvoke.side_effect = response
            else:
                model.ainvoke.return_value = AIMessage(content=response)
            with self.subTest(response=response), patch.object(workflow, "get_agent_model", return_value=model):
                state = incident_state()
                state.update(await workflow.analyze(state))
                result = await workflow.plan_action(state)
                self.assertEqual(state["suggested_action"]["action"], "none")
                self.assertTrue(state["recommendation_error"])
                self.assertEqual(result["approval_status"], "not_required")
                self.assertIn("关键证据", result["report"])

    async def test_graph_persists_model_proposal_as_pending_approval(self):
        for step, item in (("restart_service", docker_evidence()), ("scale_request_concurrency", concurrency_evidence())):
            payload = {"summary_text": "建议人工审批后处置", "suggested_action": suggestion(step)}
            model = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
                AIMessage(content="已分析证据"), AIMessage(content=json.dumps(payload)),
            ]))
            incident = SimpleNamespace()
            session = Mock()
            session.get = AsyncMock(return_value=incident)
            session.commit = AsyncMock()
            session_context = AsyncMock()
            session_context.__aenter__.return_value = session
            with self.subTest(step=step), \
                    patch.object(workflow.settings, "managed_services", "payment-api"), \
                    patch.object(workflow, "duplicate", AsyncMock(return_value={"duplicate": False})), \
                    patch.object(workflow, "agent_node", AsyncMock(return_value={"messages": [AIMessage(content="补查完成")]})), \
                    patch.object(workflow, "retrieve_node", AsyncMock(return_value={"retrieved_docs": [], "citations": []})), \
                    patch.object(workflow, "get_agent_model", return_value=model), \
                    patch.object(workflow, "active_db", SimpleNamespace(session=Mock(return_value=session_context))):
                result = await workflow.build_graph().ainvoke(incident_state(evidence=[item]))
            self.assertEqual(result["suggested_action"], payload["suggested_action"])
            self.assertEqual(incident.proposed_action["step"], step)
            self.assertEqual(incident.classification, "application")
            self.assertEqual(incident.approval_status, "pending")
            self.assertEqual(incident.action_risk["level"], "high")
            self.assertEqual(incident.evidence_assessment["level"], "sufficient_for_proposal")
            self.assertIn("证据与风险", incident.report)
            approvals = [call.args[0] for call in session.add.call_args_list if isinstance(call.args[0], workflow.Approval)]
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0].action, incident.proposed_action)
            self.assertEqual(approvals[0].status, "pending")
            self.assertIn("建议动作", incident.report)
            session.commit.assert_awaited_once()
            self.assertEqual(model.ainvoke.await_count, 2)


class ContainerEvidenceFallbackTests(unittest.IsolatedAsyncioTestCase):
    """模型明确选择 none 时，后端按 Docker 证据兜底生成重启提案。"""

    async def test_model_none_with_stopped_container_creates_proposal(self):
        with patch.object(workflow.settings, "managed_services", "payment-api"):
            result = await workflow.plan_action(incident_state(
                suggested_action=suggestion("none", reason="根因未明，先取证"),
            ))
        self.assertEqual(result["proposed_action"]["step"], "restart_service")
        self.assertEqual(result["proposed_action"]["params"], {"service_id": "payment-api"})
        self.assertEqual(result["approval_status"], "pending")
        self.assertTrue(result["approval_id"].startswith("appr-"))
        self.assertEqual(result["action_validation_error"], "")
        self.assertEqual(result["suggested_action"]["action"], "restart_service")
        self.assertEqual(result["risk_level"], "high")
        self.assertIn("后端已按 Docker 容器异常证据生成重启提案", result["report"])

    async def test_healthy_container_keeps_model_none(self):
        state = incident_state(suggested_action=suggestion("none", reason="暂不需要重启"))
        state["evidence"][0]["result"]["containers"][0].update(running=True, health_status="healthy")
        with patch.object(workflow.settings, "managed_services", "payment-api"):
            result = await workflow.plan_action(state)
        self.assertEqual(result["proposed_action"]["type"], "none")
        self.assertEqual(result["approval_status"], "not_required")

    async def test_wrong_evidence_refs_are_rebuilt_from_container_evidence(self):
        """模型想重启但引用错证据（机械性错误）时，后端按证据重建提案。"""
        no_data = {
            "tool": "query_service_metrics", "arguments": {"service_id": "payment-api"},
            "status": "no_data", "collected_at": "2026-11-19T09:10:43+08:00",
            "result": {"service_id": "payment-api", "source": "prometheus",
                       "metrics": {"request_concurrency_waiting": {"data": {"result": []}}}},
        }
        state = incident_state(
            evidence=[docker_evidence(), no_data],
            suggested_action=suggestion(evidence_refs=[1]),
        )
        with patch.object(workflow.settings, "managed_services", "payment-api"):
            result = await workflow.plan_action(state)
        self.assertEqual(result["proposed_action"]["step"], "restart_service")
        self.assertEqual(result["approval_status"], "pending")
        self.assertEqual(result["action_validation_error"], "")

    async def test_model_write_suggestion_is_not_overridden_by_backend(self):
        with patch.object(workflow.settings, "managed_services", "payment-api"):
            result = await workflow.plan_action(incident_state(
                suggested_action=suggestion(missing_information=["需要确认业务影响"]),
            ))
        self.assertEqual(result["proposed_action"]["type"], "none")
        self.assertEqual(result["approval_status"], "not_required")
        self.assertIn("需要确认业务影响", result["report"])


if __name__ == "__main__":
    unittest.main()

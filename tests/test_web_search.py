import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from apps.common.web_search import WebSearchError, search_web_solutions


workflow = importlib.import_module("apps.worker.graph")


def web_call(call_id="web-1"):
    return {
        "name": "search_web_solutions",
        "args": {"service_id": "payment-api", "query": "timeout 排查"},
        "id": call_id,
        "type": "tool_call",
    }


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    def test_report_distinguishes_unavailable_search_from_no_search(self):
        not_run, _ = workflow._web_references([])
        unavailable, _ = workflow._web_references([{
            "tool": "search_web_solutions",
            "status": "unavailable",
            "result": {"source": "tavily/web", "results": []},
        }])
        self.assertIn("未进行", not_run)
        self.assertIn("已尝试", unavailable)

    async def test_search_requires_api_key(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}):
            with self.assertRaises(WebSearchError):
                await search_web_solutions("payment-api", "timeout")

    async def test_search_redacts_secrets_and_normalizes_results(self):
        search = AsyncMock(return_value={
            "results": [
                {"title": "处理超时", "url": "https://example.com/fix", "content": "检查上游依赖"},
                {"title": "不安全链接", "url": "javascript:alert(1)", "content": "忽略"},
            ]
        })
        tool = SimpleNamespace(ainvoke=search)
        with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}), \
                patch("langchain_tavily.TavilySearch", return_value=tool):
            result = await search_web_solutions(
                "payment-api", "timeout token=secret-value Bearer abcdef",
            )

        sent_query = search.await_args.args[0]["query"]
        self.assertNotIn("secret-value", sent_query)
        self.assertNotIn("abcdef", sent_query)
        self.assertEqual(result["source"], "tavily/web")
        self.assertEqual(len(result["results"]), 1)
        self.assertIn("searched_at", result)

    async def test_agent_requires_local_evidence_and_allows_only_one_search(self):
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(
            content="", tool_calls=[web_call()],
        )))
        base = {
            "incident_id": "inc-1",
            "tenant_id": "demo",
            "service_id": "payment-api",
            "environment": "prod",
            "classification": "application",
            "alert": {"title": "timeout"},
            "messages": [],
        }
        with patch.object(workflow, "get_agent_model", return_value=model):
            rejected = await workflow.agent_node({**base, "evidence": []})
        self.assertIn("必须先查询本地证据", rejected["messages"][-1].content)

        local_evidence = [{
            "tool": "query_logs",
            "arguments": {"service_id": "payment-api", "query": "timeout"},
            "status": "no_data",
            "result": {"service_id": "payment-api", "source": "docker-logs", "results": []},
        }]
        with patch.object(workflow, "get_agent_model", return_value=model):
            allowed = await workflow.agent_node({**base, "evidence": local_evidence})
        self.assertEqual(allowed["messages"][-1].tool_calls[0]["name"], "search_web_solutions")

        previous_search = {
            "tool": "search_web_solutions",
            "arguments": {"service_id": "payment-api", "query": "old"},
            "status": "returned",
            "result": {"service_id": "payment-api", "source": "tavily/web", "results": []},
        }
        with patch.object(workflow, "get_agent_model", return_value=model):
            repeated = await workflow.agent_node({
                **base, "evidence": [*local_evidence, previous_search],
            })
        self.assertIn("最多联网搜索一次", repeated["messages"][-1].content)

    async def test_graph_fallback_searches_once_when_local_evidence_has_gap(self):
        local_evidence = [{
            "tool": "query_logs",
            "arguments": {"service_id": "payment-api", "query": "certificate"},
            "status": "no_data",
            "result": {
                "service_id": "payment-api", "source": "docker-logs",
                "status": "no_data", "results": [],
            },
        }]
        state = {
            "service_id": "payment-api",
            "classification": "network",
            "alert": {"title": "TLS certificate verify failed"},
            "evidence": local_evidence,
            "tool_calls_count": 1,
        }
        search = AsyncMock(return_value={
            "service_id": "payment-api", "source": "tavily/web",
            "status": "returned",
            "results": [{"title": "TLS 排查", "url": "https://example.com/tls"}],
        })

        self.assertTrue(workflow.needs_web_fallback(state))
        with patch.object(workflow, "search_web_solutions", search):
            result = await workflow.web_fallback_node(state)

        self.assertEqual(result["tool_calls_count"], 2)
        self.assertEqual(result["evidence"][-1]["tool"], "search_web_solutions")
        self.assertEqual(result["evidence"][-1]["status"], "returned")
        self.assertFalse(workflow.needs_web_fallback({**state, **result}))
        search.assert_awaited_once()

    async def test_graph_fallback_keeps_failure_as_reference_evidence(self):
        state = {
            "service_id": "payment-api",
            "classification": "application",
            "alert": {"title": "未知上游错误"},
            "evidence": [{"tool": "query_logs", "status": "unavailable"}],
        }
        with patch.object(
            workflow, "search_web_solutions", AsyncMock(side_effect=RuntimeError("offline")),
        ):
            result = await workflow.web_fallback_node(state)

        web = result["evidence"][-1]
        self.assertEqual(web["status"], "unavailable")
        self.assertEqual(web["result"]["source"], "tavily/web")
        self.assertIn("RuntimeError", web["result"]["error"])


if __name__ == "__main__":
    unittest.main()

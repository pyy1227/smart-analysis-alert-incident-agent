import importlib
import json
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


workflow = importlib.import_module("apps.worker.graph")


def tool_request(index):
    return {
        "name": "query_logs",
        "args": {"service_id": "payment-api", "query": f"batch-{index}"},
        "id": f"call-{index}",
        "type": "tool_call",
    }


class AgentFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def run_workflow(self, batches, *, final_error=None, summary_error=None):
        decisions = [
            AIMessage(content="", tool_calls=[tool_request(i) for i in batch])
            for batch in batches
        ]
        decision_model = SimpleNamespace(ainvoke=AsyncMock(side_effect=decisions))
        text_model = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
            final_error or AIMessage(content="最终分析：已检查最后一批日志，原因仍需人工确认。"),
            summary_error or AIMessage(content=json.dumps({
                "summary_text": "建议人工复核日志。",
                "suggested_action": {
                    "action": "none", "params": {}, "reason": "证据不足，暂不执行操作",
                    "evidence_refs": [], "missing_information": ["需要人工复核日志"],
                },
            }, ensure_ascii=False)),
        ]))

        async def query_logs(service_id, query):
            return {"service_id": service_id, "latest_batch": query}

        query = AsyncMock(side_effect=query_logs)
        publish = AsyncMock(return_value={})
        with ExitStack() as stack:
            stack.enter_context(patch.dict(workflow.READ_ONLY_TOOLS, {"query_logs": query}))
            stack.enter_context(patch.object(
                workflow, "tool_node", workflow.ToolNode(workflow.build_agent_tools(), handle_tool_errors=True),
            ))
            stack.enter_context(patch.object(workflow, "duplicate", AsyncMock(return_value={"duplicate": False})))
            stack.enter_context(patch.object(workflow, "retrieve_node", AsyncMock(return_value={
                "retrieved_docs": [], "citations": [],
            })))
            stack.enter_context(patch.object(workflow, "publish_result", publish))
            get_model = stack.enter_context(patch.object(
                workflow, "get_agent_model",
                side_effect=lambda *, allow_tools=True: decision_model if allow_tools else text_model,
            ))
            graph = workflow.build_graph()
            result = await graph.ainvoke({
                "incident_id": "incident-test", "service_id": "payment-api",
                "alert": {"title": "请求超时", "description": "需要排查日志"},
            })

        publish.assert_awaited_once()
        self.assertEqual(decision_model.ainvoke.await_count, len(batches))
        self.assertEqual(text_model.ainvoke.await_count, 2)
        self.assertEqual(sum(call.kwargs == {"allow_tools": False} for call in get_model.call_args_list), 2)
        self.assertEqual(result["tool_calls_count"], query.await_count)
        self.assertEqual(len(result["evidence"]), query.await_count)
        return result, text_model, query

    async def test_round_limit_analyzes_last_tool_results_once(self):
        result, text_model, query = await self.run_workflow([[0], [1], [2]])
        self.assertEqual(result["agent_rounds"], workflow.MAX_AGENT_ROUNDS)
        self.assertEqual(query.await_count, 3)
        messages = text_model.ainvoke.call_args_list[0].args[0]
        self.assertEqual([m.tool_call_id for m in messages if isinstance(m, ToolMessage)],
                         ["call-0", "call-1", "call-2"])
        context = json.loads(messages[-1].content)
        self.assertEqual(context["evidence"][-1]["result"]["latest_batch"], "batch-2")
        self.assertIn("建议人工复核日志", result["report"])
        self.assertIn("本轮查询次数已用完", result["report"])
        self.assertNotIn("尚未获得完整的模型结论", result["report"])

    async def test_tool_limit_analyzes_all_six_results_without_more_tools(self):
        result, text_model, query = await self.run_workflow([[0, 1, 2], [3, 4, 5]])
        self.assertEqual(query.await_count, workflow.MAX_TOOL_CALLS)
        self.assertEqual(result["agent_rounds"], 2)
        messages = text_model.ainvoke.call_args_list[0].args[0]
        self.assertEqual(len([m for m in messages if isinstance(m, ToolMessage)]), 6)
        self.assertEqual(json.loads(messages[-1].content)["evidence"][-1]["result"]["latest_batch"], "batch-5")
        self.assertIn("建议人工复核日志", result["report"])

    async def test_normal_completion_also_finalizes(self):
        result, _, query = await self.run_workflow([[0], []])
        self.assertEqual(query.await_count, 1)
        self.assertIn("建议人工复核日志", result["report"])
        self.assertNotIn("本轮查询次数已用完", result["report"])
        self.assertIn("## 分析结论\n建议人工复核日志。", result["report"])

    async def test_over_budget_batch_is_not_executed_but_existing_evidence_is_analyzed(self):
        result, text_model, query = await self.run_workflow([[0, 1, 2, 3, 4], [5, 6]])
        self.assertEqual(query.await_count, 5)
        messages = text_model.ainvoke.call_args_list[0].args[0]
        self.assertIn("超过工具调用上限", messages[-2].content)
        self.assertEqual(len(json.loads(messages[-1].content)["evidence"]), 5)
        self.assertIn("建议人工复核日志", result["report"])

    async def test_finalization_and_summary_failure_still_publish_evidence(self):
        result, _, query = await self.run_workflow(
            [[0], [1], [2]], final_error=TimeoutError(), summary_error=RuntimeError("model offline"),
        )
        self.assertEqual(query.await_count, 3)
        self.assertIn("最终分析不可用：TimeoutError", result["agent_error"])
        self.assertIn("batch-2", result["report"])
        self.assertIn("请人工复核", result["report"])
        self.assertEqual(result["action_risk"]["level"], "unknown")
        self.assertEqual(result["evidence_assessment"]["level"], "insufficient")

    async def test_finalization_rejects_tool_requests_and_empty_output(self):
        for response in (
            AIMessage(content="", tool_calls=[tool_request(9)]),
            AIMessage(content="   "),
            AIMessage(content="", invalid_tool_calls=[{
                "name": "query_logs", "args": "{", "id": "invalid", "error": "invalid JSON",
            }]),
        ):
            with self.subTest(response=response):
                model = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
                with patch.object(workflow, "get_agent_model", return_value=model) as get_model:
                    result = await workflow.finalize_node({
                        "service_id": "payment-api", "alert": {}, "messages": [],
                    })
                get_model.assert_called_once_with(allow_tools=False)
                self.assertIn("ValueError", result["agent_error"])
                self.assertFalse(result["messages"][-1].tool_calls)

    async def test_finalization_timeout_is_bounded(self):
        import asyncio

        cancelled = asyncio.Event()

        async def slow_model(messages):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(workflow, "MODEL_TIMEOUT", 0.01), patch.object(
            workflow, "get_agent_model", return_value=SimpleNamespace(ainvoke=slow_model),
        ):
            result = await workflow.finalize_node({
                "service_id": "payment-api", "alert": {}, "messages": [HumanMessage(content="告警")],
            })
        self.assertTrue(cancelled.is_set())
        self.assertIn("TimeoutError", result["agent_error"])

    def test_finalization_uses_model_without_binding_tools(self):
        model = Mock()
        with patch.dict(sys.modules, {"apps.common.llm": SimpleNamespace(model=model)}):
            self.assertIs(workflow.get_agent_model(allow_tools=False), model)
            model.bind_tools.assert_not_called()
            self.assertIs(workflow.get_agent_model(), model.bind_tools.return_value)
            model.bind_tools.assert_called_once_with(workflow.AGENT_TOOLS)


if __name__ == "__main__":
    unittest.main()

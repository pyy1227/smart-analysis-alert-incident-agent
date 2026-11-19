import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


copilot = importlib.import_module("apps.agent.copilot")
incident_routes = importlib.import_module("apps.routers.incidents")


def incident():
    return SimpleNamespace(
        id="inc-stream",
        title="支付接口延迟",
        service_id="payment-api",
        environment="staging",
        severity="warning",
        status="analyzed",
        risk_level="none",
        action_risk={"level": "none"},
        evidence_assessment={"level": "partial"},
        report="当前证据不足。",
        proposed_action={"type": "none"},
        citations=[],
        approval_status="not_required",
        classification="application",
        tenant_id="demo",
    )


class StreamingModel:
    async def astream(self, messages):
        self.messages = messages
        for text in ("第一段", "，第二段"):
            yield SimpleNamespace(content=text)


class FailingStreamingModel:
    async def astream(self, messages):
        if False:
            yield None
        raise RuntimeError("offline")


class FakeSession:
    def __init__(self, current_incident, added):
        self.current_incident = current_incident
        self.added = added

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, model, key):
        return self.current_incident

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        pass


class FakeDatabase:
    def __init__(self, current_incident):
        self.current_incident = current_incident
        self.added = []

    def session(self):
        return FakeSession(self.current_incident, self.added)


class CopilotStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_model_chunks(self):
        model = StreamingModel()
        with patch("apps.common.llm.model", model):
            chunks = [chunk async for chunk in copilot.stream_prepared_answer("问题", "降级")]
        self.assertEqual(chunks, ["第一段", "，第二段"])

    async def test_stream_falls_back_when_model_fails_before_output(self):
        model = FailingStreamingModel()
        with patch("apps.common.llm.model", model):
            chunks = [chunk async for chunk in copilot.stream_prepared_answer("问题", "降级回答")]
        self.assertEqual(chunks, ["降级回答"])

    async def test_prepare_collects_matching_read_tool_evidence(self):
        status = AsyncMock(return_value={
            "service_id": "payment-api", "source": "docker", "status": "returned",
        })
        with patch.object(copilot, "query_service_status", status):
            prompt, evidence, fallback = await copilot.prepare_incident_answer(
                incident(), "服务状态是否健康？",
            )
        status.assert_awaited_once_with("payment-api")
        self.assertEqual(evidence[0]["tool"], "query_service_status")
        self.assertIn("服务状态是否健康", prompt)
        self.assertIn("当前状态", fallback)

    async def test_stream_endpoint_emits_sse_and_persists_complete_answer(self):
        current = incident()
        current.trace_id = "trace-stream"
        database = FakeDatabase(current)

        async def chunks(prompt, fallback):
            yield "流式"
            yield "回答"

        with patch.object(incident_routes, "active_db", database), \
                patch.object(
                    incident_routes, "prepare_incident_answer",
                    AsyncMock(return_value=("提示词", [{"tool": "query_service_status"}], "降级")),
                ), \
                patch.object(incident_routes, "stream_prepared_answer", chunks):
            response = await incident_routes.stream_agent_message(
                current.id,
                incident_routes.AgentMessageIn(content="现在状态如何？"),
            )
            pieces = []
            async for piece in response.body_iterator:
                pieces.append(piece.decode() if isinstance(piece, bytes) else piece)

        body = "".join(pieces)
        self.assertIn("event: delta", body)
        self.assertIn('"content": "流式"', body)
        self.assertIn("event: done", body)
        assistant = next(
            value for value in database.added
            if getattr(value, "role", None) == "assistant"
        )
        self.assertEqual(assistant.content, "流式回答")
        self.assertEqual(assistant.tool_calls[0]["tool"], "query_service_status")


if __name__ == "__main__":
    unittest.main()

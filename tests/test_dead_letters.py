import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, inspect

from apps.common.db import Approval, AuditLog, Base, DeadLetter, Incident
from apps.routers import dead_letters as dead_letter_router
from apps.worker import dead_letters as dead_letter_writer


class FakeDatabase:
    def __init__(self):
        self.dead_letters = {}
        self.incidents = {}
        self.approvals = {}
        self.audits = []

    @asynccontextmanager
    async def session(self):
        database = self

        class Session:
            def add(self, item):
                if isinstance(item, DeadLetter):
                    database.dead_letters[item.id] = item
                elif isinstance(item, Incident):
                    database.incidents[item.id] = item
                elif isinstance(item, Approval):
                    database.approvals[item.id] = item
                elif isinstance(item, AuditLog):
                    database.audits.append(item)

            async def get(self, model, key):
                stores = {
                    DeadLetter: database.dead_letters,
                    Incident: database.incidents,
                    Approval: database.approvals,
                }
                return stores[model].get(key)

            async def commit(self):
                return None

        yield Session()


class FakeProducer:
    def __init__(self):
        self.calls = []

    async def send_and_wait(self, topic, payload, **kwargs):
        self.calls.append((topic, payload, kwargs))
        return SimpleNamespace(partition=2, offset=17)


class FailingProducer:
    async def send_and_wait(self, topic, payload, **kwargs):
        raise RuntimeError("Kafka 不可用")


class DeadLetterTests(unittest.IsolatedAsyncioTestCase):
    def test_dead_letter_table_is_created(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.assertIn("dead_letters", inspect(engine).get_table_names())

    async def test_failure_is_saved_and_confirmed_by_kafka_metadata(self):
        database = FakeDatabase()
        producer = FakeProducer()
        with patch.object(dead_letter_writer, "active_db", database):
            result = await dead_letter_writer.publish_dead_letter(
                producer,
                "incident.analysis.dlq",
                "analysis",
                {"incident_id": "inc-1"},
                RuntimeError("模型调用失败"),
            )

        item = database.dead_letters[result["id"]]
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.kafka_partition, 2)
        self.assertEqual(item.kafka_offset, 17)
        self.assertEqual(producer.calls[0][1]["dead_letter_id"], item.id)

    async def test_kafka_failure_remains_visible_in_database(self):
        database = FakeDatabase()
        with patch.object(dead_letter_writer, "active_db", database):
            result = await dead_letter_writer.publish_dead_letter(
                FailingProducer(),
                "incident.action.dlq",
                "action",
                {"incident_id": "inc-1"},
                RuntimeError("操作失败"),
            )

        item = database.dead_letters[result["id"]]
        self.assertEqual(item.status, "publish_failed")
        self.assertEqual(item.publish_error, "Kafka 不可用")

    async def test_action_replay_uses_new_approval_and_idempotency_key(self):
        database = FakeDatabase()
        producer = FakeProducer()
        incident = Incident(
            id="inc-1", event_id="event-1", service_id="payment-api",
            status="execution_failed", alert={}, trace_id="trace-1",
        )
        database.incidents[incident.id] = incident
        database.dead_letters["dlq-1"] = DeadLetter(
            id="dlq-1",
            incident_id=incident.id,
            stage="action",
            error="执行失败",
            payload={
                "incident_id": incident.id,
                "approval_id": "appr-old",
                "idempotency_key": "inc-1:appr-old",
                "action": {
                    "type": "execute_runbook_step",
                    "step": "restart_service",
                    "params": {"service_id": "payment-api"},
                },
            },
            status="pending",
            kafka_topic="incident.action.dlq",
        )
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(producer=producer)))

        with patch.object(dead_letter_router, "active_db", database):
            result = await dead_letter_router.replay_dead_letter(
                "dlq-1", dead_letter_router.ReplayIn(confirmed=True), request,
            )

        self.assertEqual(result["status"], "replayed")
        topic, payload, _ = producer.calls[0]
        self.assertEqual(topic, "incident.action.requested")
        self.assertNotEqual(payload["approval_id"], "appr-old")
        self.assertEqual(payload["idempotency_key"], f"inc-1:{payload['approval_id']}")
        self.assertEqual(database.approvals[payload["approval_id"]].status, "approved")


if __name__ == "__main__":
    unittest.main()

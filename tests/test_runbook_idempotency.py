import importlib
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from redis.exceptions import RedisError


server = importlib.import_module("apps.mcp-ops-server.server")


@asynccontextmanager
async def fake_session():
    yield object()


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, numkeys, key, *args):
        token = args[0]
        if self.values.get(key) != token:
            return 0
        if "redis.call('del'" in script:
            self.values.pop(key, None)
            return 1
        self.values[key] = args[1]
        return "OK"


class RunbookIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.active_db = SimpleNamespace(session=fake_session)
        self.redis = FakeRedis()

    async def execute(self, key: str) -> dict:
        return await server.execute_runbook_step(
            step="restart_service",
            params={"service_id": "payment-api"},
            approval_id=key.split(":", 1)[1],
            idempotency_key=key,
        )

    async def test_same_approval_key_executes_only_once(self):
        operation = Mock(return_value={"step": "restart_service", "status": "success"})
        with patch.object(server, "active_db", self.active_db), \
                patch.object(server, "redis_client", self.redis), \
                patch.object(server, "_validate_execution", AsyncMock(return_value=None)), \
                patch.object(server, "_execute_runbook_operation", operation):
            first = await self.execute("inc-1:appr-1")
            duplicate = await self.execute("inc-1:appr-1")

        operation.assert_called_once()
        self.assertEqual(first["status"], "success")
        self.assertEqual(duplicate["status"], "rejected")

    async def test_new_approval_id_uses_a_new_key(self):
        operation = Mock(return_value={"step": "restart_service", "status": "success"})
        with patch.object(server, "active_db", self.active_db), \
                patch.object(server, "redis_client", self.redis), \
                patch.object(server, "_validate_execution", AsyncMock(return_value=None)), \
                patch.object(server, "_execute_runbook_operation", operation):
            first = await self.execute("inc-1:appr-1")
            reapproved = await self.execute("inc-1:appr-2")

        self.assertEqual(operation.call_count, 2)
        self.assertEqual(first["status"], "success")
        self.assertEqual(reapproved["status"], "success")

    async def test_redis_failure_rejects_before_write_operation(self):
        unavailable = SimpleNamespace(
            set=AsyncMock(side_effect=RedisError("unavailable")),
        )
        operation = Mock(return_value={"step": "restart_service", "status": "success"})
        with patch.object(server, "active_db", self.active_db), \
                patch.object(server, "redis_client", unavailable), \
                patch.object(server, "_validate_execution", AsyncMock(return_value=None)), \
                patch.object(server, "_execute_runbook_operation", operation):
            result = await self.execute("inc-1:appr-1")

        operation.assert_not_called()
        self.assertEqual(result["status"], "rejected")
        self.assertIn("Redis", result["reason"])

if __name__ == "__main__":
    unittest.main()

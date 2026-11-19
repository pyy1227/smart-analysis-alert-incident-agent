import importlib.util
from pathlib import Path
import unittest

import httpx


path = Path(__file__).resolve().parents[1] / "payment_api/load-test/pool_pressure.py"
spec = importlib.util.spec_from_file_location("pool_pressure", path)
pool_pressure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool_pressure)


class PoolPressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_are_unique_and_send_valid_payment_fields(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"status": "succeeded"})

        async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
            counts = await pool_pressure.run_load(client, 3, 10, "batch123")

        self.assertEqual(counts, {"200": 3})
        self.assertEqual({r.url.params["order_id"] for r in requests}, {
            "loadtest-batch123-0", "loadtest-batch123-1", "loadtest-batch123-2",
        })
        self.assertTrue(all(r.url.path == "/api/pay" and r.url.params["amount"] == "1.00" for r in requests))

    async def test_failed_responses_are_counted(self):
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        ) as client:
            counts = await pool_pressure.run_load(client, 2, 10, "batch123")
        self.assertEqual(counts, {"503": 2})


if __name__ == "__main__":
    unittest.main()

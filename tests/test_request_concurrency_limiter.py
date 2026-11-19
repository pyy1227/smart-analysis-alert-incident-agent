import unittest

from payment_api.app.db import ResizableConcurrencyLimiter


class RequestConcurrencyLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_expansion_adds_request_permits_immediately(self):
        limiter = ResizableConcurrencyLimiter(2)
        await limiter.acquire()

        limiter.resize(4)

        self.assertEqual(limiter.current_limit, 4)
        self.assertEqual(limiter.target_limit, 4)
        self.assertEqual(limiter._value, 3)
        self.assertEqual(limiter.pending_reduction, 0)

    async def test_shrink_retires_active_permits_without_over_admitting(self):
        limiter = ResizableConcurrencyLimiter(3)
        await limiter.acquire()
        await limiter.acquire()

        limiter.resize(1)

        self.assertEqual(limiter.current_limit, 2)
        self.assertEqual(limiter.target_limit, 1)
        self.assertEqual(limiter._value, 0)
        self.assertEqual(limiter.pending_reduction, 1)

        limiter.release()
        self.assertEqual(limiter.current_limit, 1)
        self.assertEqual(limiter._value, 0)
        self.assertEqual(limiter.pending_reduction, 0)

        limiter.release()
        self.assertEqual(limiter.current_limit, 1)
        self.assertEqual(limiter._value, 1)


if __name__ == "__main__":
    unittest.main()

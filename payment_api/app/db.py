import asyncio
from contextlib import asynccontextmanager

from .config import settings
from .metrics import REQUEST_CONCURRENCY_ACTIVE, REQUEST_CONCURRENCY_WAITING


class ResizableConcurrencyLimiter(asyncio.Semaphore):
    def __init__(self, value, timeout=None):
        super().__init__(value)
        self.timeout = timeout
        self.current_limit = value
        self.target_limit = value
        self.waiting_count = 0

    async def acquire(self):
        if not self.locked():
            await super().acquire()
            return

        self.waiting_count += 1
        REQUEST_CONCURRENCY_WAITING.set(self.waiting_count)
        try:
            if self.timeout:
                await asyncio.wait_for(super().acquire(), timeout=self.timeout)
            else:
                await super().acquire()
        finally:
            self.waiting_count -= 1
            REQUEST_CONCURRENCY_WAITING.set(self.waiting_count)

    @property
    def active_count(self) -> int:
        """当前正在处理的请求数。"""
        return self.current_limit - self._value

    @property
    def pending_reduction(self) -> int:
        return self.current_limit - self.target_limit

    def resize(self, new_size: int) -> None:
        """调整请求并发上限；缩容时等待占用中的许可自然释放。"""
        if new_size < 1:
            raise ValueError("并发上限不能小于 1")

        self.target_limit = new_size
        if new_size > self.current_limit:
            for _ in range(new_size - self.current_limit):
                # 直接调用父类，为扩容增加新的许可。
                super().release()
            self.current_limit = new_size
            return

        # 先移除空闲许可，其余许可在请求结束时逐步回收。
        idle_permits_to_remove = min(self._value, self.current_limit - new_size)
        self._value -= idle_permits_to_remove
        self.current_limit -= idle_permits_to_remove

    def release(self) -> None:
        """释放许可；缩容尚未完成时直接回收该许可。"""
        if self.current_limit > self.target_limit:
            self.current_limit -= 1
            return
        super().release()


# 创建实例时，把 settings 里的配置传进去
request_concurrency = ResizableConcurrencyLimiter(
    value=settings.REQUEST_CONCURRENCY_LIMIT,
    timeout=settings.REQUEST_CONCURRENCY_TIMEOUT,
)


@asynccontextmanager
async def request_slot():
    await request_concurrency.acquire()
    REQUEST_CONCURRENCY_ACTIVE.set(request_concurrency.active_count)

    try:
        await asyncio.sleep(settings.REQUEST_PROCESSING_DELAY)
        yield
    finally:
        request_concurrency.release()
        REQUEST_CONCURRENCY_ACTIVE.set(request_concurrency.active_count)

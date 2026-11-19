"""为 payment-api 请求并发告警生成本地流量。

脚本会调用 /api/pay 并写入测试支付记录，只能用于本地测试。
"""

import argparse
import asyncio
import time
import uuid
from collections import Counter

import httpx


def positive_int(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 200:
        raise argparse.ArgumentTypeError("并发数必须在 1 到 200 之间")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="向本地 Payment API 发送并发测试请求")
    parser.add_argument("--concurrency", type=positive_int, default=20, help="并发请求数，默认 20")
    parser.add_argument("--timeout", type=positive_int, default=90, help="单个请求超时秒数，默认 90")
    parser.add_argument(
        "--confirm-test-data", action="store_true",
        help="确认正在使用测试数据库，并允许写入测试支付记录",
    )
    args = parser.parse_args()
    if not args.confirm_test_data:
        parser.error("该测试会写入支付记录；确认使用测试数据库后，请添加 --confirm-test-data")
    return args


async def run_load(client: httpx.AsyncClient, concurrency: int, timeout: int, run_id: str) -> Counter:
    async def send_request(number: int) -> str:
        order_id = f"loadtest-{run_id}-{number}"
        try:
            response = await client.post(
                "/api/pay", params={"order_id": order_id, "amount": "1.00"}, timeout=timeout,
            )
            status = str(response.status_code)
            if response.is_error:
                print(f"{order_id}: HTTP {status} {response.text[:200]}")
            else:
                print(f"{order_id}: HTTP {status}")
            return status
        except httpx.HTTPError as error:
            print(f"{order_id}: 请求失败 {type(error).__name__}: {error}")
            return "request_error"

    return Counter(await asyncio.gather(*(send_request(i) for i in range(concurrency))))


async def main() -> int:
    args = parse_args()
    run_id = uuid.uuid4().hex[:12]
    # 只允许访问本机，避免误压其他环境。
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001", trust_env=False) as client:
        try:
            response = await client.get("/concurrency", timeout=5)
            response.raise_for_status()
            limiter = response.json()
        except (httpx.HTTPError, ValueError) as error:
            print(f"无法查询 Payment API 请求并发限制器，请先确认服务已启动: {error}")
            return 1

        limit = limiter.get("current_limit")
        print(f"测试批次: loadtest-{run_id}；并发: {args.concurrency}；当前请求并发上限: {limit}")
        print("注意：本测试会向 Payment MySQL 写入金额为 1.00 的测试记录。")
        if isinstance(limit, int) and args.concurrency <= limit + 5:
            print("提示：并发数未超过请求上限 + 5，可能达不到告警的等待阈值。")
        print("开始发送请求；请同时观察 /concurrency、Prometheus 和前端事件。")
        started = time.monotonic()
        counts = await run_load(client, args.concurrency, args.timeout, run_id)
        print(f"完成：耗时 {time.monotonic() - started:.1f} 秒；结果 {dict(counts)}")
        return 0 if counts.get("200", 0) == args.concurrency else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

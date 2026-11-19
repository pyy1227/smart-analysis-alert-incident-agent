import asyncio
import random
import uuid

from apps.common.kafka_client import create_producer


async def main():
    producer = await create_producer()
    try:
        for i in range(10):
            event = {
                "event_id": uuid.uuid4().hex,
                "incident_id": f"inc-{uuid.uuid4().hex[:8]}",
                "tenant_id": "demo",
                "service_id": "payment-api",
                "environment": "prod",
                "severity": random.choice(["warning", "critical"]),
                "title": random.choice(
                    ["支付接口超时", "数据库连接池耗尽", "payment-api 5xx 升高"]
                ),
                "description": "监控发现错误率升高，请分析原因。",
                "user_id": "system",
                "trace_id": uuid.uuid4().hex,
            }
            await producer.send_and_wait("alert.triggered", event)
            print(f"[{i + 1}/10] sent {event['event_id']}")
            await asyncio.sleep(0.5)
    finally:
        await producer.stop()


if __name__ == "__main__":
    asyncio.run(main())

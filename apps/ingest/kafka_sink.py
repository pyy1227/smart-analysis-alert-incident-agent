"""把告警写入 Kafka。"""
from apps.common.kafka_client import create_producer

producer = None


async def get_producer():
    global producer
    if producer is None:
        producer = await create_producer()
    return producer


async def publish_alert(alert: dict):
    producer = await get_producer()
    await producer.send_and_wait(
        "alert.triggered",
        alert,
        key=alert["service_id"].encode(),
    )

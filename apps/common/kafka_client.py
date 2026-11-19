import json

"""Kafka 客户端。"""
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from apps.common.config import settings


async def create_producer():
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap,#Kafka集群地址
        value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode(),
        enable_idempotence=True,
    )
    await producer.start()
    return producer


def create_consumer(topics: list[str], group_id: str):
    return AIOKafkaConsumer(
        *topics,
        bootstrap_servers=settings.kafka_bootstrap,
        group_id=group_id,
        value_deserializer=lambda v: json.loads(v.decode()),
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_interval_ms=settings.kafka_max_poll_interval_ms,
        session_timeout_ms=settings.kafka_session_timeout_ms,
    )

"""Payment API 的 MySQL 持久化操作。"""

import time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .models import Base, Payment, PaymentConcurrencyConfig
from .metrics import (
    DB_OPERATION_SECONDS,
    DB_POOL_AVAILABLE,
    DB_POOL_CAPACITY,
    DB_POOL_IN_USE,
    DB_POOL_OVERFLOW,
)


engine = create_async_engine(settings.payment_database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def initialize_storage() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def close_storage() -> None:
    await engine.dispose()


async def record_payment(order_id: str, amount: Decimal) -> tuple[Payment, bool]:
    """同一订单只写入一次，并返回本次请求是否创建了记录。"""
    started = time.perf_counter()
    outcome = "success"
    try:
        async with SessionLocal() as session:
            existing = await session.scalar(select(Payment).where(Payment.order_id == order_id))
            if existing:
                return existing, False

            payment = Payment(order_id=order_id, amount=amount, status="succeeded")
            session.add(payment)
            try:
                await session.commit()
            except IntegrityError:
                # 并发重试可能已经先写入了相同订单。
                await session.rollback()
                existing = await session.scalar(select(Payment).where(Payment.order_id == order_id))
                if existing:
                    return existing, False
                raise
            await session.refresh(payment)
            return payment, True
    except Exception:
        outcome = "error"
        raise
    finally:
        DB_OPERATION_SECONDS.labels(operation="record_payment", outcome=outcome).observe(
            time.perf_counter() - started
        )


def refresh_database_pool_metrics() -> None:
    """读取 SQLAlchemy 连接池状态，不额外执行数据库查询。"""
    pool = engine.sync_engine.pool
    base_size = int(pool.size())
    max_overflow = max(0, int(getattr(pool, "_max_overflow", 0)))
    DB_POOL_IN_USE.set(pool.checkedout())
    DB_POOL_AVAILABLE.set(pool.checkedin())
    DB_POOL_OVERFLOW.set(max(0, pool.overflow()))
    DB_POOL_CAPACITY.set(base_size + max_overflow)


async def load_concurrency_target() -> int | None:
    async with SessionLocal() as session:
        config = await session.get(PaymentConcurrencyConfig, "payment-api")
        return config.target_limit if config else None


async def save_concurrency_target(limit: int, updated_by: str = "mcp") -> None:
    async with SessionLocal() as session:
        config = await session.get(PaymentConcurrencyConfig, "payment-api")
        if config:
            config.target_limit = limit
            config.updated_by = updated_by
        else:
            session.add(PaymentConcurrencyConfig(
                service_id="payment-api",
                target_limit=limit,
                updated_by=updated_by,
            ))
        await session.commit()

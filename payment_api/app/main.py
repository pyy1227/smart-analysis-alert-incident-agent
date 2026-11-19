from contextlib import asynccontextmanager
from decimal import Decimal
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from payment_api.routers import db

from .db import request_concurrency, request_slot
from .metrics import HTTP_REQUEST_SECONDS, PAYMENT_FAILED
from .storage import (
    close_storage,
    initialize_storage,
    load_concurrency_target,
    record_payment,
    refresh_database_pool_metrics,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await initialize_storage()
    saved_limit = await load_concurrency_target()
    if saved_limit is not None:
        request_concurrency.resize(saved_limit)
    yield
    await close_storage()


app = FastAPI(title='Payment API', lifespan=lifespan)
app.include_router(db.router)


@app.middleware("http")
async def observe_http_request(request: Request, call_next):
    started = time.perf_counter()
    status = "500"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        return response
    finally:
        route = request.scope.get("route")
        route_name = getattr(route, "path", request.url.path)
        HTTP_REQUEST_SECONDS.labels(
            method=request.method, route=route_name, status=status,
        ).observe(time.perf_counter() - started)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    """返回 Prometheus 可读取的应用指标。"""
    refresh_database_pool_metrics()
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post('/api/pay')
async def pay(order_id: str, amount: Decimal = Query(gt=0)):
    try:
        # 这里只限制请求并发，数据库连接由 SQLAlchemy 连接池管理。
        async with request_slot():
            payment, created = await record_payment(order_id, amount)
            return {
                "status": payment.status,
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "amount": str(payment.amount),
                "idempotent": not created,
                "msg": "支付成功" if created else "重复请求，已返回已有支付记录",
            }

    except Exception as error:
        PAYMENT_FAILED.labels(reason="db_error").inc()
        raise HTTPException(status_code=500, detail=str(error)) from error


@app.get("/status")
def status():
    return {
        "request_concurrency_limit": request_concurrency.current_limit,
        "target_concurrency_limit": request_concurrency.target_limit,
        "pending_reduction": request_concurrency.pending_reduction,
        "active_requests": request_concurrency.active_count,
        "waiting_requests": request_concurrency.waiting_count,
        "semantics": "application_request_concurrency_limiter",
    }

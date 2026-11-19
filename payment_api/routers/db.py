from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from payment_api.app.config import settings
from payment_api.app.db import request_concurrency
from payment_api.app.storage import save_concurrency_target


router = APIRouter(prefix="/concurrency", tags=["request-concurrency"])


class ConcurrencyConfig(BaseModel):
    concurrency_limit: int = Field(ge=1, le=200)


@router.get("")
async def get_request_concurrency():
    return {
        "current_limit": request_concurrency.current_limit,
        "target_limit": request_concurrency.target_limit,
        "pending_reduction": request_concurrency.pending_reduction,
        "active_requests": request_concurrency.active_count,
        "waiting_requests": request_concurrency.waiting_count,
        "wait_timeout": request_concurrency.timeout,
        "semantics": "application_request_concurrency_limiter",
    }

@router.post("/expand")
async def expand_request_concurrency(body: ConcurrencyConfig):
    """审批通过后提高应用请求并发上限。"""
    current_limit = request_concurrency.current_limit
    waiting_requests = request_concurrency.waiting_count

    if body.concurrency_limit <= current_limit:
        raise HTTPException(
            status_code=400,
            detail=f"concurrency_limit 必须大于当前上限 {current_limit}",
        )

    if not request_concurrency.locked():
        raise HTTPException(
            status_code=409,
            detail="当前请求并发未饱和，无需提高上限",
        )

    if waiting_requests <= settings.REQUEST_CONCURRENCY_WAIT_THRESHOLD:
        raise HTTPException(
            status_code=409,
            detail=(
                "等待请求数必须大于 "
                f"{settings.REQUEST_CONCURRENCY_WAIT_THRESHOLD}，当前为 {waiting_requests}"
            ),
        )

    await save_concurrency_target(body.concurrency_limit)
    request_concurrency.resize(body.concurrency_limit)

    return {
        "status": "success",
        "previous_limit": current_limit,
        "new_limit": request_concurrency.current_limit,
        "waiting_requests": waiting_requests,
        "waiting_threshold": settings.REQUEST_CONCURRENCY_WAIT_THRESHOLD,
        "message": f"Payment API request concurrency limit increased to {body.concurrency_limit}",
    }


@router.post("/shrink")
async def shrink_request_concurrency(body: ConcurrencyConfig):
    """随着活跃请求结束，平滑降低请求并发上限。"""
    current_limit = request_concurrency.current_limit

    if body.concurrency_limit >= current_limit:
        raise HTTPException(
            status_code=400,
            detail=f"concurrency_limit 必须小于当前上限 {current_limit}",
        )

    await save_concurrency_target(body.concurrency_limit)
    request_concurrency.resize(body.concurrency_limit)
    return {
        "status": "scheduled" if request_concurrency.pending_reduction else "success",
        "previous_limit": current_limit,
        "current_limit": request_concurrency.current_limit,
        "target_limit": request_concurrency.target_limit,
        "pending_reduction": request_concurrency.pending_reduction,
        "message": (
            "等待活跃请求结束后降低并发上限"
            if request_concurrency.pending_reduction
            else "请求并发上限已降低"
        ),
    }


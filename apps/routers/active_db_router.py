"""读取 payment-api 的请求并发状态。"""

from fastapi import APIRouter, HTTPException

from apps.common.config import settings
import httpx

router = APIRouter(prefix="/api", tags=["request-concurrency"])


@router.get("/concurrency")
def get_concurrency() -> dict:
    """向前端返回请求并发状态。"""
    return _get_payment_api_concurrency()


def _get_payment_api_concurrency() -> dict:
    try:
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            response = client.get(f"{settings.payment_api_url.rstrip('/')}/concurrency")
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=f"payment-api 请求并发状态查询失败：{error}",
        ) from error

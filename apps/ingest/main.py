"""告警接收接口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from apps.ingest.dedupe import is_duplicate, rate_limit
from apps.ingest.kafka_sink import publish_alert
from apps.ingest.lifecycle import enrich_alert_lifecycle
from apps.ingest.parsers import PARSERS


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Incident Ingest", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/webhook/{source}")
async def webhook(
    source: str,
    request: Request,
):
    payload = await request.json()

    parser = PARSERS.get(source)
    if not parser:
        return JSONResponse(
            {"ok": False, "error": f"未知来源：{source}"},
            status_code=400,
        )

    alerts = parser(payload)
    accepted = []

    for alert in alerts:
        if not await rate_limit(alert.service_id):
            continue
        lifecycle = await enrich_alert_lifecycle(alert)
        # 告警状态发生变化（firing -> resolved）时必须放行，否则恢复通知会被去重吃掉，
        # 事件会一直停留在触发状态。因此只有"状态没变"的重复告警才进入去重判断。
        if not lifecycle["status_changed"] and await is_duplicate(
                alert.fingerprint, alert.service_id, alert.environment, alert.status.value,
        ):
            continue
        await publish_alert(alert.model_dump(mode="json"))
        accepted.append(alert.event_id)

    return {"ok": True, "accepted": accepted, "count": len(accepted)}

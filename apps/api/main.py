from contextlib import asynccontextmanager
import logging

from apps.common.db import  active_db, Incident

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import desc, select
from apps.routers import active_db_router, dead_letters, incidents
from apps.common.kafka_client import create_producer

logger = logging.getLogger(__name__)

#初始化数据库
"""
lifespan 是 FastAPI 的生命周期钩子：应用启动时执行 yield 之前的代码，关闭时执行 yield 之后的代码。

这里在启动时调用 await init_db()，确保数据库和表已就绪（和 worker 里一样，create_all 不会重复建表）
"""
@asynccontextmanager
async def lifespan(app: FastAPI):
    await active_db.init_db()
    try:
        producer = await create_producer()
    except Exception:
        # The interactive API can still create, analyse and review incidents while
        # Kafka is temporarily unavailable. Approval execution will remain queued.
        logger.exception("Kafka producer 连接失败，无法发送审批事件")
        producer = None
    app.state.producer = producer
    try:
        yield
    finally:
        if producer:
            await producer.stop()

app = FastAPI(title="Incident Copilot API", lifespan=lifespan)

app.include_router(incidents.router)
app.include_router(active_db_router.router)
app.include_router(dead_letters.router)


@app.get("/health")
async def health():
    return {"status": "healthy"}



#解决跨域问题
# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite默认端口
        "http://localhost:3000",  # React默认端口
        "http://localhost:8080",  # 其他可能的前端端口
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8000",  # 如果需要
    ],
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有HTTP方法
    allow_headers=["*"],  # 允许所有请求头
)

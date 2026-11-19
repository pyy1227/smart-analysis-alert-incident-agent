from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Payment API 配置
class Settings(BaseSettings):
    # 本地配置从项目根目录的 .env 读取，敏感信息不写入代码。
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        extra="ignore",
    )

    # 支付数据库与事件数据库分离，保存支付记录和请求并发目标值。
    payment_database_url: str = (
        "mysql+aiomysql://payment:payment@127.0.0.1:3307/payment"
    )
    ENVIRONMENT: str = "staging"
    SERVICE_NAME: str = "payment-api"

    # 这些配置控制应用请求信号量，不是 SQLAlchemy/MySQL 连接池。
    REQUEST_CONCURRENCY_LIMIT: int = Field(default=5, ge=1, le=200)
    REQUEST_CONCURRENCY_TIMEOUT: float = Field(default=60, gt=0, le=300)
    REQUEST_PROCESSING_DELAY: float = Field(default=10.0, ge=0, le=300)
    REQUEST_CONCURRENCY_WAIT_THRESHOLD: int = Field(default=5, ge=0, le=200)

    CHAOS_ENABLED: bool = False
    CHAOS_TOKEN: str = "change-me"


settings = Settings()

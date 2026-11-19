from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]

#从.env文件中读取配置项，如果没有就会使用默认值
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:123456@localhost:5432/incident"
    graph_url: str = "postgresql://postgres:123456@localhost:5432/incident"
    redis_url: str = "redis://localhost:6379/0"
    redis_host: str = "localhost"
    redis_port: int = 6379
    kafka_bootstrap: str = "localhost:9092"
    kafka_max_poll_interval_ms: int = 900000
    kafka_session_timeout_ms: int = 30000
    worker_metrics_port: int = 9200
    mcp_server_url: str = "http://127.0.0.1:8931/mcp"
    # Keep local MCP traffic away from Windows/system HTTP proxies by default.
    # Set this to true only when the MCP endpoint intentionally needs a proxy.
    mcp_trust_env: bool = False
    chroma_path: str = str(PROJECT_ROOT / "data" / "chroma")
    # Root payment_api is the local monitored workload (not services/payment_api).
    payment_api_url: str = "http://127.0.0.1:8001"

    # 本地可观测性配置。
    managed_services: str = "payment-api,order-api,user-api,incident-api,prometheus,alertmanager"
    prometheus_url: str | None = None
    prometheus_service_label: str = "service"



settings = Settings()

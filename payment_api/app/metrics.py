"""
定义的指标，用来检测支付系统的并发限制器、请求状态、数据库连接池状态等。
"""

from prometheus_client import Counter, Gauge, Histogram

# 应用层并发限制器，不代表 SQLAlchemy/MySQL 连接池。
REQUEST_CONCURRENCY_ACTIVE = Gauge(
    'payment_request_concurrency_active', 'Payment requests holding a concurrency permit')
REQUEST_CONCURRENCY_WAITING = Gauge(
    'payment_request_concurrency_waiting', 'Payment requests waiting for a concurrency permit')
# 支付失败计数
PAYMENT_FAILED = Counter('payment_failed_total', 'Total failed payments', ['reason'])

# 请求量、错误率和耗时指标。
HTTP_REQUEST_SECONDS = Histogram(
    'http_server_requests_seconds',
    'HTTP server request duration in seconds',
    ['method', 'route', 'status'],
)

# 这些指标描述真实的 SQLAlchemy/MySQL 连接池，不是应用并发限制器。
DB_POOL_IN_USE = Gauge(
    'payment_db_pool_in_use', 'SQLAlchemy connections currently checked out')
DB_POOL_CAPACITY = Gauge(
    'payment_db_pool_capacity', 'Configured SQLAlchemy pool capacity including max overflow')
DB_POOL_AVAILABLE = Gauge(
    'payment_db_pool_available', 'Idle SQLAlchemy connections currently in the pool')
DB_POOL_OVERFLOW = Gauge(
    'payment_db_pool_overflow', 'SQLAlchemy overflow connections currently allocated')
DB_OPERATION_SECONDS = Histogram(
    'payment_db_operation_seconds',
    'Payment database operation duration in seconds',
    ['operation', 'outcome'],
)

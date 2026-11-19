# 数据库连接池耗尽 Runbook

## 2.1 现象
应用报连接超时，数据库 active 连接接近 max_connections。

## 2.2 处置
1. 先确认真实 SQLAlchemy/MySQL 连接池容量、等待数和数据库 `max_connections`。
2. 排查慢查询与长事务。
3. 当前自动化只支持调整 payment-api 的请求并发上限，不能把它当作数据库连接池扩容。

## 2.3 可执行动作
- action: submit_ticket
  params: { "service_id": "{{service_id}}" }
  risk: medium
  requires_approval: true

只有 Docker 证据显示容器已停止或 unhealthy 时，才可另行提出 `restart_service`。

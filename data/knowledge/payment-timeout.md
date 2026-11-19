# payment-api 支付超时 Runbook

## 3.1 现象
payment-api P99 延迟升高，支付回调超时，错误率升高。

## 3.2 排查步骤
1. 查看 payment-api P99 与错误率。
2. 查看应用请求并发限制器的 active / waiting 指标，并单独查看数据库监控指标。
3. 检查下游 bank-gateway 是否超时。
4. 检查最近一次发布记录。

## 3.3 处置
1. 若 `payment_request_concurrency_waiting` 持续大于零，可提出调整请求并发上限；该动作不会修改数据库连接池。
2. 若怀疑真实数据库连接池耗尽，提交工单核实 SQLAlchemy/MySQL 配置和数据库承载能力。
3. 高风险操作必须人工审批。

## 3.4 可执行动作
- action: scale_request_concurrency
  tool: execute_runbook_step
  step: scale_request_concurrency
  params: { "service_id": "{{service_id}}", "concurrency_limit": 50 }
  risk: high
  requires_approval: true

- action: submit_ticket
  params: { "service_id": "{{service_id}}" }
  risk: medium
  requires_approval: true

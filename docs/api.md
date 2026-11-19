# API 参考

本文件说明 Sentinel 对外暴露的 HTTP 接口与 MCP 工具，供阅读代码或不方便启动服务时查阅。
服务启动后，Swagger 文档是更权威的接口来源：`incident-api` → <http://localhost:8000/docs>，`payment-api` → <http://localhost:8001/docs>。

| 服务 | 默认地址 | 职责 |
| --- | --- | --- |
| ingest | <http://localhost:8100> | 接收 Alertmanager Webhook，限流、去重、投递 Kafka |
| incident-api | <http://localhost:8000> | 事件查询、对话、只读工具、审批、死信、并发状态 |
| payment-api | <http://localhost:8001> | 被监控的独立支付服务，提供并发上限的读写接口 |
| mcp-ops-server | `127.0.0.1:8931`（MCP over HTTP） | 只读诊断与受控写操作，默认只在 Compose 网络内监听 |

---

## 1. ingest（告警接入）

### `GET /health`

返回 `{"status": "ok"}`。

### `GET /metrics`

Prometheus 文本格式指标。

### `POST /webhook/{source}`

接收告警。`source` 目前只注册了 `prometheus`（见 `apps/ingest/parsers.py` 的 `PARSERS`），未知来源返回 400。

请求体为 Alertmanager Webhook 结构，逐条处理 `alerts[]`，处理顺序为：

1. **限流**：按服务维度的 Redis 计数器，超限的告警直接丢弃。
2. **告警生命周期**：`enrich_alert_lifecycle` 判断 firing → resolved 的状态变化。
3. **去重**：状态没有变化、且 fingerprint 在窗口内重复的告警丢弃；状态变化（例如 resolved）不受去重限制。
4. **投递 Kafka**：写入 `alert.triggered`，由 Worker 落库。

```json
{
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "PaymentHighLatency",
        "service": "payment-api",
        "severity": "critical",
        "environment": "staging"
      },
      "annotations": { "description": "payment-api P99 超过 2 秒" },
      "startsAt": "2026-11-18T08:00:00+08:00"
    }
  ]
}
```

返回被接受的 `event_id`；被限流或去重的告警不会出现在 `accepted` 中：

```json
{ "ok": true, "accepted": ["3f2a..."], "count": 1 }
```

> `fingerprint` 由 `prom:服务:alertname` 组成，因此同一服务同一告警名在窗口内只会创建一个事件。

---

## 2. incident-api（事件与审批）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/incidents?limit=50` | 事件列表，`limit` 1–100 |
| GET | `/api/incidents/{incident_id}` | 事件详情，含 `approvals` / `audits` / `messages` |
| POST | `/api/incidents` | 手动创建事件并提交分析 |
| POST | `/api/incidents/{incident_id}/reanalyze` | 重新排队分析 |
| POST | `/api/incidents/{incident_id}/messages` | 对话（一次性返回） |
| POST | `/api/incidents/{incident_id}/messages/stream` | 对话（SSE 流式，前端使用） |
| POST | `/api/incidents/{incident_id}/tools/{tool_name}` | 调用只读工具，白名单见下 |
| POST | `/api/incidents/{incident_id}/concurrency-limit` | 针对 payment-api 生成"调整并发上限"建议 |
| POST | `/api/incidents/{incident_id}/approval` | 确认或取消待执行操作 |
| POST | `/api/incidents/{incident_id}/retry-action` | 执行失败后重新发起 |
| GET | `/api/concurrency` | 透传 payment-api 的并发状态 |
| GET | `/api/dead-letters?limit=100&active_only=false` | 死信列表 |
| POST | `/api/dead-letters/{id}/ignore` | 忽略死信（仅 pending / publish_failed 可操作） |
| POST | `/api/dead-letters/{id}/replay` | 重放死信 |

**创建事件**

```json
{
  "title": "支付接口 5xx 升高",
  "description": "P99 超过 2 秒",
  "service_id": "payment-api",
  "severity": "critical",
  "environment": "staging",
  "tenant_id": "demo"
}
```

**只读工具白名单**（`tool_name` 只能取以下值，其余返回 404）

`query_service_status`、`query_service_metrics`、`query_database_health`、`query_logs`、`search_incident_history`

请求体统一为 `{"query": "可选的检索词"}`；服务号取自事件本身，调用方无法指定任意服务。

**确认操作**

```powershell
$id = '<incident_id>'

# 查看待确认的操作建议
(Invoke-RestMethod "http://localhost:8000/api/incidents/$id").approvals

# 确认执行；approved=false 表示拒绝，reason 会写入审计
Invoke-RestMethod -Method Post "http://localhost:8000/api/incidents/$id/approval" `
  -ContentType 'application/json' `
  -Body '{"approved": true, "reason": "确认重启 payment-api"}'
```

确认成功后事件状态变为 `action_queued`，写入 `incident.action.requested`，由动作消费者调用 MCP 执行。
只有 `restart_service` 与 `scale_request_concurrency` 两类步骤可以被确认；其它建议只能拒绝。

---

## 3. payment-api（被监控负载）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/status` | 当前并发上限、活跃请求、等待请求 |
| GET | `/metrics` | Prometheus 指标（含连接池与并发信号量） |
| POST | `/api/pay?order_id=...&amount=...` | 幂等下单，`amount > 0` |
| GET | `/concurrency` | 并发状态下限/目标/待降量 |
| POST | `/concurrency/expand` | 提高并发上限，`{"concurrency_limit": 10}` |
| POST | `/concurrency/shrink` | 降低并发上限，缩容时等待在途请求自然结束 |

`expand` 只用于演示"提高并发"的受控写操作：并发未饱和或等待请求数不足阈值时返回 400/409，不会无条件放行。

---

## 4. MCP 工具（mcp-ops-server）

| 工具 | 类型 | 说明 |
| --- | --- | --- |
| `query_service_status` | 只读 | 通过 Docker Compose 查询容器状态 |
| `query_service_metrics` | 只读 | 查询 Prometheus 指标 |
| `query_database_health` | 只读 | 数据库连接池健康度 |
| `query_logs` | 只读 | 受管容器日志，支持检索词 |
| `search_incident_history` | 只读 | 同租户/同服务/同环境/同分类的历史事件 |
| `execute_runbook_step` | 写 | 执行已确认的 Runbook 步骤 |

写操作有三道约束，任何一道不满足都会返回 `status: rejected`：

1. `step` 必须在 `{restart_service, scale_request_concurrency}` 内。
2. `service_id` 必须在 `MANAGED_SERVICES` 白名单内（默认 payment-api、incident-api、prometheus、alertmanager）。
3. 必须有对应的 `approval_id` 与 `idempotency_key`；幂等键形如 `{incident_id}:{approval_id}`，通过 Redis `SET NX` 原子占用，处理中 TTL 300 秒。重复投递会被拒绝，不会重复执行同一审批动作。

---

## 5. 事件状态

`Incident.status` 的实际取值（以 `apps/worker/main.py`、`apps/routers/incidents.py` 为准）：

| 状态 | 含义 |
| --- | --- |
| `received` | 事件已落库，等待分析 |
| `queued_for_analysis` | 分析消息已投递 Kafka |
| `analysing` | Worker 正在运行 LangGraph 工作流 |
| `analyzed` | 分析完成，尚未产生待确认操作 |
| `awaiting_approval` | 已生成操作建议，等待所有者确认 |
| `action_queued` | 已确认，动作消息进入 Kafka |
| `executed` | 动作执行成功但验证未确认恢复 |
| `resolved` | 动作执行成功且验证确认恢复 |
| `analysis_failed` / `analysis_queue_failed` | 分析失败 / 投递失败 |
| `execution_failed` | 动作执行或验证失败，可重试 |

> `docs/architecture.md` 中的状态机是设计意图的简化示意，具体字符串以本节为准。

---

## 6. 幂等与审计约定

- 同一个 Kafka 消息可能被重复投递（at-least-once），因此落库以 `event_id` 唯一约束兜底，写操作用幂等键兜底。
- 每次确认、取消、工具调用、执行结果都会写 `audit_logs`，带 `trace_id`，`user_id` 固定为本地所有者 `owner`。
- 失败重试不复用旧审批：重试会创建新的待确认记录，因此使用新的幂等键。
- 相关实现：[apps/worker/outcomes.py](../apps/worker/outcomes.py)（执行结果判定）、[apps/mcp-ops-server/server.py](../apps/mcp-ops-server/server.py)（幂等与白名单校验）、[apps/worker/action_proposals.py](../apps/worker/action_proposals.py)（模型输出 Schema 与证据校验）。

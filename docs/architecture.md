# 系统架构

## 产品边界

Sentinel 是单所有者的事件响应控制面，本身不是监控系统。本地 Prometheus、Alertmanager 和 Docker 日志是证据来源。Sentinel 关联这些信号，向本地所有者请求确认，再通过受控适配器下发已确认的处置动作。

## 运行拓扑

```text
Prometheus / Alertmanager
              │ Webhook / 事件
              ▼
       接入 API（ingest） ──► Kafka ──► 事件工作流 Worker
              │                          │
              │                          ├── PostgreSQL：事件、审批、审计
              │                          ├── Redis：去重 / 幂等
              │                          └── RAG + 只读 MCP 诊断
              ▼
        前端控制台 ◄──── API ◄──── 动作状态 / 证据
                                       │ 已确认的动作
                                       ▼
                              动作 Worker ──► MCP 操作适配器
                                                    ├── Docker Compose
                                                    └── Payment API 控制接口
```

## 信任边界

1. **浏览器**永远拿不到 MCP 凭据，也不能自行选择任意工具或命令。
2. **API** 接收本地所有者的意图，随后持久化"待执行操作"和"确认决定"。
3. **动作 Worker** 拿到的是已存储的操作、审批 ID 和幂等键；调用 MCP 之前会再校验一次白名单。
4. **MCP 适配器**持有集成凭据，并再次校验服务、动作和版本约束。
5. 本地所有者拥有全部应用权限；服务白名单、参数校验和幂等键保留下来，作为防止误执行的护栏。

## 事件状态机

```text
received → analysing → awaiting_approval → approved → queued → executing → resolved
                             │               │                       │
                             └── rejected ───┘                       └── failed
```

状态转换必须是事务性的、可审计的。重复的确认请求和重复的 Kafka 投递必须落到同一个动作执行实例，绝不能重复执行处置。

上图是示意性的简化版本：`incidents.status` 里真正落库的状态字符串（例如 `queued_for_analysis`、`analyzed`、`action_queued`）见 [docs/api.md 第 5 节](api.md#5-事件状态)。

`approval` 作为内部状态和记录名保留，但它代表的是唯一本地所有者的一次确认；系统里没有角色、管理员或多级审批规则。

## 集成契约

- **告警接入**：兼容 Alertmanager 的 Webhook，规范化为 `event_id`、服务、环境、严重级别、labels、annotations 和 trace ID。
- **服务范围**：通过配置的白名单限定哪些本地服务可以被查询或重启。
- **只读适配器**：容器状态、本地 Docker 日志、Prometheus 指标和事件历史；调用失败会体现为证据缺口。
- **写适配器**：Payment API 请求并发控制，以及白名单内服务重启。两者都需要所有者确认和幂等键。

## 运维 SLO

| SLI（指标） | 初始目标 |
| --- | --- |
| 告警被接受到事件可见 | 99% 在 30 秒内 |
| 已确认动作的下发 | 99% 在 60 秒内 |
| 重复处置 | 零容忍 |
| 写操作的审计覆盖率 | 100% |
| MCP 只读工具的错误可见性 | 100% 暴露到前端 / 指标 |

## 部署标准

API、工作流 Worker、动作 Worker 和 MCP 适配器分别作为独立工作负载运行。使用托管的 PostgreSQL、Redis 和 Kafka；数据库迁移在 CI/CD 中执行；凭据通过密钥管理服务注入；导出 OpenTelemetry 链路和 Prometheus 指标。`docker-compose.yml` 那套配置仅用于本地开发。

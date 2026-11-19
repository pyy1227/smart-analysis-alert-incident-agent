# Sentinel 项目文件说明

本文档说明 `incident-agent` 当前项目中各文件的用途、所属模块，以及它是否处于当前 Docker Compose 主链路中。

## 1. 阅读约定

- **当前使用**：根目录 `docker-compose.yml` 启动或当前代码直接引用。
- **辅助工具**：用于测试、初始化、压测或本地调试，不是常驻服务。
- **自动生成**：依赖、缓存、构建产物或运行数据，不应手工修改。
- `.env`、`.env.compose` 只说明用途，不记录任何实际密码或 API Key。

不逐项展开 `.venv/`、`apps/web/node_modules/`、`apps/web/dist/`、`.idea/`、`.git/` 和 Python 缓存，因为这些分别属于依赖、构建产物、IDE配置和版本控制内部数据，不是项目业务代码。

## 2. 项目主链路

```text
Payment API 指标
    → Prometheus
    → Alertmanager
    → Ingest Webhook
    → Kafka: alert.triggered
    → Worker / LangGraph
    → MCP、RAG、Tavily、本地监控证据
    → PostgreSQL 事件、审批、消息、审计
    → Incident API
    → React 前端

本人确认写操作
    → Kafka: incident.action.requested
    → Worker
    → MCP 执行层
    → Payment API / Docker
    → 执行结果与审计记录
```

## 3. 根目录文件

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `README.md` | 当前使用 | 项目总说明，介绍产品定位、核心能力、Compose启动方式、安全边界和数据迁移注意事项。 |
| `docker-compose.yml` | 当前使用 | 统一编排 PostgreSQL、MySQL、Redis、Kafka、Payment API、Incident API、Worker、MCP、Ingest、Web、Prometheus 和 Alertmanager，并声明数据卷、健康检查和服务依赖。 |
| `pyproject.toml` | 当前使用 | Python项目元数据和主依赖声明；Docker镜像通过它和 `uv.lock` 安装依赖。 |
| `uv.lock` | 当前使用/自动生成 | `uv` 的精确依赖锁文件，保证本地、测试和容器使用一致的Python依赖版本。 |
| `.env` | 私密运行配置 | 宿主机运行时配置，例如数据库、Kafka、模型和第三方服务地址；可能包含密钥，不应提交或写入本文档。 |
| `.env.compose` | 私密运行配置 | Docker Compose变量文件，保存本地密码、并发参数和模型API Key；不应提交。 |
| `.env.example` | 配置模板 | 宿主机运行配置示例，告诉开发者需要哪些变量以及默认语义。 |
| `.env.compose.example` | 配置模板/需安全检查 | Compose配置模板。该文件只能保存占位值；如果填过真实 DeepSeek 或 Tavily Key，应立即替换为占位符并轮换已经暴露的密钥。 |
| `.gitignore` | 当前使用 | 排除虚拟环境、缓存、运行配置、构建结果和测试产物，避免提交无关或敏感文件。 |
| `.dockerignore` | 当前使用 | 缩小Docker构建上下文，排除Git、虚拟环境、前端依赖、Chroma运行数据和私密环境文件。 |

## 4. `apps/agent`：事件对话 Agent

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/agent/__init__.py` | 当前使用 | 将目录标记为Python包。 |
| `apps/agent/copilot.py` | 当前使用 | 事件详情中的问答层；根据问题关键词选择状态、指标、数据库、日志或历史工具，构造有证据的提示词，支持DeepSeek流式输出，并在模型不可用时返回确定性降级回答。 |

## 5. `apps/api`：Incident API 应用

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/api/__init__.py` | 当前使用 | 将API目录标记为Python包。 |
| `apps/api/main.py` | 当前使用 | FastAPI主入口；初始化事件数据库和Kafka生产者，注册事件与并发路由，提供 `/health`，并配置本地CORS。 |
| `apps/api/serialize.py` | 当前使用 | 将SQLAlchemy的 `Incident` ORM对象转换为前端可消费的JSON字典，包括报告、引用、风险、审批和执行结果。 |

## 6. `apps/routers`：HTTP接口

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/routers/__init__.py` | 当前使用 | 将路由目录标记为Python包。 |
| `apps/routers/incidents.py` | 当前使用 | 事件核心接口：列表、详情、创建、重新研判、Agent普通/流式对话、MCP只读工具、并发上限申请、批准/拒绝以及审批后Kafka执行。SSE流结束后保存完整消息和审计记录。 |
| `apps/routers/active_db_router.py` | 当前使用 | 把Payment API的 `/concurrency` 状态只读代理到前端；这里展示的是应用请求并发限制器，不是数据库连接池。 |

## 7. `apps/common`：共享基础设施

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/common/__init__.py` | 当前使用 | 将共享模块目录标记为Python包。 |
| `apps/common/config.py` | 当前使用 | 使用Pydantic Settings读取 `.env` 和环境变量，集中定义PostgreSQL、Redis、Kafka、MCP、Chroma、Payment API、Prometheus和受管服务配置。 |
| `apps/common/db.py` | 当前使用 | 创建异步SQLAlchemy数据库连接；定义 `incidents`、`approvals`、`audit_logs`、`agent_messages` 四张表及会话依赖。 |
| `apps/common/schema_updates.py` | 当前使用 | 对已有 `incidents` 表做幂等增量升级，补充证据评估、动作风险和分类字段，不重写历史记录。 |
| `apps/common/kafka_client.py` | 当前使用 | 封装AIOKafka生产者和消费者；生产者开启幂等，消费者关闭自动提交并使用适合长时间Agent任务的超时配置。 |
| `apps/common/redis_client.py` | 当前使用 | 创建异步Redis客户端，供告警去重、限流和工作流事件去重使用。 |
| `apps/common/llm.py` | 当前使用 | 加载环境变量并创建DeepSeek聊天模型实例；Graph研判和事件对话共同使用。 |
| `apps/common/rag.py` | 当前使用 | Chroma RAG封装：切分文档、延迟初始化DashScope Embedding、写入知识文档、按租户/服务/可见性检索Runbook。 |
| `apps/common/web_search.py` | 当前使用 | 受限Tavily联网搜索；限制查询长度、脱敏常见Token和密码、最多返回4条结果，并明确联网结果只作参考。 |
| `apps/common/docker_adapters.py` | 当前使用 | 本地Docker证据适配层；按白名单和Compose标签查询容器状态及最近30分钟日志，限制容器数、日志行数和返回字段。 |
| `apps/common/prometheus_adapter.py` | 当前使用 | 查询本地Prometheus；验证受管服务，读取HTTP、错误率、P99、请求并发以及SQLAlchemy/MySQL连接池监控指标。 |

## 8. `apps/crud`：数据库查询

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/crud/__init__.py` | 当前使用 | 将CRUD目录标记为Python包。 |
| `apps/crud/incidents.py` | 当前使用 | 提供按事件ID查询和按数量倒序查询事件的数据库函数，供HTTP路由调用。 |

## 9. `apps/ingest`：告警接入

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/ingest/__init__.py` | 当前使用 | 将告警接入目录标记为Python包。 |
| `apps/ingest/main.py` | 当前使用 | 运行8100端口的FastAPI Webhook；根据来源选择解析器，执行限流和去重，再把统一告警写入Kafka。 |
| `apps/ingest/schemas.py` | 当前使用 | 定义统一告警的状态枚举和Pydantic数据模型，规范事件ID、租户、服务、环境、级别、标签和时间等字段。 |
| `apps/ingest/parsers.py` | 当前使用 | 将Alertmanager兼容的Prometheus载荷转换为统一告警；当前只注册 `prometheus` 来源。 |
| `apps/ingest/dedupe.py` | 当前使用 | 使用Redis按告警指纹、服务和环境做时间窗口去重，并对每个服务实施简单速率限制。 |
| `apps/ingest/kafka_sink.py` | 当前使用 | 管理Ingest自己的Kafka生产者，并将标准化告警发送到 `alert.triggered`。 |

## 10. `apps/worker`：异步事件工作流

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/worker/__init__.py` | 当前使用 | 将Worker目录标记为Python包。 |
| `apps/worker/main.py` | 当前使用 | Worker常驻入口；分别消费告警接入、研判请求和动作请求Topic，创建事件、运行Graph、处理失败/DLQ、执行批准后的动作并保存结果。 |
| `apps/worker/graph.py` | 当前使用 | LangGraph主工作流：去重、分类、ReAct只读取证、证据收集、Tavily确定性兜底、最终分析、RAG检索、结构化动作建议、后端校验、证据/风险评估和结果持久化。 |
| `apps/worker/tools.py` | 当前使用 | Worker工具门面；包装MCP只读/写工具与Tavily搜索，维护工具注册表，并把已批准动作转换成具体MCP调用。 |
| `apps/worker/mcp_client.py` | 当前使用 | 通过MCP Streamable HTTP连接本地MCP服务，调用指定工具并把MCP内容块整理成Python字典。 |
| `apps/worker/action_proposals.py` | 当前使用 | 定义结构化建议和提示词；严格校验 `none`、重启和请求并发调整，阻止跨服务、无证据或参数非法的动作。 |
| `apps/worker/assessment.py` | 当前使用 | 将每条工具结果标注为直接证据、监控证据、历史类比或网络参考；计算证据充分度与建议动作风险，并生成报告片段。 |

## 11. `apps/mcp-ops-server`：受控执行层

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/mcp-ops-server/__init__.py` | 当前使用 | 将MCP服务目录标记为Python包。 |
| `apps/mcp-ops-server/server.py` | 当前使用 | MCP工具服务器：提供Docker状态/日志、Prometheus指标、PostgreSQL历史事件查询；写工具会校验操作确认、白名单和幂等键，再执行并发调整或Docker重启。 |

## 12. `apps/seed`：RAG初始化

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/seed/__init__.py` | 辅助工具 | 将知识库初始化目录标记为Python包。 |
| `apps/seed/seed_docs.py` | 辅助工具 | 从项目根目录 `data/knowledge` 读取四份Runbook，附加租户、服务、角色和版本元数据后写入Chroma。 |

## 13. `apps/producer`

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/producer/__init__.py` | 辅助工具 | 将测试生产者目录标记为Python包。 |
| `apps/producer/simulator.py` | 辅助工具 | 随机生成10条模拟告警并发送到 `alert.triggered`，用于Kafka链路调试，不代表真实监控接入。 |
| `apps/__init__.py` | 当前使用 | 顶层Python包标记。 |

## 14. `apps/web`：React操作台

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/web/index.html` | 当前使用 | Vite前端HTML入口，提供React挂载节点。 |
| `apps/web/package.json` | 当前使用 | 前端项目元数据、React依赖以及开发/生产构建命令。 |
| `apps/web/package-lock.json` | 当前使用/自动生成 | npm精确依赖锁文件，保证Docker中的 `npm ci` 可复现。 |
| `apps/web/tsconfig.json` | 当前使用 | TypeScript编译选项。 |
| `apps/web/vite.config.ts` | 当前使用 | Vite和React插件配置，以及本地开发构建行为。 |
| `apps/web/src/main.tsx` | 当前使用 | React浏览器入口，将主组件挂载到页面DOM。 |
| `apps/web/src/Apps.tsx` | 当前使用 | 前端主界面：事件列表/详情、报告和引用、MCP只读工具、请求并发调整、人工审批、执行结果、审计记录、事件创建、重新研判及SSE流式Agent对话。 |
| `apps/web/src/styles.css` | 当前使用 | 全局页面、事件队列、报告、审批、聊天、表单和弹窗样式。 |
| `apps/web/src/tool-panel.css` | 当前使用 | MCP工具区按钮、状态和结果面板样式。 |
| `apps/web/src/pool-panel.css` | 当前使用 | Payment API请求并发限制器表单、提示和错误样式。 |

## 15. `apps/data/chroma`：本地向量数据

这些文件是Chroma/HNSW自动生成的本地运行数据，不是源代码，不应手工编辑。

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `apps/data/chroma/chroma.sqlite3` | 自动生成 | Chroma集合、文档元数据和索引元数据数据库。 |
| `apps/data/chroma/b88da082-ea04-49dd-9c60-4b9a678d17de/data_level0.bin` | 自动生成 | HNSW向量索引的底层数据文件。 |
| `apps/data/chroma/b88da082-ea04-49dd-9c60-4b9a678d17de/header.bin` | 自动生成 | HNSW索引头部和结构参数。 |
| `apps/data/chroma/b88da082-ea04-49dd-9c60-4b9a678d17de/length.bin` | 自动生成 | 向量索引长度相关数据。 |
| `apps/data/chroma/b88da082-ea04-49dd-9c60-4b9a678d17de/link_lists.bin` | 自动生成 | HNSW图结构的邻接链接数据。 |

## 16. `data/knowledge`：RAG知识文档

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `data/knowledge/payment-timeout.md` | 当前使用 | Payment API支付超时的现象、指标、下游检查、处置和允许动作。 |
| `data/knowledge/db-pool-exhausted.md` | 当前使用 | 数据库连接池耗尽排查Runbook，并强调应用请求并发上限与数据库连接池不是同一概念。 |
| `data/knowledge/prometheus-restart.md` | 当前使用 | Prometheus异常与安全重启Runbook，包含配置验证、影响评估和处置步骤。 |

## 17. `payment_api`：被监控的支付业务服务

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `payment_api/__init__.py` | 当前使用 | Payment API顶层包标记。 |
| `payment_api/app/__init__.py` | 当前使用 | Payment API应用包标记。 |
| `payment_api/app/config.py` | 当前使用 | 读取支付数据库、环境、服务名和请求并发限制参数。 |
| `payment_api/app/main.py` | 当前使用 | Payment FastAPI入口；初始化MySQL、恢复并发目标，提供幂等支付、状态和Prometheus指标接口，并记录HTTP延迟。 |
| `payment_api/app/db.py` | 当前使用 | 实现可动态扩缩的异步请求信号量，维护当前/目标上限、活跃请求、等待请求和平滑缩容。 |
| `payment_api/app/metrics.py` | 当前使用 | 声明请求并发、等待、支付失败、HTTP耗时、数据库连接池和数据库操作耗时等Prometheus指标。 |
| `payment_api/app/models.py` | 当前使用 | 定义MySQL的 `payments` 和 `payment_concurrency_config` 两张表；订单号是支付幂等键。 |
| `payment_api/app/storage.py` | 当前使用 | 创建MySQL异步引擎，初始化表，幂等保存支付记录，读取/保存并发目标值，并从SQLAlchemy连接池刷新监控指标。 |
| `payment_api/routers/__init__.py` | 当前使用 | Payment API路由包标记。 |
| `payment_api/routers/db.py` | 当前使用 | 提供请求并发状态、扩容和缩容接口；扩容必须在限制器饱和且等待数超过阈值时才允许，缩容支持平滑回收许可。 |

## 18. `payment_api/load-test`：压测脚本

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `payment_api/load-test/__init__.py` | 辅助工具 | 将压测目录标记为Python包。 |
| `payment_api/load-test/pool_pressure.py` | 当前推荐的辅助工具 | 安全边界较清晰的本地并发压测：只访问回环地址、生成唯一测试订单、要求显式确认测试数据，用于触发请求等待指标和Prometheus告警。 |
## 19. `infra/prometheus`：本地监控与告警

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `infra/prometheus/prometheus.yml` | 当前使用 | 配置2秒抓取/评估周期，抓取Payment API指标，加载告警规则，并把告警发送给Alertmanager。 |
| `infra/prometheus/alert-rules.yml` | 当前使用 | 定义 `PaymentRequestConcurrencySaturated`；请求等待数持续大于5秒后触发critical告警。 |
| `infra/prometheus/alertmanager.yml` | 当前使用 | 配置告警分组与发送间隔，把告警和恢复通知转发到Ingest的 `/webhook/prometheus`。 |

## 20. `dockerfiles`：镜像与代理配置

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `dockerfiles/incident-api.Dockerfile` | 当前使用 | 基于Python 3.14和uv构建Incident API/Worker共享镜像，复制应用代码与知识文档，默认启动Incident API。 |
| `dockerfiles/mcp-ops-server.Dockerfile` | 当前使用 | 构建MCP镜像，并从Docker CLI镜像复制 `docker` 与Compose插件，使批准后的本地容器重启可执行。 |
| `dockerfiles/payment-api.Dockerfile` | 当前使用 | 构建根目录Payment API镜像，默认在8001端口启动。 |
| `dockerfiles/web.Dockerfile` | 当前使用 | 多阶段构建React前端，再用Nginx提供静态页面。 |
| `dockerfiles/nginx.conf` | 当前使用 | 把 `/api/` 反向代理到Incident API，处理SPA回退，并关闭SSE缓冲以支持Agent流式回答。 |
## 21. `docs`：设计和维护文档

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `docs/architecture.md` | 当前文档 | 描述产品边界、运行拓扑、信任边界、事件状态机、集成契约、SLO和部署标准。 |
| `docs/api.md` | 当前文档 | 说明 HTTP 接口清单、MCP 工具、审批流程、事件状态取值和幂等约定。 |
| `docs/docker-service-status.md` | 当前文档 | 说明Docker状态证据的查询范围、返回值、部署验证和自动测试。 |
| `docs/roadmap.md` | 规划文档 | 按里程碑记录生产基础、可观测性、安全处置和操作体验的待办事项；部分内容可能落后于当前实现。 |
| `docs/project-file-reference.md` | 当前文档 | 本文件；维护项目文件职责、主链路和生成文件边界。 |

## 22. 根目录 `tests`：平台回归测试

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `tests/test_action_proposals.py` | 当前测试 | 测试事件分类、结构化动作Schema、重启/并发证据约束、跨服务拒绝、Graph动作持久化与失败降级。 |
| `tests/test_agent_finalization.py` | 当前测试 | 测试Agent轮次和工具预算、最后一批证据总结、模型超时/非法输出降级及最终模型禁用工具。 |
| `tests/test_compose_configuration.py` | 当前测试 | 静态验证Compose服务、内部连接、健康检查、告警链路和统一重启配置。 |
| `tests/test_copilot_streaming.py` | 当前测试 | 测试事件问答工具选择、模型分片、失败降级、SSE事件以及完整回答/工具证据持久化。 |
| `tests/test_docker_service_status.py` | 当前测试 | 测试Docker容器发现、状态/健康判断、异常归一化、服务白名单、日志范围限制和MCP包装。 |
| `tests/test_evidence_assessment.py` | 当前测试 | 测试证据来源、有效样本、历史/网页降权、风险与证据分离、数据库升级和历史事件查询。 |
| `tests/test_local_ingest.py` | 当前测试 | 验证只注册Prometheus Webhook，并正确标准化Alertmanager告警。 |
| `tests/test_payment_pool_wait_metric.py` | 当前测试 | 确认服务指标查询使用根Payment API的请求等待指标，并区分数据库连接池指标。 |
| `tests/test_pool_pressure.py` | 当前测试 | 测试压测脚本生成唯一订单、发送合法支付字段并统计失败响应。 |
| `tests/test_request_concurrency_limiter.py` | 当前测试 | 测试请求信号量即时扩容和平滑缩容，不会在缩容期间超额放行。 |
| `tests/test_seed_docs.py` | 当前测试 | 测试知识文档路径、服务映射、缺失文件跳过和正常写入。 |
| `tests/test_web_search.py` | 当前测试 | 测试Tavily Key检查、敏感信息脱敏、结果归一化、先本地后联网、单次限制、Graph自动兜底和报告状态。 |

## 24. `payment_api/tests`：支付服务契约测试

| 文件 | 状态 | 作用 |
| --- | --- | --- |
| `payment_api/tests/__init__.py` | 当前测试 | 将Payment API测试目录标记为Python包。 |
| `payment_api/tests/test_payment.py` | 当前测试 | 验证支付、状态、并发管理和指标路由存在，并检查Agent依赖的Prometheus指标名称。 |

## 25. 不属于业务源码的目录

| 目录 | 作用与处理方式 |
| --- | --- |
| `.venv/` | uv/Python虚拟环境，包含第三方解释器和依赖，自动生成，不提交。 |
| `apps/web/node_modules/` | npm第三方依赖，使用 `package-lock.json` 和 `npm ci` 重建。 |
| `apps/web/dist/` | Vite生产构建结果，由 `npm run build` 生成。 |
| `.idea/` | JetBrains/PyCharm本地项目配置，不参与运行；其中可能包含个人数据源信息。 |
| `.jbeval/` | 当前为空的本地工具目录，不参与项目运行。 |
| `.git/` | Git仓库内部对象、引用和配置，不属于应用源码。 |
| `__pycache__/`、`*.pyc` | Python字节码缓存，可随时重新生成。 |

## 26. 当前应重点维护的文件

日常功能修改最常涉及以下文件：

1. Agent工作流：`apps/worker/graph.py`、`action_proposals.py`、`assessment.py`。
2. 工具与执行安全：`apps/worker/tools.py`、`apps/mcp-ops-server/server.py`。
3. API与审批：`apps/routers/incidents.py`。
4. 前端：`apps/web/src/Apps.tsx` 和三个CSS文件。
5. 支付演示服务：`payment_api/app/*`、`payment_api/routers/db.py`。
6. 监控链路：`infra/prometheus/*`。
7. 部署：根目录 `docker-compose.yml` 和 `dockerfiles/*`。
8. 回归保护：根目录 `tests/*`。


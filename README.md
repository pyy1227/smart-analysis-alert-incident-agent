# Sentinel Incident Response Platform

[![CI](https://github.com/pyy1227/Smart-Analytics-Alert-Incident-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/pyy1227/Smart-Analytics-Alert-Incident-Agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Sentinel 是一个个人维护的 AI 辅助事件响应平台。它接收来自监控系统的告警，结合 Runbook、历史事件和实时只读诊断生成处置建议；所有有副作用的操作由项目所有者确认后执行，并以可审计、可重试的任务运行。

这是单人维护的本地演示项目，默认当前使用者就是系统所有者。重点展示可靠事件状态机、跨服务归因、操作确认、审计、异步执行、可观测性和可扩展的集成层。

## 业务域

当前演示只保留已经接通的支付服务能力：

| 关键服务 | 主要信号 | 常见处置 |
| --- | --- | --- |
| Payment API | 成功率、P99、请求排队、数据库连接池、5xx | 调整请求并发上限、重启异常容器 |

允许执行的写操作只有调整请求并发上限和重启受管容器。

## 核心能力

- 标准化告警接入：本地 Prometheus / Alertmanager Webhook → 统一事件模型。
- 事件工作流：去重、关联、证据检索、风险分级、所有者确认、执行、复盘。
- MCP 执行层：只读诊断可直接调用；写操作通过单人确认记录和幂等键执行。
- 审计与安全：每一次确认、取消、调用和执行结果均带有 trace ID，并统一记录为本地所有者操作。
- 可靠执行：Kafka 作为异步执行通道；交互式本地环境可回退为同步执行。
- 运维可观测性：Payment API 请求、错误率、耗时、请求并发和数据库连接池指标。

## 快速开始

### 环境要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Docker Desktop（Linux 容器模式）+ Compose 插件 | 4.x / Compose v2 | 一键启动全部服务，推荐用这个方式体验 |
| Python + [uv](https://docs.astral.sh/uv/) | 3.14 | 跑单元测试、本地直连调试单个服务 |
| Node.js | 24 | 仅前端本地开发需要，用 Compose 时由镜像内构建 |

### 一键安装依赖

用 Docker 启动时依赖会装进镜像，只有**本地跑测试或直连调试**时才需要执行本节。

```powershell
# 方式 A（推荐）：uv，与 Docker 镜像、CI 使用的依赖完全一致
uv sync --frozen

# 方式 B：不想装 uv，用 venv + pip，版本取自 requirements.txt
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

前端依赖单独安装（与 Python 无关）：

```powershell
Set-Location apps/web
npm ci        # 锁定版本来自 package-lock.json
```

> `requirements.txt` 是从 `uv.lock` 导出的完整清单：**直接依赖和所有间接依赖都固定了版本**，共 142 个包，
> 也包含跨平台条件标记（例如 `uvloop` 在 Windows 上会自动跳过）。
> 它是生成文件，请勿手工编辑；新增或升级依赖请改 `pyproject.toml` 后按
> [CONTRIBUTING.md](CONTRIBUTING.md#依赖变更) 的方式重新导出，两份文件不一致时以 `uv.lock` 为准。

### 启动

从项目根目录执行：

```powershell
Copy-Item .env.compose.example .env.compose   # 编辑后填入本地凭据；该文件已被 .gitignore 排除
docker compose --env-file .env.compose config --quiet   # 先校验配置再启动
docker compose --env-file .env.compose up -d --build
docker compose --env-file .env.compose ps
```

首次构建需要下载镜像和依赖，耗时几分钟。全部容器 healthy 后即可访问：

| 地址 | 服务 |
| --- | --- |
| http://localhost:5173 | 前端控制台 |
| http://localhost:8000/docs | incident-api（Swagger，事件 / 审批 / 死信） |
| http://localhost:8001/docs | payment-api（Swagger，被监控的支付服务） |
| http://localhost:8100/health | ingest（告警 Webhook 入口） |
| http://localhost:9090 | Prometheus |
| http://localhost:9094 | Alertmanager |

### 常用命令

```powershell
docker compose --env-file .env.compose logs -f worker          # 工作流日志
docker compose --env-file .env.compose logs -f ingest          # 告警接入日志
docker compose --env-file .env.compose logs -f mcp-ops-server  # MCP 执行层日志
docker compose --env-file .env.compose down                    # 停止；不要加 -v，否则命名卷会被删除
```

### 触发一次告警

模拟 Alertmanager 投递一条 firing 告警，走完 `ingest → Kafka → worker → 事件页`。

**推荐用脚本**（已处理 Windows PowerShell 5.1 的中文编码问题）：

```powershell
# 最简：给 prometheus 发一条告警
.\scripts\send-alert.ps1

# 指定服务和告警名
.\scripts\send-alert.ps1 -Service payment-api -AlertName PaymentHighLatency -Severity critical

# 一次发 3 条，演示去重（同一 fingerprint 5 分钟内只会建一个事件）
.\scripts\send-alert.ps1 -Service payment-api -Count 3

# 如果提示"未对文件进行数字签名"，用这行绕过执行策略
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\send-alert.ps1
```

**或者内联调用**：

```powershell
$body = @{
  alerts = @(@{
    status      = "firing"
    labels      = @{ alertname = "PaymentHighLatency"; service = "payment-api"; severity = "critical"; environment = "staging" }
    annotations = @{ description = "payment-api P99 超过 2 秒" }
    startsAt    = (Get-Date).ToString("o")
  })
} | ConvertTo-Json -Depth 5

# Windows PowerShell 5.1 必须转成 UTF-8 字节，否则中文会变成问号
$utf8 = [System.Text.Encoding]::UTF8.GetBytes($body)
Invoke-RestMethod -Method Post -Uri http://localhost:8100/webhook/prometheus `
  -ContentType "application/json; charset=utf-8" -Body $utf8
```

返回 `{"ok": true, "accepted": [...], "count": 1}` 后刷新 http://localhost:5173 即可看到事件。分析耗时约 20-40 秒，状态会从 `analysing` 变成 `analyzed`。接口清单、审批与幂等细节见 [docs/api.md](docs/api.md)。

> 未配置 `DEEPSEEK_API_KEY` 时，对话会回退到固定回复；未配置 `DASHSCOPE_API_KEY` 时，RAG 向量生成不可用。这两项都不影响告警链路本身。

### 演示：审批后重启容器

`restart_service` 只能由"分析 → 给出建议 → 你确认"触发，而且分析阶段必须拿到容器异常证据。现场演示建议用脚本走确定性路径（跳过模型提建议这一步，审批之后的链路完全真实）：

```powershell
.\scripts\demo-restart.ps1                        # 默认重启 alertmanager
.\scripts\demo-restart.ps1 -Service prometheus    # 换目标服务
```

脚本会依次：写入演示事件和待确认记录 → 调用确认接口（等价于前端点「确认」）→ 轮询执行结果 → 用 alertmanager 的 `uptime` 前后对比证明容器确实重启过。结束时打印清理演示数据的 SQL。

> 想演示完整链路（让模型自己提出重启建议）：先 `docker compose --env-file .env.compose stop prometheus` 制造真实异常，再 `.\scripts\send-alert.ps1 -Service prometheus`，等事件变成 `awaiting_approval` 后在前端确认。

## 本地开发（不走 Docker）

需要先用 Docker 起依赖（PostgreSQL / MySQL / Redis / Kafka），再单独跑某个 Python 服务：

```powershell
docker compose --env-file .env.compose up -d postgres mysql redis kafka
Copy-Item .env.example .env      # 填入本地连接串与 key
uv sync --frozen
uv run uvicorn apps.api.main:app --port 8000            # 事件 API
uv run uvicorn apps.ingest.main:app --port 8100         # 告警接入
uv run python -m apps.worker.main                       # 工作流 Worker
uv run python -m apps.mcp-ops-server.server             # MCP 执行层（默认 127.0.0.1:8931）
```

前端热更新（Vite 已把 `/api` 代理到 `http://localhost:8000`）：

```powershell
Set-Location apps/web
npm ci
npm run dev        # http://localhost:5173
```

测试与提交前检查：

```powershell
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s payment_api/tests -v
```

## Docker Compose 说明

根目录 `docker-compose.yml` 启动前端、事件 API、Worker、MCP、告警接收、根目录的 `payment_api`、PostgreSQL、MySQL、Redis、Kafka、Prometheus 和 Alertmanager。需要 Docker Desktop 的 Linux 容器模式及 Compose 插件。

**安全提示：仅供可信的本机开发环境使用。** MCP 容器挂载了 Docker daemon socket，以便审批后重启受管容器；能访问 socket 的进程可以获得对 Docker 主机的广泛控制权。当前 MCP 和告警 Webhook 并非生产级鉴权，示例密码也不安全。不要把此配置直接部署到生产或暴露给不可信网络。生产环境应使用受限的独立执行器、身份认证和密钥管理。

前端通过 Nginx 将 `/api` 代理到 `incident-api`；Worker 通过 Kafka 接收事件；告警通过 `Prometheus → Alertmanager → ingest → Kafka → worker` 流转。MCP 默认仅在 Compose 网络内监听。

### 当前运行说明

- Compose 项目名固定为 `incident-agent`，PostgreSQL、MySQL、Kafka、Chroma、Prometheus 和 Alertmanager 使用各自的命名卷。
- 数据库镜像只在首次初始化空卷时使用数据库密码环境变量；更改 `.env.compose` 不会自动修改卷中的数据库账号密码。
- 当前只读服务状态和日志工具直接查询本地 Docker Compose，指标查询本地 Prometheus。RAG 向量生成需要在 `.env.compose` 中配置 `DASHSCOPE_API_KEY`；前端对话需要配置 `DEEPSEEK_API_KEY`，未配置或调用失败时会回退到固定回复。

如果需要对外部署，必须补充生产级身份认证和权限隔离；不得直接暴露本地 Docker socket。

## 目录结构

```text
incident-agent/
├─ apps/
│  ├─ ingest/            # 告警接入：Webhook → 限流/去重 → Kafka
│  ├─ worker/            # LangGraph 工作流、动作执行、失败与死信处理
│  ├─ api/ + routers/    # incident-api 入口及事件 / 审批 / 死信 / 并发路由
│  ├─ mcp-ops-server/    # MCP 执行层：只读诊断 + 受控写操作
│  ├─ agent/             # 事件页对话助手（流式输出）
│  ├─ common/            # 配置、数据库、Kafka、Redis、LLM、RAG、Prometheus、Docker 适配器
│  ├─ seed/              # Runbook 入库脚本
│  └─ web/               # React + Vite 前端
├─ payment_api/          # 被监控的独立支付服务（含压测脚本）
├─ data/knowledge/       # Runbook 知识库（RAG 语料）
├─ docs/                 # 架构、Kafka、接口、路线图等文档
├─ dockerfiles/          # 各服务镜像定义与 nginx 配置
├─ infra/prometheus/     # 抓取配置、告警规则、Alertmanager 配置
├─ scripts/              # 演示脚本：发告警、审批后重启容器
├─ tests/                # 单元测试（unittest）
├─ docker-compose.yml    # 本地一键启动
├─ pyproject.toml        # Python 依赖声明（权威来源）
├─ uv.lock               # 锁定全部依赖版本
└─ requirements.txt      # 由 uv.lock 导出的 pip 安装清单
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 运行拓扑、信任边界、事件状态机、集成契约、SLO |
| [docs/api.md](docs/api.md) | HTTP 接口清单、MCP 工具、审批与幂等约定 |
| [docs/docker-service-status.md](docs/docker-service-status.md) | 只读服务状态 / 日志工具的白名单与实现 |
| [docs/project-file-reference.md](docs/project-file-reference.md) | 逐文件职责说明（改动前先查这里） |
| [docs/roadmap.md](docs/roadmap.md) | 实施路线与后续计划 |
| [CONTRIBUTING.md](CONTRIBUTING.md) / [SECURITY.md](SECURITY.md) | 贡献流程与安全问题报告方式 |

## 提交到 GitHub 前

本仓库可公开提交的内容是源码、测试、文档、Docker 配置、`.env.example` / `.env.compose.example` 模板和 `uv.lock`。请勿提交 `.env`、`.env.compose`、`.venv`、`.idea`、本地 Chroma 数据或构建产物；这些内容已由 `.gitignore` 排除。

提交前请执行：

```powershell
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s payment_api/tests -v
Set-Content -Path .env.compose -Value (Get-Content .env.compose.example -Raw)
docker compose --env-file .env.compose config --quiet
```

然后编辑 `.env.compose`，填入自己的本地开发凭据。任何曾出现在文件、终端输出或聊天记录中的真实 API key 都应在服务提供商处轮换；GitHub Secrets 只用于 CI/CD 的部署凭据，不应用来替代本地 `.env.compose`。

仓库已包含 CI、贡献说明、安全披露方式、PR / Issue 模板和 MIT 许可证。提交 PR 时请按 `.github/pull_request_template.md` 填写验证命令。

## 许可证

本项目以 [MIT License](LICENSE) 发布。若要改用其它许可证，替换 `LICENSE` 文件内容并同步更新本节与 `README` 顶部的徽章即可。

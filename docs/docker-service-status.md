# Docker 服务状态证据

`query_service_status(service_id)` 查询本地 Docker Compose 容器。
MCP 版本、其他查询工具和重启/审批逻辑未变。

## 定位和返回值

- `apps/common/docker_adapters.py` 中的 `SERVICE_DOCKER_TARGETS` 是固定白名单。
  当前项目为 `incident-agent`，支持 payment-api、incident-api、prometheus、alertmanager。
- MCP 同时保留 `MANAGED_SERVICES` 的限制。
- 根据 `com.docker.compose.project` 和 `com.docker.compose.service` 标签查询，
  包含已停止容器。启动 Compose 时若使用 `-p` 改项目名，必须同步修改后端映射。
- `containers` 数组返回所有匹配实例的 service_id、source、container_id、
  container_name、container_status、running、health_status、started_at、finished_at、
  exit_code、oom_killed、restart_count。不会返回环境变量或完整 inspect。
- 顶层 `status=ok` 表示采集成功，不代表容器或业务健康；已停止容器也属于有效证据。
- 未配置健康检查时 `health_status=unknown`。应结合 running 和 container_status 判断，
  不能只用历史健康状态判断已停止容器。
- `status=not_found` 表示该标签组合未找到容器，不等于停止。
- `status=unavailable` 和 error 表示命令缺失、Docker 不可达、权限、超时、响应格式等问题。
  如果容器在查询期间被删除，重新查询即可。
- 单次最多 50 个实例，两个 Docker 命令分别限制 5 秒。Docker socket 本身并非只读权限；
  此工具仅执行只读命令，不应把 socket 暴露给前端或不可信调用方。

## 部署和验证（项目根目录，Windows CMD）

```cmd
docker compose --env-file .env.compose up -d --build --no-deps mcp-ops-server
docker compose --env-file .env.compose exec -T worker python -c "import asyncio; from apps.worker.tools import query_service_status; print(asyncio.run(query_service_status('payment-api')))"
```

返回的 source 应为 docker。将容器 ID 与下面的查询对照：

```cmd
docker ps -a --filter "label=com.docker.compose.project=incident-agent" --filter "label=com.docker.compose.service=payment-api"
```

在前端创建新事件或重新研判，报告会通过已有 collect_evidence 流程显示新结果；
已经保存的旧报告不会自动改写。无需重建前端或 Worker。

## 自动测试

```cmd
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_docker_service_status.py" -v
```

测试使用模拟 Docker 输出，不会停止、重启或修改任何容器；真实 Docker 连通性需要上述命令验证。

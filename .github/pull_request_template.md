## 变更说明

<!-- 这个 PR 解决什么问题？为什么现在要做？关联的 Issue 编号（如有）。 -->

## 影响范围

- [ ] 事件工作流 / 状态机
- [ ] MCP 执行层或所有者确认流程
- [ ] 告警接入（ingest / Kafka）
- [ ] 数据模型或迁移
- [ ] Compose / 基础设施 / 可观测性
- [ ] 前端
- [ ] 文档

## 验证

<!-- 粘贴实际执行过的命令和结果，而不是"应该能通过"。 -->

```powershell
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s payment_api/tests -v
Set-Location apps/web; npm ci; npm run build
docker compose --env-file .env.compose.example config --quiet
```

## 自查

- [ ] 没有提交密钥、令牌、`.env`、本地数据库 / 向量库数据或构建产物
- [ ] 行为变更已补充或更新测试，`uv.lock` 与 `pyproject.toml` 保持同步
- [ ] 前端依赖变更已同步 `apps/web/package-lock.json`
- [ ] 没有削弱"模型只读诊断 + 所有者确认后执行"的安全边界
- [ ] 涉及 Compose、端口或数据模型时，已同步更新 `README` / `docs/`

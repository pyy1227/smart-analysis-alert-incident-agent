# Contributing

感谢改进 Sentinel。该项目是面向本地开发和演示的事件响应平台；请勿将它部署到不受信任的网络或生产环境。

## 开发与验证

使用 Python 3.14、[uv](https://docs.astral.sh/uv/) 和 Node.js 24：

```powershell
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s payment_api/tests -v
Set-Location apps/web
npm ci
npm run build
```

涉及 Compose、服务依赖或环境变量的改动，还必须通过：

```powershell
docker compose --env-file .env.compose.example config --quiet
```

## 提交要求

- 不提交密钥、令牌、密码、个人数据、Docker socket 内容或本地数据库 / 向量库数据。
- 为行为变更补充或更新测试；保持 `uv.lock`、`requirements.txt` 与 `pyproject.toml` 同步（见下节）。
- 前端依赖变更需同步更新 `apps/web/package-lock.json`。
- 在 Pull Request 中说明动机、验证命令，以及是否影响 Compose、数据模型或安全边界。

## 依赖变更

三个文件各司其职，不要混用：

| 文件 | 角色 |
| --- | --- |
| `pyproject.toml` | 直接依赖声明，唯一手工编辑的入口 |
| `uv.lock` | 全部依赖的锁定版本，由 uv 生成 |
| `requirements.txt` | 从 `uv.lock` 导出的 pip 安装清单，供不用 uv 的人使用 |

新增或升级依赖后，按顺序执行并把三个文件一起提交：

```powershell
uv add <包名>              # 或用 uv remove 移除；也可以先改 pyproject.toml 再 uv lock
uv lock                    # 刷新 uv.lock
uv export --frozen --format requirements.txt --no-hashes --no-annotate --no-emit-project -o requirements.txt
uv sync --frozen           # 本地验证锁定依赖可以安装
```

只改 `pyproject.toml` 不同步 `uv.lock` 会让 CI 的 `uv sync --frozen` 直接失败；忘了重新导出 `requirements.txt` 则会让用 pip 的人装到旧版本。`uv.lock` 和 `requirements.txt` 都不要手工编辑。

## Issue 与 Pull Request

- 新建 Issue 时请选择「Bug 报告」或「功能建议」模板；模板要求填写环境信息和复现步骤，并提醒先脱敏。
- Pull Request 请按 `.github/pull_request_template.md` 填写变更说明、影响范围和**实际执行过**的验证命令。
- 环境变量统一从 `.env.example` 复制为本地 `.env`；新增变量时请同时更新 `.env.example`，否则新贡献者无法启动。

安全问题请不要公开提交 Issue，改按 [SECURITY.md](SECURITY.md) 中的方式报告。

## 许可证

提交即表示同意以本仓库的 [MIT 许可证](LICENSE) 授权你的贡献。

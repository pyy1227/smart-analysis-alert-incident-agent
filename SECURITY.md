# Security Policy

## Supported versions

仅维护 `main` 分支上的最新版本。本项目仅供可信的本地开发和演示环境使用，不适合直接暴露到公网或作为生产事件响应系统运行。

## Reporting a vulnerability

请不要为潜在漏洞创建公开 GitHub Issue，也不要在报告中粘贴真实密钥、令牌、客户数据或日志。

请使用仓库所有者在 GitHub 个人资料中公布的私密联系方式报告，并提供：

- 受影响的组件与版本 / commit；
- 可复现步骤和实际影响；
- 已验证的缓解措施（如有）。

维护者会确认收到报告、评估影响并协商披露时间。若仓库尚未公开私密联系方式，请先不要公开细节，改联系仓库所有者后再报告。

## Local deployment warning

Compose 开发环境中的 MCP 服务会挂载 Docker socket，以执行经确认的受管容器重启。任何能控制该容器的进程都可能获得 Docker 主机的广泛控制权。不要在生产环境或不可信网络中使用此配置。

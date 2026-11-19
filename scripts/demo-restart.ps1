<#
.SYNOPSIS
    演示"审批后由 MCP 重启受管容器"的完整链路。

.DESCRIPTION
    写入一条 awaiting_approval 演示事件和一条 pending 审批记录，然后调用确认接口，
    由 action worker 通过 MCP 执行 docker compose restart，最后打印执行结果与审计日志。
    alertmanager 作为目标时，会用 /api/v2/status 的 uptime 前后对比证明容器确实重启过。

    前置条件：docker compose --env-file .env.compose up -d 已启动。

.EXAMPLE
    .\scripts\demo-restart.ps1

.EXAMPLE
    .\scripts\demo-restart.ps1 -Service prometheus

.NOTES
    如果提示"未对文件进行数字签名"，用：
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-restart.ps1
#>
param(
    [ValidateSet("alertmanager", "prometheus", "payment-api", "incident-api")]
    [string]$Service = "alertmanager"
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 控制台默认 GBK，先切成 UTF-8 才能正常显示中文。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Warning "没找到 .venv，改用系统 python；建议先执行 uv sync --frozen"
    $python = "python"
}

& $python (Join-Path $PSScriptRoot "demo_restart.py") --service $Service
exit $LASTEXITCODE

<#
.SYNOPSIS
    向本地 ingest 投递一条 Alertmanager 兼容告警，用于演示
    ingest → Kafka → worker → 事件页 的完整链路。

.DESCRIPTION
    默认投递 prometheus 服务的 firing 告警。需要 ingest 容器在运行：
    docker compose --env-file .env.compose up -d

.EXAMPLE
    # 最简用法：给 prometheus 发一条告警
    .\scripts\send-alert.ps1

.EXAMPLE
    # 指定服务和告警名
    .\scripts\send-alert.ps1 -Service payment-api -AlertName PaymentHighLatency -Severity critical

.EXAMPLE
    # 一次发 3 条（演示去重：只有第一条会建事件）
    .\scripts\send-alert.ps1 -Service payment-api -Count 3
#>
param(
    [string]$Service = "prometheus",
    [string]$AlertName = "",
    [ValidateSet("info", "warning", "critical")][string]$Severity = "critical",
    [string]$Environment = "staging",
    [string]$Description = "",
    [int]$Count = 1,
    [string]$IngestUrl = "http://localhost:8100/webhook/prometheus"
)

# 服务是 prometheus 时，默认告警名用 PrometheusDown，其它服务用 <service>Down。
if (-not $AlertName) { $AlertName = "${Service}Down" }
if (-not $Description) { $Description = "$Service 容器状态异常" }

$alerts = @()
for ($i = 0; $i -lt $Count; $i++) {
    $alerts += @{
        status      = "firing"
        labels      = @{
            alertname   = $AlertName
            service     = $Service
            severity    = $Severity
            environment = $Environment
        }
        annotations = @{ description = $Description }
        startsAt    = (Get-Date).ToString("o")
    }
}

$payload = @{ alerts = $alerts } | ConvertTo-Json -Depth 5
# Windows PowerShell 5.1 用默认编码发送字符串时会把中文变成问号，这里显式转 UTF-8 字节。
$utf8 = [System.Text.Encoding]::UTF8.GetBytes($payload)

try {
    $response = Invoke-RestMethod -Method Post -Uri $IngestUrl `
        -ContentType "application/json; charset=utf-8" -Body $utf8
}
catch {
    Write-Error ("投递失败：{0}`n请确认 ingest 在运行： docker compose --env-file .env.compose ps ingest" -f $_.Exception.Message)
    exit 1
}

Write-Host ("已投递 count={0} accepted={1}" -f $response.count, ($response.accepted -join ", ")) -ForegroundColor Green

if ($response.count -eq 0) {
    Write-Warning "没有被接受：可能命中了限流（每服务每 60 秒 100 条）或去重窗口（同一 fingerprint 5 分钟内重复）。"
}

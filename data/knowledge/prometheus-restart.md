# Runbook: Prometheus 服务异常与重启操作指南

## 1. 现象描述 (Symptoms)
当 Prometheus 出现以下情况时，触发此 Runbook：
*   告警规则（`ALERTS`）显示 `PrometheusDown` 或 `PrometheusTargetDown`。
*   业务监控面板（Grafana 等）数据中断，查询报 `502 Bad Gateway` 或 `Context Deadline Exceeded`。
*   访问 `http://localhost:9090/-/healthy` 返回非 200 状态码。
*   容器状态异常（通过 `docker ps` 查看，发现 `prometheus-demo` 状态为 `Restarting` 或 `Exited`）。

## 2. 可能原因 (Root Causes)
*   **配置文件错误**：修改了 `prometheus.yml` 或告警规则文件，存在 YAML 语法错误，导致热加载失败或重启崩溃。
*   **资源耗尽**：Prometheus 占用内存过大（OOM），被操作系统或 Docker 强行杀掉。
*   **存储卷异常**：磁盘空间满，或 `TSDB` 数据目录损坏，无法写入。
*   **采集目标不可达**：Prometheus 无法连接本地服务的指标接口（网络问题）。

## 3. 影响评估 (Impact)
*   **监控盲区**：在 Prometheus 恢复前，将无法进行指标采集、告警计算和指标查询。
*   **数据丢失**：如果发生 OOM 或强制重启，未持久化到磁盘的内存数据（最近几分钟的指标）可能会丢失。

## 4. 处置步骤 (Resolution Steps)

### 步骤 1：验证配置合法性（重中之重）
在重启之前，必须校验配置文件，避免因配置错误导致无限重启。
```bash
# 在宿主机存放 prometheus.yml 的目录下执行
docker run --rm -v "%cd%/prometheus.yml:/etc/prometheus/prometheus.yml" --entrypoint promtool prom/prometheus check config /etc/prometheus/prometheus.yml

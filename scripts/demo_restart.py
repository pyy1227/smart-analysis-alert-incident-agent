"""演示：审批后由 MCP 重启受管容器。

流程：写一条 awaiting_approval 事件 + pending 审批记录 → 调用确认接口 → 轮询执行结果。

审批之后的链路完全真实：
    API(approvals.status=approved) → Kafka incident.action.requested
    → action worker → MCP execute_runbook_step
    → docker compose restart <service>
    → 健康检查（最多 30 秒）→ 回写事件状态与审计日志

这条"确定性路径"跳过的是"让大模型自己提出重启建议"这一步，因为那依赖
Docker 容器异常证据和模型判断，现场演示容易不稳定。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import httpx
import psycopg

SERVICES = ("alertmanager", "prometheus", "payment-api", "incident-api")
TERMINAL = {"resolved", "executed", "execution_failed"}


def log(message: str) -> None:
    print(message, flush=True)


def build_action(service: str) -> dict:
    return {
        "type": "execute_runbook_step",
        "step": "restart_service",
        "params": {"service_id": service},
        "requires_approval": True,
    }


def insert_demo_rows(dsn: str, service: str) -> tuple[str, str]:
    incident_id = f"inc-demo-restart-{uuid.uuid4().hex[:6]}"
    approval_id = f"appr-demo-restart-{uuid.uuid4().hex[:6]}"
    action = build_action(service)
    alert = {"title": f"{service} 容器异常（演示）", "service_id": service, "status": "firing"}

    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute(
            """
            INSERT INTO incidents
                (id, event_id, status, severity, service_id, environment, tenant_id,
                 title, alert, citations, trace_id, approval_status, proposed_action)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::json, %s::json, %s, %s, %s::json)
            """,
            (
                incident_id,
                f"evt-{uuid.uuid4().hex}",
                "awaiting_approval",
                "critical",
                service,
                "staging",
                "demo",
                f"{service} 容器异常（演示）",
                json.dumps(alert, ensure_ascii=False),
                "[]",
                f"trace-{uuid.uuid4().hex}",
                "pending",
                json.dumps(action, ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO approvals (id, incident_id, action, status)"
            " VALUES (%s, %s, %s::json, %s)",
            (approval_id, incident_id, json.dumps(action, ensure_ascii=False), "pending"),
        )
    return incident_id, approval_id


def read_alertmanager_uptime(client: httpx.Client, base_url: str) -> str:
    try:
        response = client.get(f"{base_url.rstrip('/')}/api/v2/status")
        response.raise_for_status()
        return str(response.json().get("uptime", "unknown"))
    except httpx.HTTPError as error:
        return f"<不可达：{type(error).__name__}>"


def main() -> int:
    parser = argparse.ArgumentParser(description="演示审批后重启受管容器")
    parser.add_argument("--service", default="alertmanager", choices=SERVICES)
    parser.add_argument("--api", default="http://localhost:8000", help="incident-api 地址")
    parser.add_argument("--alertmanager", default="http://localhost:9094", help="用于校验重启的 uptime")
    parser.add_argument(
        "--dsn",
        default="host=127.0.0.1 port=15432 dbname=incident user=postgres password=123456",
        help="PostgreSQL 连接串（compose 把 postgres 映射到本机 15432）",
    )
    parser.add_argument("--timeout", type=int, default=90, help="等待执行完成的最长秒数")
    args = parser.parse_args()

    with httpx.Client(timeout=15.0, trust_env=False) as client:
        try:
            client.get(f"{args.api.rstrip('/')}/health").raise_for_status()
        except httpx.HTTPError as error:
            log(f"[X] incident-api 不可达：{type(error).__name__}。先执行 docker compose --env-file .env.compose up -d")
            return 1

        log(f"[1/5] 目标服务：{args.service}")
        uptime_before = (
            read_alertmanager_uptime(client, args.alertmanager)
            if args.service == "alertmanager"
            else None
        )
        if uptime_before is not None:
            log(f"      重启前 alertmanager uptime = {uptime_before}")

        log("[2/5] 写入演示事件与待确认记录 …")
        try:
            incident_id, approval_id = insert_demo_rows(args.dsn, args.service)
        except psycopg.Error as error:
            log(f"[X] 写库失败：{error}")
            log("    确认 postgres 端口映射存在（127.0.0.1:15432）且密码与 .env.compose 一致")
            return 1
        log(f"      incident = {incident_id}")
        log(f"      approval = {approval_id}")

        log("[3/5] 调用确认接口（等价于前端点「确认」）…")
        try:
            response = client.post(
                f"{args.api.rstrip('/')}/api/incidents/{incident_id}/approval",
                json={"approved": True, "reason": "演示：重启受管容器"},
            )
            response.raise_for_status()
            log(f"      {json.dumps(response.json(), ensure_ascii=False)}")
        except httpx.HTTPError as error:
            log(f"[X] 确认失败：{error}")
            return 1

        log("[4/5] 轮询执行结果 …")
        deadline = time.time() + args.timeout
        detail = {}
        while time.time() < deadline:
            time.sleep(3)
            try:
                detail = client.get(
                    f"{args.api.rstrip('/')}/api/incidents/{incident_id}"
                ).json()
            except httpx.HTTPError as error:
                log(f"      查询失败：{type(error).__name__}")
                continue
            status = detail.get("status")
            elapsed = int(time.time() - (deadline - args.timeout))
            log(f"      [{elapsed:>3}s] status={status}")
            if status in TERMINAL:
                break

        log("[5/5] 结果")
        log(f"      最终状态：{detail.get('status')} / approval={detail.get('approval_status')}")
        log("      execution_result：" + json.dumps(detail.get("execution_result"), ensure_ascii=False))
        audits = [item.get("action") for item in detail.get("audits", [])]
        log("      审计动作：" + ", ".join(str(a) for a in audits))

        if uptime_before is not None:
            uptime_after = read_alertmanager_uptime(client, args.alertmanager)
            log(f"      重启后 alertmanager uptime = {uptime_after}")
            changed = uptime_after != uptime_before and not uptime_after.startswith("<")
            log("      容器确实重启：%s" % ("是" if changed else "无法确认"))

    log("")
    log("清理演示数据（可选）：")
    log(f"  DELETE FROM approvals WHERE id = '{approval_id}';")
    log(f"  DELETE FROM incidents WHERE id = '{incident_id}';")
    log(f"  DELETE FROM audit_logs WHERE incident_id = '{incident_id}';")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows PowerShell 5.1 的控制台是 GBK，统一成 UTF-8 避免中文乱码。
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())

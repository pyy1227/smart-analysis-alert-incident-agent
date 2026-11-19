import json
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

china_time = ZoneInfo("Asia/Shanghai")

# 受管服务与 Compose 服务的对应关系。
SERVICE_DOCKER_TARGETS = {
    "payment-api": {
        "project": "incident-agent",
        "service": "payment-api",
    },
    "incident-api": {
        "project": "incident-agent",
        "service": "incident-api",
    },
    "prometheus": {
        "project": "incident-agent",
        "service": "prometheus",
    },
    "alertmanager": {
        "project": "incident-agent",
        "service": "alertmanager",
    },
}


class DockerQueryError(RuntimeError):
    pass


def _run_docker(command: list[str]) -> str:
    """执行有时限的只读命令；不向调用方泄露原始 stderr。"""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=True,
            shell=False,
        )
    except FileNotFoundError as error:
        raise DockerQueryError("运行环境中找不到 Docker 命令") from error
    except subprocess.TimeoutExpired as error:
        raise DockerQueryError("查询 Docker 容器超时") from error
    except subprocess.CalledProcessError as error:
        raise DockerQueryError(
            "Docker 查询失败，请检查 Docker daemon、socket 权限或容器是否已被删除"
        ) from error
    except OSError as error:
        raise DockerQueryError("无法执行 Docker 查询命令") from error
    return result.stdout


def _run_docker_logs(command: list[str]) -> str:
    """执行有时限的只读日志命令，并合并容器的标准输出和错误输出。"""
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=True,
            shell=False,
        )
    except FileNotFoundError as error:
        raise DockerQueryError("运行环境中找不到 Docker 命令") from error
    except subprocess.TimeoutExpired as error:
        raise DockerQueryError("查询 Docker 日志超时") from error
    except subprocess.CalledProcessError as error:
        raise DockerQueryError("Docker 日志查询失败") from error
    except OSError as error:
        raise DockerQueryError("无法执行 Docker 日志查询命令") from error
    return result.stdout


def find_service_containers(service_id: str) -> list[str]:
    """根据受控映射，查询服务的所有容器 ID，包括停止的容器。"""

    target = SERVICE_DOCKER_TARGETS.get(service_id)
    if target is None:
        raise DockerQueryError(f"不允许查询服务：{service_id}")

    command = [
        "docker",
        "ps",
        "--all",
        "--quiet",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={target['project']}",
        "--filter",
        f"label=com.docker.compose.service={target['service']}",
    ]

    container_ids = list(dict.fromkeys(_run_docker(command).split()))
    # 只接受完整容器 ID。
    if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in container_ids):
        raise DockerQueryError("Docker 返回了无效的容器 ID")
    if len(container_ids) > 50:
        raise DockerQueryError("匹配的容器超过单次查询上限（50），请检查服务映射")
    return container_ids


# 只读取需要的容器字段。
# 按照给定格式查询指定字段
_INSPECT_FORMAT = (
    '{"container_id":{{json .Id}},'
    '"container_name":{{json .Name}},'
    '"container_status":{{json .State.Status}},'
    '"running":{{json .State.Running}},'
    '"health_status":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"unknown"{{end}},'
    '"started_at":{{json .State.StartedAt}},'
    '"finished_at":{{json .State.FinishedAt}},'
    '"exit_code":{{json .State.ExitCode}},'
    '"oom_killed":{{json .State.OOMKilled}},'
    '"restart_count":{{json .RestartCount}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}}}'
)


def query_service_status(service_id: str) -> dict:
    """采集容器状态；status=ok 表示查询成功，不表示业务健康。"""
    container_ids = find_service_containers(service_id)
    target = SERVICE_DOCKER_TARGETS[service_id]
    result = {
        "service_id": service_id,
        "source": "docker",
        "compose_project": target["project"],
        "compose_service": target["service"],
        "status": "ok" if container_ids else "not_found",
        "containers": [],
    }
    if not container_ids:
        result["observed_at"] = datetime.now(china_time).isoformat()
        return result

    # 一次查询全部实例。
    output = _run_docker([
        "docker", "container", "inspect", "--format", _INSPECT_FORMAT,
        *container_ids,
    ])
    seen = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise DockerQueryError("Docker 返回的状态不是有效 JSON") from error
        if not isinstance(item, dict):
            raise DockerQueryError("Docker 返回的容器状态格式无效")
        container_id = item.get("container_id")
        if not isinstance(container_id, str) or container_id not in container_ids or container_id in seen:
            raise DockerQueryError("Docker 返回的容器与查询目标不一致")
        if item.get("project") != target["project"] or item.get("service") != target["service"]:
            raise DockerQueryError("容器标签与允许查询的 Compose 项目或服务不一致")

        string_fields = ("container_name", "container_status", "started_at", "finished_at")
        if any(not isinstance(item.get(key), str) for key in string_fields):
            raise DockerQueryError("Docker 返回的容器状态字段缺失或无效")
        if any(type(item.get(key)) is not bool for key in ("running", "oom_killed")):
            raise DockerQueryError("Docker 返回的容器状态字段缺失或无效")
        if any(type(item.get(key)) is not int for key in ("exit_code", "restart_count")):
            raise DockerQueryError("Docker 返回的容器状态字段缺失或无效")

        health = item.get("health_status")
        if health not in ("healthy", "unhealthy", "starting"):
            health = "unknown"
        result["containers"].append({
            "service_id": service_id,
            "source": "docker",
            "container_id": container_id,
            "container_name": item["container_name"].lstrip("/"),
            "container_status": item["container_status"],
            "running": item["running"],
            "health_status": health,
            "started_at": item["started_at"],
            "finished_at": item["finished_at"],
            "exit_code": item["exit_code"],
            "oom_killed": item["oom_killed"],
            "restart_count": item["restart_count"],
        })
        seen.add(container_id)
    if seen != set(container_ids):
        raise DockerQueryError("Docker 未返回全部目标容器的状态，请重新查询")
    result["observed_at"] = datetime.now(china_time).isoformat()
    return result


def query_service_logs(service_id: str, query: str = "", *, limit: int = 50) -> dict:
    """读取白名单服务的近期日志；查询词只在 Python 中过滤，不传给 shell。"""
    if not 1 <= limit <= 200:
        raise DockerQueryError("日志条数上限必须在 1 到 200 之间")
    if not isinstance(query, str) or len(query) > 500:
        raise DockerQueryError("日志查询内容无效或过长")

    status = query_service_status(service_id)
    containers = status.get("containers", [])
    if not containers:
        return {
            "service_id": service_id,
            "source": "docker-logs",
            "status": "not_found",
            "results": [],
            "observed_at": datetime.now(china_time).isoformat(),
            "limitations": ["仅查询本地 Compose 容器最近 30 分钟、每容器最多 500 行日志。"],
        }

    tokens = [token.casefold() for token in re.findall(r"[\w.-]+", query) if len(token) >= 2]
    entries = []
    for container in containers[:10]:
        output = _run_docker_logs([
            "docker", "logs", "--timestamps", "--since", "30m", "--tail", "500",
            container["container_id"],
        ])
        for line in output.splitlines():
            timestamp, separator, message = line.partition(" ")
            if not separator:
                timestamp, message = "", timestamp
            if tokens and not any(token in message.casefold() for token in tokens):
                continue
            entries.append({
                "container_name": container["container_name"],
                "timestamp": timestamp,
                "message": message[:2000],
            })

    return {
        "service_id": service_id,
        "source": "docker-logs",
        "status": "returned" if entries else "no_data",
        "query": query,
        "results": entries[-limit:],
        "observed_at": datetime.now(china_time).isoformat(),
        "limitations": ["仅查询本地 Compose 容器最近 30 分钟、每容器最多 500 行日志。"],
    }

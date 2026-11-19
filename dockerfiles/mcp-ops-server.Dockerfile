FROM docker:27-cli AS docker_cli
FROM ghcr.io/astral-sh/uv:0.10.7 AS uv
FROM python:3.14-slim

COPY --from=uv /uv /uvx /bin/
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY apps ./apps
COPY docker-compose.yml ./docker-compose.yml
CMD ["uv", "run", "--no-sync", "python", "-m", "apps.mcp-ops-server.server"]

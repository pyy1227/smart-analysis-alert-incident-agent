FROM ghcr.io/astral-sh/uv:0.10.7 AS uv

FROM python:3.14-slim

COPY --from=uv /uv /uvx /bin/

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Keep dependency installation in a separate layer so source changes can use
# Docker's build cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY payment_api ./payment_api

EXPOSE 8001
CMD ["uv", "run", "--no-sync", "uvicorn", "payment_api.app.main:app", "--host", "0.0.0.0", "--port", "8001"]

FROM oven/bun:1-alpine AS web
WORKDIR /web
# bun.loc[k] is a glob so the build still works before the lockfile exists; a
# real one is matched and honoured by --frozen-lockfile.
COPY apps/frontend/package.json apps/frontend/bun.loc[k] ./
RUN bun install --frozen-lockfile
COPY apps/frontend/ ./
RUN bun run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
COPY apps/backend/pyproject.toml apps/backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY apps/backend/app/ ./app/
COPY apps/backend/models.yaml ./models.yaml
COPY apps/backend/alembic.ini ./alembic.ini
COPY apps/backend/alembic/ ./alembic/
COPY --from=web /web/dist ./console

FROM python:3.12-slim-bookworm
COPY --from=builder /app /app
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001
WORKDIR /app
ENV PATH=/app/.venv/bin:$PATH
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

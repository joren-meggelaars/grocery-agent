FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/files && chown -R app:app /data && chmod 700 /data/files
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
# Single instance: migrate, then serve. --no-proxy-headers: X-Forwarded-* is never trusted.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory grocery.main:create_app --host 0.0.0.0 --port 8000 --no-proxy-headers"]

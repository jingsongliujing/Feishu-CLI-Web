# syntax=docker/dockerfile:1

FROM node:20-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    API_PREFIX=/api/v1 \
    FRONTEND_DIST_DIR=/app/frontend/dist \
    LARK_CLI_COMMAND_TIMEOUT=120

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        nodejs \
        npm \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @larksuite/cli \
    && npx skills add larksuite/cli -y -g

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/ /app/backend/
COPY .env.example /app/.env.example
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

RUN mkdir -p /app/.feishu_cli_data

WORKDIR /app/backend
EXPOSE 8000
VOLUME ["/app/.feishu_cli_data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()" || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

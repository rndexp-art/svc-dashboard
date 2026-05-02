# Dashboard service — FastAPI + Jinja. Runs on port 8003.
#
# Mirrors the auth service Dockerfile shape: uv installs into the system
# Python from a pinned requirements.txt, no venv, non-root.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.5.11

WORKDIR /app

COPY requirements.txt pyproject.toml /app/
RUN uv pip install --system --no-cache -r requirements.txt

COPY app/ /app/app/

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin dashboardsvc \
    && chown -R dashboardsvc:dashboardsvc /app
USER dashboardsvc

EXPOSE 8003

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM python:3.12-slim
WORKDIR /app
# PYTHONPATH makes the shared `common` package importable.
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
COPY gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/ ./common/
COPY gateway/main.py .
COPY gateway/static/ ./static/
# `exec` keeps uvicorn as PID 1 so SIGTERM triggers graceful Consul deregistration.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${SERVICE_PORT}

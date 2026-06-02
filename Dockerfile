FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bluez dbus \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir ".[ble]"

EXPOSE 8000

CMD ["uvicorn", "pitboss_bridge.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

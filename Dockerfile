FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# EXTRAS selects the pytboss source: "http" (WiFi, default) or "ble"
# (needs BlueZ/D-Bus, see docker-compose.ble.yml).
ARG EXTRAS=http

WORKDIR /app

RUN if [ "$EXTRAS" = "ble" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends bluez dbus \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir ".[${EXTRAS},mqtt]"

RUN useradd --system --uid 10001 --create-home pitboss \
    && mkdir /data \
    && chown pitboss /data
USER pitboss

ENV PITBOSS_DB=/data/pitboss.sqlite
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "pitboss_bridge.healthcheck"]

CMD ["uvicorn", "pitboss_bridge.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

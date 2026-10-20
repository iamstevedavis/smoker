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

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir ".[${EXTRAS},mqtt]"

# Fixed gid to match SMOKESTACK_RUN_AS in docker-compose.yml.
RUN groupadd --system --gid 10001 smokestack \
    && useradd --system --uid 10001 --gid 10001 --create-home smokestack \
    && mkdir /data \
    && chown smokestack:smokestack /data
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/smokestack-entrypoint
USER smokestack

ENV SMOKESTACK_DB=/data/smokestack.sqlite
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["smokestack-entrypoint", "python", "-m", "smokestack.healthcheck"]

ENTRYPOINT ["smokestack-entrypoint"]
CMD ["uvicorn", "smokestack.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

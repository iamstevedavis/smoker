FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

# One image for WiFi and Bluetooth. Bluetooth needs no system packages: bleak
# talks to the host's bluetoothd over the host's D-Bus socket, which
# docker-compose.yml mounts, using the pure-Python dbus-fast.
RUN python -m pip install --no-cache-dir ".[mqtt]"

# The default user without compose; docker-compose.yml runs as `nobody` instead
# (see docker/entrypoint.sh).
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

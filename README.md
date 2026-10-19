# Pit Boss Bridge

A local, read-only bridge for Pit Boss pellet grills (built for the `PB850PS2`). It polls the grill over WiFi or Bluetooth, stores the history in SQLite, and serves a live dashboard and a JSON API. It never sends commands to the grill.

![Dashboard](docs/images/dashboard-desktop.png)

## Quick start (Docker)

Runs on any always-on machine (NAS, mini-PC, Raspberry Pi). No Bluetooth adapter needed.

```bash
git clone https://github.com/iamstevedavis/smoker.git && cd smoker
cp .env.example .env        # set PITBOSS_HOST to the grill's IP (and PITBOSS_PASSWORD)
docker compose up -d --build
```

Open `http://<server>:8000/`, or check `curl http://<server>:8000/api/health`.

No grill handy? Try the fake source: `PITBOSS_SOURCE=fake docker compose up --build`.

- Find the grill's IP in your router's DHCP list and give it a reservation.
- Multi-arch images (amd64, arm64) are published to `ghcr.io/iamstevedavis/smoker` on each `v*` tag: `docker compose pull && docker compose up -d`.
- Data lives on the `pitboss-data` volume. Upgrading, backup and migration: [docs/docker.md](docs/docker.md).
- Not every grill serves local HTTP (ESP-IDF boards such as `PBL2`/`PBC2` don't). If the bridge says so, use [Bluetooth](docs/ble.md): `docker compose -f docker-compose.ble.yml up -d --build`.

## Security

- **The bridge API has no authentication.** Don't expose port 8000 to the internet; use a VPN (WireGuard, Tailscale) or an authenticating reverse proxy.
- The grill's local HTTP RPC exposes its configuration, including stored **WiFi credentials**, to anyone on the network. Keep the grill on an IoT VLAN that only the bridge host can reach. Details: [docs/testing/2026-10-18-cook-test.md](docs/testing/2026-10-18-cook-test.md).

## Configuration

Set these in `.env` (Docker) or the environment.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PITBOSS_SOURCE` | `fake` (`http` in Docker Compose) | `http` (WiFi), `ble` or `fake` |
| `PITBOSS_HOST` | | Grill LAN address for `http`, `host` or `host:port` |
| `PITBOSS_DEVICE_NAME` | | BLE device name for `ble` |
| `PITBOSS_PASSWORD` | | Grill password, if set |
| `PITBOSS_MODEL` | `PB850PS2` | Grill model |
| `PITBOSS_DB` | `data/pitboss.sqlite` | SQLite path (`/data/pitboss.sqlite` in Docker) |
| `PITBOSS_POLL_SECONDS` | `2` | Polling interval |
| `PITBOSS_STALE_AFTER_SECONDS` | `30` | Age at which a reading counts as stale |
| `PITBOSS_RETENTION_DAYS` | `0` | Delete data older than this; `0` keeps everything |
| `PITBOSS_LOG_LEVEL` | `INFO` | Use `DEBUG` when troubleshooting |

Home Assistant (`PITBOSS_MQTT_*`) and ntfy alerts (`PITBOSS_NTFY_*`, `PITBOSS_ALERT_*`) are optional and off by default. Every variable and its default is listed in [docs/integrations.md](docs/integrations.md#settings).

## More

- [API reference](docs/api.md): history, cook sessions, health
- [Home Assistant and alerts](docs/integrations.md)
- [Bluetooth setup](docs/ble.md)
- [Docker operations](docs/docker.md): upgrade, backup, migrating the old `./data` mount

## Development

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[test]"
pytest
PITBOSS_SOURCE=fake uvicorn pitboss_bridge.api:create_app --factory --reload
```

On Windows, run this inside WSL2 (Ubuntu). The dashboard is plain HTML/CSS/JS in `src/pitboss_bridge/static/` with no build step.

# Smokestack

A self-hosted, read-only dashboard and alert service for Pit Boss pellet grills (built for the `PB850PS2`). It polls the grill over WiFi or Bluetooth, stores the history in SQLite, and serves a live dashboard and a JSON API. It never sends commands to the grill.

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

- **Auth is off by default**, so anyone on the LAN can use the dashboard and API. Set `PITBOSS_UI_PASSWORD` to require a shared password for the dashboard, `/api/*` (except `/api/health`), the `/api/live` WebSocket and `/docs`. Sessions are signed cookies; the signing key is `session.key` next to the database (`/data` in Docker), and deleting it or changing the password signs everyone out.
- **On plain HTTP the password and session cookie cross the LAN unencrypted.** That's fine for home use, but don't expose port 8000 to the internet: use a VPN (WireGuard, Tailscale), or put a TLS reverse proxy such as Caddy in front.
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
| `PITBOSS_UI_PASSWORD` | | Shared password for the dashboard and API; unset means no login |
| `PITBOSS_SESSION_DAYS` | `30` | How long a login lasts |
| `PITBOSS_SETTINGS_EDITABLE` | on with a UI password | `false` makes the settings panel read-only; `true` allows editing without a login |

Home Assistant (`PITBOSS_MQTT_*`) and ntfy alerts (`PITBOSS_NTFY_*`, `PITBOSS_ALERT_*`) are optional and off by default. Every variable and its default is listed in [docs/integrations.md](docs/integrations.md#settings).

### Changing settings from the dashboard

The gear button opens a settings panel. **Save & apply** restarts the grill connection, MQTT and alerts inside the bridge, so there's no container restart and the page stays up.

- **Precedence:** built-in default, then `.env`/environment, then values saved in the panel. Saved values win.
- Saved values live in the SQLite database in the data volume (`/data` in Docker), so they survive restarts and upgrades. Each field shows where its value comes from (Default, .env or UI); **Reset** drops the saved value and falls back to `.env` or the default.
- Passwords and tokens are write-only: the panel shows whether they are set, never their value.
- Editing needs a login, since otherwise anyone on the LAN could point MQTT or ntfy at their own server and receive the saved secrets. Without `PITBOSS_UI_PASSWORD` the panel is read-only unless you set `PITBOSS_SETTINGS_EDITABLE=true`.
- **Env only:** `PITBOSS_DB`, `PITBOSS_PORT`, `PITBOSS_START_COLLECTOR`, `PITBOSS_UI_PASSWORD`, `PITBOSS_SESSION_DAYS` and `PITBOSS_SETTINGS_EDITABLE`.
- The source list only offers what can actually run: `fake` always, `http` (WiFi) in every image, and `ble` only when the host's Bluetooth is reachable, which in Docker means `docker-compose.ble.yml` ([docs/ble.md](docs/ble.md)). So switching from WiFi to Bluetooth means switching compose files.

## More

- [API reference](docs/api.md): history, cook sessions, settings, health
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

## License

[AGPL-3.0-or-later](LICENSE). You can use, change and share it freely. If you
run a modified version as a service for other people, you must offer them its
source.

## Credits

Grill communication comes from [pytboss](https://github.com/dknowles2/pytboss)
(Apache-2.0) by David Knowles.

Smokestack is an independent project, not affiliated with or endorsed by
Dansons or Pit Boss. Pit Boss is a trademark of its owner, used here only to
say which grills this works with.

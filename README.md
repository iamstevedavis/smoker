# Pit Boss Bridge

Monitoring-only local bridge for a Pit Boss `PB850PS2` grill.

Milestone 1 uses a fake source for local development and exposes:

- `GET /api/health`
- `GET /api/state`
- `GET /api/history`
- `WS /api/live`
- `/api/cooks` (cook sessions, see below)

`GET /api/history` query parameters (all optional; the response is always
`{"count": n, "items": [...]}`):

- `since` / `until`: ISO time window on `observed_at` (both inclusive)
- `limit`: max items, 1-10000 (default 1000)
- `order`: `asc` (default, oldest first) or `desc`. With `desc` the *latest*
  `limit` items are returned, newest first
- `bucket_seconds`: downsample to one item per bucket (1-86400). Numeric fields
  are averaged, booleans and strings take the last value in the bucket. Items
  carry the bucket start as `observed_at`, a `samples` count, and empty
  `unknown`/`raw`. `limit` then counts buckets, e.g. a 14-hour cook at
  `bucket_seconds=60` is 840 points

## Cook sessions

Name and review a cook. These endpoints write to the **bridge database only**,
never to the grill. Only one cook can be active at a time.

```bash
# start (409 if a cook is already active)
curl -X POST localhost:8000/api/cooks -H 'content-type: application/json' \
  -d '{"name": "Brisket", "meat": "beef", "weight": "12 lb", "target_internal_temp": 203}'
curl -X PATCH localhost:8000/api/cooks/1 -H 'content-type: application/json' \
  -d '{"notes": "wrapped at 165"}'
curl -X POST localhost:8000/api/cooks/1/stop
curl localhost:8000/api/cooks            # newest first
curl localhost:8000/api/cooks/1          # includes summary stats
curl 'localhost:8000/api/cooks/1/history?bucket_seconds=60'
```

`GET /api/cooks/{id}` adds a `summary` (duration, reading count, min/max/avg
grill temperature, max probe temperatures). `/history` takes the same `limit`,
`order` and `bucket_seconds` parameters as `/api/history`, restricted to the
cook's window.

Real BLE validation should happen on a Linux host near the grill.

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
pytest
PITBOSS_SOURCE=fake uvicorn pitboss_bridge.api:create_app --factory --reload
```

On Windows with WSL2, run these commands inside Ubuntu so Python 3.10 is used.

## Web Dashboard

The bridge serves a read-only dashboard at `/` (for example
`http://localhost:8000/`). It shows grill vs set temperature, both probes,
power/fan/igniter/auger, error flags, and a chart of the last 1-12 hours that
updates live over `WS /api/live`. A banner appears when the bridge can't be
reached, the grill is disconnected, or readings are older than
`PITBOSS_STALE_AFTER_SECONDS`. The API and `/docs` are unchanged.

![Dashboard](docs/images/dashboard-desktop.png)

It is plain HTML/CSS/JS in `src/pitboss_bridge/static/` with no build step,
and works as an installable app (PWA). Browsers only allow installing from
`localhost` or HTTPS: on a phone over plain `http://<laptop-ip>:8000/`, the
dashboard works in the browser, and iOS "Add to Home Screen" still gives a
full-screen icon, but Android/Chrome's install prompt needs HTTPS (for example
behind a reverse proxy). The service worker caches only the page shell, never
API data.

## Testing With A Real Grill (Linux Laptop)

Written for Linux Mint 22 / Ubuntu 24.04. Mint 21 ships Python 3.10, which is
too old; the setup script explains how to add a newer Python.

```bash
git clone https://github.com/iamstevedavis/smoker.git
cd smoker
./scripts/setup.sh    # one time: packages, Bluetooth checks, venv, tests, grill scan
./scripts/run.sh      # start the bridge; logs go to logs/
./scripts/report.sh   # afterwards: writes reports/report-*.txt
```

`setup.sh` scans for nearby `PBL-...` devices and saves the chosen grill and
optional password to `.env`. Before scanning, turn the grill on and close the
Pit Boss phone app -- the grill usually allows only one Bluetooth connection.

`run.sh` blocks idle suspend while it runs. Keep the laptop plugged in, lid
open, and within a few metres of the grill. It prints URLs for `/api/state`,
`/api/health`, and `/docs` that you can open from a phone on the same network.

Suggested test:

1. Quick check: compare `/api/state` with the grill display, change the set
   temperature on the grill and watch it update.
2. Drop test: break the connection (walk the laptop out of range, or
   `sudo systemctl restart bluetooth`), restore it, and confirm `/api/health`
   shows `reconnect_count: 1`.
3. A full cook with a probe in.

Then run `./scripts/report.sh` and share the report file.

## Linux BLE Setup (Manual)

For real Bluetooth testing on a Linux host, use a Python 3.13 virtualenv for now.
Python 3.14 cannot currently resolve the BLE dependencies used by this repo.

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[ble,test]"
bluetoothctl scan on
```

Once you know the smoker's advertised BLE name, start the bridge with BLE enabled:

```bash
PITBOSS_SOURCE=ble \
PITBOSS_MODEL=PB850PS2 \
PITBOSS_DEVICE_NAME='REAL_DEVICE_NAME' \
uvicorn pitboss_bridge.api:create_app --factory --reload
```

In another terminal, check the health and state endpoints:

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/state
```

If `PITBOSS_DEVICE_NAME` does not match what `bluetoothctl` shows, the BLE source will not connect.

## WiFi Source (Local HTTP)

The `http` source polls the grill over your home network instead of BLE. It
avoids BLE's range limits and single-connection restriction, needs no
BlueZ/D-Bus, and in testing never hit the spurious `Unauthorized` errors BLE
does. It is read-only, like the other sources.

```bash
python -m pip install -e ".[http,test]"
PITBOSS_SOURCE=http \
PITBOSS_HOST=192.168.1.50 \
PITBOSS_MODEL=PB850PS2 \
PITBOSS_PASSWORD='...' \
uvicorn pitboss_bridge.api:create_app --factory
```

Find the grill's IP in your router's DHCP client list, or read `wifi.sta_ip`
from `Sys.GetInfo` over BLE. Give the grill a DHCP reservation so the address
does not change.

Not every grill serves local HTTP. ESP-IDF boards (`PBL2`, `PBC2`, ...) have
no HTTP server, and on Mongoose boards it is a per-unit setting. If the bridge
reports that the grill may not serve local HTTP, use the `ble` source.

**Security:** the grill's local HTTP RPC is unauthenticated for config calls,
and those calls expose its configuration, including stored WiFi credentials.
Keep the grill on an isolated or IoT network. See
`docs/testing/2026-10-18-cook-test.md`.

## Configuration

- `PITBOSS_SOURCE`: `fake`, `ble` or `http`
- `PITBOSS_MODEL`: grill model, default `PB850PS2`
- `PITBOSS_DEVICE_NAME`: BLE device name for the `ble` source
- `PITBOSS_HOST`: grill LAN address for the `http` source, a bare host or `host:port`
- `PITBOSS_PASSWORD`: optional grill password
- `PITBOSS_DB`: SQLite path, default `data/pitboss.sqlite`
- `PITBOSS_POLL_SECONDS`: source polling interval, default `2`
- `PITBOSS_STALE_AFTER_SECONDS`: age threshold for stale data, default `30`
- `PITBOSS_RETENTION_DAYS`: delete readings and health events older than this many
  days, at startup and then daily; default `0` keeps everything
- `PITBOSS_MQTT_HOST`, `PITBOSS_MQTT_PORT` (default `1883`), `PITBOSS_MQTT_USER`,
  `PITBOSS_MQTT_PASSWORD`, `PITBOSS_MQTT_PREFIX` (default `pitboss`): MQTT
  publishing, disabled when the host is unset (see Home Assistant)
- `PITBOSS_NTFY_URL`, `PITBOSS_NTFY_TOKEN`: ntfy topic URL and optional access
  token; alerts are off when the URL is unset (see Alerts)
- `PITBOSS_ALERT_PROBE1_TARGET`, `PITBOSS_ALERT_PROBE2_TARGET`: probe alert
  temperatures, in the grill's unit; unset disables that alert
- `PITBOSS_ALERT_DRIFT_DEGREES` (default `25`), `PITBOSS_ALERT_DRIFT_MINUTES`
  (default `10`): grill-vs-set-temp drift alert
- `PITBOSS_ALERT_STALE_SECONDS`: seconds without data before a disconnect alert,
  default `60`
- `PITBOSS_ALERT_COOLDOWN_SECONDS`: minimum gap before the same alert can fire
  again, default `0`
- `PITBOSS_LOG_LEVEL`: bridge log level, default `INFO` (use `DEBUG` when
  troubleshooting BLE)

## Home Assistant

The bridge can publish to MQTT with Home Assistant discovery, so the grill shows
up as a device with no custom integration. It is read-only: there are no command
topics. Install the extra and point it at your broker:

```bash
python -m pip install -e ".[mqtt]"
PITBOSS_MQTT_HOST=192.168.1.10 PITBOSS_MQTT_USER=ha PITBOSS_MQTT_PASSWORD=... \
  uvicorn pitboss_bridge.api:create_app --factory
```

Home Assistant (with the MQTT integration and discovery enabled) creates a
**Pit Boss Grill** device with temperature sensors for the grill, set
temperature and both probes (unit follows the grill's `isFahrenheit`), binary
sensors for power, fan, igniter and auger, and a `Problem` sensor that turns on
when any error flag is set. Entities go unavailable when the source is
disconnected or stale (`pitboss/availability`, with a last-will of `offline`).

Topics, with the default prefix: `pitboss/state` (JSON of field values) and
`pitboss/availability`. The unreliable fields are not published. An unreachable
broker is retried with backoff and never affects `/api/*` or collection.

Example automation:

```yaml
automation:
  - alias: Brisket probe done
    trigger:
      - platform: numeric_state
        entity_id: sensor.pit_boss_grill_probe_1
        above: 200
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Probe 1 is at {{ states('sensor.pit_boss_grill_probe_1') }}"
```

To try it locally with a throwaway broker (the overlay also points the bridge
at it, and the image includes the `mqtt` extra):

```bash
docker compose -f docker-compose.yml -f docker-compose.mqtt.yml --profile mqtt up --build
```

## Alerts

The bridge can push notifications through [ntfy](https://ntfy.sh) with a plain
HTTP POST, so any phone with the ntfy app gets them. Set `PITBOSS_NTFY_URL` to
enable it:

```bash
# public ntfy.sh topic
PITBOSS_NTFY_URL=https://ntfy.sh/my-long-random-topic-name
# self-hosted, with an access token
PITBOSS_NTFY_URL=http://ntfy.lan/smoker PITBOSS_NTFY_TOKEN=tk_...
```

Subscribe to the same topic in the ntfy app. **A public topic name is effectively
a password**: anyone who guesses it can read your alerts, so pick a long random
one or self-host.

Alerts, each sent once per incident and re-armed when the condition clears:

- a probe reaches its target (`PITBOSS_ALERT_PROBE1_TARGET` / `..._PROBE2_TARGET`;
  the grill's own `p1Target` is not used). Re-arms 5 degrees below the target.
- the grill is more than 25 degrees from its set temperature for 10 minutes while on
- no data for 60 seconds, plus a "recovered" message
- an error flag turns on (`noPellets` is sent as urgent)

Sending happens in a background task with a timeout. A failed send is logged and
recorded as an `alert_error` health event; sent alerts are recorded as
`alert_sent`. The collector is never affected.

## Health Reporting

`GET /api/health` is the main signal during a real-grill test:

- `connected`: true only once a reading has arrived from the current session
- `reconnect_count`: sessions that recovered after a lost connection
- `consecutive_failures`: failed attempts since the last good reading
- `last_error` / `last_error_at`: most recent source error

After a source error the collector waits 5s, then doubles the wait on each
consecutive failure up to 60s, and resets after a good reading. Connects,
errors, and retries are also logged and recorded as health events in SQLite.

## Run at home (Docker)

The recommended deployment is the WiFi (`http`) source in a container on any
always-on machine (NAS, mini-PC, Raspberry Pi). It needs no Bluetooth adapter,
host networking or D-Bus.

1. **Find the grill's IP** in your router's DHCP client list (or `wifi.sta_ip`
   from `Sys.GetInfo` over BLE) and give it a **DHCP reservation**.
2. **Configure:**

   ```bash
   cp .env.example .env     # then edit PITBOSS_HOST (and PITBOSS_PASSWORD)
   ```

   ```ini
   PITBOSS_SOURCE=http
   PITBOSS_HOST=192.168.1.50
   PITBOSS_MODEL=PB850PS2
   PITBOSS_PASSWORD=
   ```

3. **Start:** `docker compose up -d --build`, then open
   `http://<server>:8000/` or `curl http://<server>:8000/api/health`.
   To try it without a grill: `PITBOSS_SOURCE=fake docker compose up --build`.

The container runs as a non-root user, keeps the SQLite database on the
`pitboss-data` volume (`PITBOSS_DB=/data/pitboss.sqlite`), restarts
automatically, and has a healthcheck on `/api/health`: healthy once the source
is delivering fresh (non-stale) readings, with a 2 minute startup grace period
(`PITBOSS_HEALTH_GRACE_SECONDS`).

Multi-arch images (amd64 and arm64, so a Raspberry Pi works) are published to
`ghcr.io/iamstevedavis/smoker` when a `v*` tag is pushed, so you can also run
the published image instead of building: `docker compose pull && docker compose up -d`.

**Upgrade:** `git pull && docker compose up -d --build` (or `pull` for the
published image). The database lives on the volume and survives upgrades.

**Back up** the database volume:

```bash
docker run --rm -v smoker_pitboss-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/pitboss-data.tgz -C /data .
```

(The volume name is `<project>_pitboss-data`; check `docker volume ls`.)

**Bluetooth variant:** `docker compose -f docker-compose.ble.yml up -d --build`.
It builds with the `ble` extra, uses host networking and read-only D-Bus mounts
so `bleak` can reach the host's BlueZ, and runs as root for D-Bus access. Set
`PITBOSS_DEVICE_NAME` in `.env`.

### Security

- **The bridge API has no authentication.** Do not expose port 8000 to the
  internet. For remote access put it behind a VPN (WireGuard, Tailscale) or an
  authenticating reverse proxy.
- The grill's local HTTP RPC leaks its configuration, including stored **WiFi
  credentials**, to anyone on the network. Isolate the grill on an IoT VLAN and
  allow only the bridge host to reach it. See `docs/testing/2026-10-18-cook-test.md`.

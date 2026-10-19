# Pit Boss Bridge

Monitoring-only local bridge for a Pit Boss `PB850PS2` grill.

Milestone 1 uses a fake source for local development and exposes:

- `GET /api/health`
- `GET /api/state`
- `GET /api/history`
- `WS /api/live`

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
- `PITBOSS_LOG_LEVEL`: bridge log level, default `INFO` (use `DEBUG` when
  troubleshooting BLE)

## Health Reporting

`GET /api/health` is the main signal during a real-grill test:

- `connected`: true only once a reading has arrived from the current session
- `reconnect_count`: sessions that recovered after a lost connection
- `consecutive_failures`: failed attempts since the last good reading
- `last_error` / `last_error_at`: most recent source error

After a source error the collector waits 5s, then doubles the wait on each
consecutive failure up to 60s, and resets after a good reading. Connects,
errors, and retries are also logged and recorded as health events in SQLite.

## Docker Direction

The target server deployment is Linux with BlueZ. Start with the fake source:

```bash
docker compose up --build
curl http://localhost:8000/api/health
```

After the Python BLE probe works on the Linux host, switch:

```yaml
environment:
  PITBOSS_SOURCE: ble
  PITBOSS_MODEL: PB850PS2
  PITBOSS_DEVICE_NAME: PBL-EC6260C77A8C
```

The compose file uses host networking and read-only D-Bus mounts so `bleak`
can talk to the host Bluetooth stack.

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

## Configuration

- `PITBOSS_SOURCE`: `fake` or `ble`
- `PITBOSS_MODEL`: grill model, default `PB850PS2`
- `PITBOSS_DEVICE_NAME`: BLE device name for real source
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

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

## Configuration

- `PITBOSS_SOURCE`: `fake` or `ble`
- `PITBOSS_MODEL`: grill model, default `PB850PS2`
- `PITBOSS_DEVICE_NAME`: BLE device name for real source
- `PITBOSS_PASSWORD`: optional grill password
- `PITBOSS_DB`: SQLite path, default `data/pitboss.sqlite`
- `PITBOSS_POLL_SECONDS`: source polling interval, default `2`
- `PITBOSS_STALE_AFTER_SECONDS`: age threshold for stale data, default `30`

## Docker Direction

The target server deployment is Linux with BlueZ. The container should use the host Bluetooth stack via host networking and D-Bus mounts.

# Bluetooth (BLE) setup

The WiFi source is simpler; use BLE only if your grill has no local HTTP server.

## Linux laptop test

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

## Manual setup

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

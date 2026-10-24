# Bluetooth (BLE) setup

The WiFi source is simpler; use BLE only if your grill has no local HTTP server.

## Docker

The same `docker-compose.yml` runs WiFi and Bluetooth, so there's nothing
extra to start:

```bash
docker compose pull && docker compose up -d
```

Then choose **Bluetooth** under **Settings → Grill → Source** and fill in the
**Bluetooth name**; the bridge reconnects when the field saves. Or set it in
`.env` before starting:

```bash
PITBOSS_SOURCE=ble
PITBOSS_DEVICE_NAME=PBL-...
```

Needs a Linux host with a Bluetooth adapter and `bluetoothd` running. Docker
Desktop on macOS or Windows can't reach the host's Bluetooth, so there the
panel doesn't offer it.

The first connection can take a minute or two. A grill that is connected to
something else, usually the Pit Boss phone app, stops advertising, and the
bridge retries with growing delays of up to 60 seconds. Close the app and wait.

The container talks to the host's BlueZ through the host's D-Bus socket
(`/run/dbus`, mounted read-only), which is the only host resource it gets: no
host networking, so `SMOKESTACK_PORT` works as usual. The bridge runs unprivileged as `nobody` (uid 65534). The entrypoint
starts as root only to hand the data volume to that user, then drops root.

Why `nobody` and not the image's own `smokestack` user (uid 10001): the host's
D-Bus refuses connections from uids it has no user for. `nobody` exists on
every Linux host, and BlueZ as shipped by Debian 12+ (the base of Raspberry Pi
OS) and Ubuntu 24.04 lets any local user talk to it.

If the bridge reports `AccessDenied` from `org.bluez`, your distro limits
BlueZ to the `bluetooth` group. Set `SMOKESTACK_RUN_AS` in `.env` to a host
`uid:gid` that is allowed, for example your own user if it's in that group:

```bash
SMOKESTACK_RUN_AS=1000:1000
```

The container runs without Docker's AppArmor profile (`apparmor=unconfined`).
That profile has no D-Bus rules, so on AppArmor hosts such as Ubuntu, Mint
and Debian the host bus answers every call with `AccessDenied ... An AppArmor
policy prevents this sender`. It also runs without SELinux labelling
(`label=disable`): on SELinux hosts such as Fedora and RHEL the container
can't open the mounted `/run/dbus` at all, so the settings panel doesn't offer
Bluetooth. Don't relabel the mount with `:z` instead, which changes the label
on the host's own `/run/dbus`. The rest of the hardening stays: no root, no
capabilities, `no-new-privs`, Docker's seccomp filter, no host networking.

Upgrading from the earlier setups needs nothing extra for the data: the
first start hands the existing `smokestack-data` volume to `nobody`.

**Upgrading from `docker-compose.ble.yml`:** that file is gone, and it set
`PITBOSS_SOURCE=ble` itself. Add `PITBOSS_SOURCE=ble` to `.env` (unless you
saved the source in the settings panel), then run plain `docker compose up -d`
from the same folder. It's the same compose project and service, so it
replaces the old container and keeps the volume.

## Linux laptop test

Written for Linux Mint 22 / Ubuntu 24.04. Mint 21 ships Python 3.10, which is
too old; the setup script explains how to add a newer Python.

```bash
git clone https://github.com/iamstevedavis/smokestack.git
cd smokestack
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
python -m pip install -e ".[test]"
bluetoothctl scan on
```

Once you know the smoker's advertised BLE name, start the bridge with BLE enabled:

```bash
PITBOSS_SOURCE=ble \
PITBOSS_MODEL=PB850PS2 \
PITBOSS_DEVICE_NAME='REAL_DEVICE_NAME' \
uvicorn smokestack.api:create_app --factory --reload
```

In another terminal, check the health and state endpoints:

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/state
```

If `PITBOSS_DEVICE_NAME` does not match what `bluetoothctl` shows, the BLE source will not connect.

## Choosing the Bluetooth adapter

On a host with more than one Bluetooth adapter, the bridge uses the system
default, usually `hci0`. If another adapter has the better range (a USB dongle
with an external antenna, say), choose it under **Settings → Grill →
Bluetooth adapter**; the bridge reconnects through it.
The list shows the adapters BlueZ knows right now, with their alias and
address, and marks one that is powered off. A saved adapter that's unplugged
stays selected, marked "not found".

Or set it in `.env`:

```bash
SMOKESTACK_BLE_ADAPTER=hci1
```

From a shell, `hciconfig` (or `ls /sys/class/bluetooth`) shows the names;
`bluetoothctl list` shows each adapter's address and marks the default one.
Leave the setting empty, **System default** in the panel, to use the default.
It's Linux (BlueZ) only.

The startup log names the adapter (`Scanning for Pit Boss BLE device 'PBL-…'
on hci1`). If the adapter doesn't exist, the error names
`SMOKESTACK_BLE_ADAPTER`, in the log and as `last_error` in `/api/health`, and
the bridge keeps retrying.

`setup.sh` and a plain `bluetoothctl scan on` scan with the default adapter.
To scan with another one, run `bluetoothctl`, then `select <address>` and
`scan on`.

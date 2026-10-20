#!/usr/bin/env bash
# One-time setup for testing the bridge against a real grill on a Linux
# laptop (written for Linux Mint / Ubuntu). Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

say() { printf '\n==> %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
ask_yes() {
  local reply
  read -r -p "  $1 [Y/n] " reply
  [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

# --- Python ---------------------------------------------------------------
# 3.12 is the oldest version CI tests, and BLE dependencies don't resolve on 3.14 yet.
say "Looking for Python 3.12-3.13"
PYTHON=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; sys.exit(not ((3, 12) <= sys.version_info[:2] <= (3, 13)))' 2>/dev/null; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done

if [[ -z "$PYTHON" ]]; then
  found="$(python3 --version 2>/dev/null || echo 'none')"
  die "Need Python 3.12 or 3.13 (found: $found).
  Linux Mint 21.x ships Python 3.10. Either upgrade to Mint 22, or install a
  newer Python alongside it, e.g.:
      sudo add-apt-repository ppa:deadsnakes/ppa
      sudo apt update
      sudo apt install python3.12 python3.12-venv
  then re-run this script."
fi
PY_VERSION="$("$PYTHON" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
echo "  Using $PYTHON ($PY_VERSION)"

# --- System packages ------------------------------------------------------
say "Checking system packages"
packages=()
"$PYTHON" -c 'import ensurepip' 2>/dev/null || packages+=("python${PY_VERSION}-venv")
command -v bluetoothctl >/dev/null 2>&1 || packages+=("bluez")
command -v rfkill >/dev/null 2>&1 || packages+=("rfkill")
if ((${#packages[@]})); then
  echo "  Missing: ${packages[*]}"
  if ask_yes "Install them with sudo apt?"; then
    sudo apt-get update
    sudo apt-get install -y "${packages[@]}"
  else
    die "Install ${packages[*]} and re-run."
  fi
else
  echo "  All present"
fi

# --- Bluetooth ------------------------------------------------------------
say "Checking Bluetooth"
if ! systemctl is-active --quiet bluetooth; then
  warn "The bluetooth service is not running."
  if ask_yes "Start and enable it?"; then
    sudo systemctl enable --now bluetooth
  else
    die "Bluetooth service is required."
  fi
fi
if rfkill list bluetooth 2>/dev/null | grep -q 'Soft blocked: yes'; then
  warn "Bluetooth is turned off (soft blocked)."
  if ask_yes "Turn it on?"; then
    rfkill unblock bluetooth || sudo rfkill unblock bluetooth
    sleep 2
  fi
fi
if rfkill list bluetooth 2>/dev/null | grep -q 'Hard blocked: yes'; then
  die "Bluetooth is disabled by a hardware switch or BIOS setting. Enable it and re-run."
fi
if ! bluetoothctl list 2>/dev/null | grep -q '^Controller'; then
  die "No Bluetooth adapter found. Check that this laptop has Bluetooth and it is enabled."
fi
bluetoothctl power on >/dev/null 2>&1 || true
echo "  Adapter: $(bluetoothctl list | head -n1)"

# --- Python environment ---------------------------------------------------
say "Installing the bridge into .venv"
if [[ -x .venv/bin/python ]] && ! .venv/bin/python -c \
  'import sys; sys.exit(not ((3, 12) <= sys.version_info[:2] <= (3, 13)))' 2>/dev/null; then
  warn "Existing .venv uses an unsupported Python; recreating it."
  rm -rf .venv
fi
[[ -x .venv/bin/python ]] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[test]"

say "Running tests"
.venv/bin/python -m pytest -q

# --- Find the grill -------------------------------------------------------
say "Scanning for Pit Boss grills"
cat <<'MSG'
  Before continuing:
    - Turn the grill on.
    - Close the Pit Boss app on your phone (or turn off the phone's
      Bluetooth). The grill usually allows one Bluetooth connection, and
      if the app holds it the bridge can't connect.
MSG
read -r -p "  Press Enter to start a 15-second scan..."

mapfile -t grills < <(.venv/bin/python - <<'PY'
import asyncio
from bleak import BleakScanner

async def main():
    found = await BleakScanner.discover(timeout=15, return_adv=True)
    for device, adv in sorted(found.values(), key=lambda x: -x[1].rssi):
        name = adv.local_name or device.name or ""
        if name.startswith("PBL-"):
            print(f"{name}\t{adv.rssi}")

asyncio.run(main())
PY
)

DEVICE_NAME=""
if ((${#grills[@]} == 0)); then
  warn "No devices named PBL-... found."
  echo "  Check the grill is on, the phone app is closed, and the laptop is close by."
  read -r -p "  Enter the device name manually (or leave blank to quit): " DEVICE_NAME
  [[ -n "$DEVICE_NAME" ]] || die "No grill selected. Re-run once the grill is visible."
else
  echo "  Found (signal strength in dBm; closer to 0 is stronger, below -85 is weak):"
  for i in "${!grills[@]}"; do
    IFS=$'\t' read -r name rssi <<<"${grills[$i]}"
    printf '    %d) %s  (%s dBm)\n' "$((i + 1))" "$name" "$rssi"
  done
  choice=1
  if ((${#grills[@]} > 1)); then
    read -r -p "  Which one? [1] " choice
    choice="${choice:-1}"
  fi
  if ! [[ "$choice" =~ ^[0-9]+$ ]] || ((choice < 1 || choice > ${#grills[@]})); then
    die "Invalid choice: $choice"
  fi
  IFS=$'\t' read -r DEVICE_NAME rssi <<<"${grills[$((choice - 1))]}"
  if ((rssi < -85)); then
    warn "Signal is weak ($rssi dBm). Move the laptop closer to the grill for the test."
  fi
fi

read -r -s -p "  Grill password, if you set one in the Pit Boss app (Enter for none): " PASSWORD
echo

# --- Save settings --------------------------------------------------------
say "Saving settings to .env"
{
  echo "# Written by scripts/setup.sh"
  echo "PITBOSS_SOURCE=ble"
  echo "PITBOSS_MODEL=${PITBOSS_MODEL:-PB850PS2}"
  printf 'PITBOSS_DEVICE_NAME=%q\n' "$DEVICE_NAME"
  printf 'PITBOSS_PASSWORD=%q\n' "$PASSWORD"
  echo "SMOKESTACK_DB=data/smokestack.sqlite"
  echo "SMOKESTACK_LOG_LEVEL=DEBUG"
} >.env
chmod 600 .env
echo "  Grill: $DEVICE_NAME"

say "Setup complete. Start the bridge with:  ./scripts/run.sh"

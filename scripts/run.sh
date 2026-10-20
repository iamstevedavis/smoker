#!/usr/bin/env bash
# Start the bridge using the settings saved by scripts/setup.sh. Logs are
# written to logs/ and the laptop is kept awake while it runs.
set -euo pipefail

cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "No .env found. Run ./scripts/setup.sh first." >&2; exit 1; }
[[ -x .venv/bin/uvicorn ]] || { echo "No .venv found. Run ./scripts/setup.sh first." >&2; exit 1; }

set -a
# shellcheck disable=SC1091
. ./.env
set +a
export PYTHONUNBUFFERED=1
PORT="${SMOKESTACK_PORT:-8000}"

if (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "Something is already listening on port $PORT (is the bridge already running?)." >&2
  exit 1
fi

mkdir -p logs data
LOG="logs/bridge-$(date +%Y%m%d-%H%M%S).log"

cmd=(.venv/bin/uvicorn smokestack.api:create_app --factory --host 0.0.0.0 --port "$PORT")
# Stop the laptop suspending from inactivity mid-cook.
inhibit=(systemd-inhibit --what=sleep:idle --who="Smokestack" --why="Recording a grill test")
if command -v systemd-inhibit >/dev/null 2>&1 && "${inhibit[@]}" true 2>/dev/null; then
  cmd=("${inhibit[@]}" "${cmd[@]}")
  awake="Sleep is blocked while the bridge runs."
else
  awake="Could not block sleep -- disable suspend in Power Management for the test."
fi

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<MSG
Smokestack -> ${PITBOSS_DEVICE_NAME:-?} (source: ${PITBOSS_SOURCE:-fake})
  State:   http://${ip:-localhost}:$PORT/api/state
  Health:  http://${ip:-localhost}:$PORT/api/health
  Docs:    http://${ip:-localhost}:$PORT/docs
  Log:     $LOG
  $awake
  Keep the laptop plugged in with the lid open. Ctrl+C to stop.
  Afterwards run ./scripts/report.sh

MSG

"${cmd[@]}" 2>&1 | tee -a "$LOG"

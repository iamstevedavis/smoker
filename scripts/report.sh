#!/usr/bin/env bash
# Collect everything needed to review a grill test into one text file.
set -euo pipefail

cd "$(dirname "$0")/.."

[[ -x .venv/bin/python ]] || { echo "No .venv found. Run ./scripts/setup.sh first." >&2; exit 1; }
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
DB="${PITBOSS_DB:-data/pitboss.sqlite}"
PORT="${PITBOSS_PORT:-8000}"
[[ -f "$DB" ]] || { echo "No database at $DB -- has the bridge run yet?" >&2; exit 1; }

mkdir -p reports
OUT="reports/report-$(date +%Y%m%d-%H%M%S).txt"

{
  echo "# Pit Boss bridge test report ($(date -Is))"
  echo
  echo "## System"
  grep -h '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '"' || true
  uname -srm
  .venv/bin/python --version
  bluetoothctl --version 2>/dev/null || true
  .venv/bin/python -m pip show pytboss bleak 2>/dev/null | grep -E '^(Name|Version):' || true
  echo "Device: ${PITBOSS_DEVICE_NAME:-?}  Model: ${PITBOSS_MODEL:-?}  Source: ${PITBOSS_SOURCE:-?}"
  echo

  echo "## Live health (if the bridge is running)"
  curl -fsS "http://127.0.0.1:$PORT/api/health" 2>/dev/null && echo || echo "(bridge not running)"
  echo

  .venv/bin/python - "$DB" <<'PY'
import json
import sqlite3
import sys
from datetime import datetime

conn = sqlite3.connect(sys.argv[1])
conn.row_factory = sqlite3.Row

def ts(value):
    return datetime.fromisoformat(value)

rows = conn.execute(
    "SELECT observed_at, source, fields_json, unknown_json, raw_json"
    " FROM readings ORDER BY observed_at"
).fetchall()
print("## Readings")
print(f"Total: {len(rows)}")
if rows:
    first, last = ts(rows[0]["observed_at"]), ts(rows[-1]["observed_at"])
    span = (last - first).total_seconds()
    print(f"From {first.isoformat()} to {last.isoformat()} ({span / 60:.1f} min)")
    if len(rows) > 1:
        print(f"Average interval: {span / (len(rows) - 1):.1f}s")
    print(f"Sources: {sorted({r['source'] for r in rows})}")

    print("\n## Gaps longer than 10s between readings")
    gaps = []
    for prev, cur in zip(rows, rows[1:]):
        gap = (ts(cur["observed_at"]) - ts(prev["observed_at"])).total_seconds()
        if gap > 10:
            gaps.append((prev["observed_at"], gap))
    for at, gap in gaps:
        print(f"{at}  {gap:.0f}s")
    if not gaps:
        print("(none)")

    print("\n## Fields seen (count of readings with a non-empty value)")
    counts = {}
    for r in rows:
        for name, value in json.loads(r["fields_json"]).items():
            if value not in (None, ""):
                counts[name] = counts.get(name, 0) + 1
    for name in sorted(counts):
        print(f"{name}: {counts[name]}")

    unknown = set()
    for r in rows:
        unknown.update(json.loads(r["unknown_json"]))
    print(f"\nUnknown fields: {sorted(unknown) or '(none)'}")

    print("\n## Latest raw reading")
    print(json.dumps(json.loads(rows[-1]["raw_json"]), indent=2, sort_keys=True))

print("\n## Health events")
for e in conn.execute(
    "SELECT at, event_type, message, data_json FROM health_events ORDER BY at"
):
    print(f"{e['at']}  {e['event_type']:<18} {e['message']}  {e['data_json']}")
PY
  echo

  # shellcheck disable=SC2012  # log names are generated, no odd characters
  latest_log="$(ls -t logs/bridge-*.log 2>/dev/null | head -n1 || true)"
  if [[ -n "$latest_log" ]]; then
    echo "## Last 300 lines of $latest_log"
    tail -n 300 "$latest_log"
  else
    echo "## Logs"
    echo "(no logs/bridge-*.log found)"
  fi
} >"$OUT"

echo "Report written to $OUT"

# Pit Boss Bridge Milestone 1 Design

## Goal

Build a monitoring-only Pit Boss bridge that can be developed on this Windows/WSL machine and later moved to a Linux server near the grill for real BLE testing. The first milestone should make the app independent of the unreliable Pit Boss Wi-Fi/app path by collecting local state, storing history, and exposing a simple local API.

## Scope

Milestone 1 includes:

- A Python service with a FastAPI HTTP API.
- A normalized current-state cache where every value has a freshness timestamp.
- SQLite persistence for grill/probe history and collector health events.
- A fake grill data source for development without BLE access.
- A real `pytboss` BLE source stub/interface that can be completed and tested on Linux with BlueZ.
- Docker-ready project structure for eventual Linux server deployment.

Milestone 1 excludes:

- Grill write controls such as set temperature, shutdown, or primer control.
- Push notifications.
- Rich phone UI beyond API-ready data.
- Final BLE stability claims from Windows/WSL.

## Runtime Approach

The bridge runs as a single Python process. It starts a collector task alongside the FastAPI app. The collector reads from a configured source, normalizes the state, writes history rows, and updates an in-memory current-state snapshot.

For local development, the source is fake and emits realistic changing values. For server deployment, the source uses `pytboss` over BLE.

```text
Fake or pytboss source
  -> collector loop
  -> normalized state cache
  -> SQLite history
  -> FastAPI HTTP/WebSocket API
```

## API

The initial API should provide:

- `GET /api/health`: service status, source type, connected flag, last update time, last update age, reconnect count, and last error.
- `GET /api/state`: latest normalized grill state plus timestamps.
- `GET /api/history?since=...`: recorded grill and probe readings from SQLite.
- `WS /api/live`: live state stream for a future phone dashboard.

All responses should make stale data visible. The API must not quietly present an old reading as current.

## State Model

The normalized state should cover the fields known from `pytboss` for `PB850PS2`:

- Grill readings: `grillTemp`, `grillSetTemp`, `smokerActTemp`, `isFahrenheit`.
- Probe readings: `p1Temp`, `p1Target`, `p2Temp`.
- Power/status: `moduleIsOn`.
- Hardware activity: `fanState`, `hotState`, `motorState`, `primeState`, `lightState`.
- Alerts/errors: `err1`, `err2`, `err3`, `highTempErr`, `fanErr`, `hotErr`, `motorErr`, `noPellets`, `erL`.
- Recipe metadata if present: `recipeStep`, `recipeTime`.

The model should preserve unknown/raw data separately for diagnostics rather than dropping it.

## Persistence

SQLite should be the first persistence layer because it is portable, simple to back up, and enough for one grill. The database should store:

- Timestamped normalized readings.
- Optional raw source payloads for debugging.
- Collector health events such as connect, disconnect, reconnect, source error, and stale-data transitions.

The schema should be intentionally small in milestone 1. It can be expanded once real BLE data confirms what is useful.

## Error Handling

The collector should never crash permanently on a transient source error. It should:

- Record the error in health state and health events.
- Mark the current state stale when updates stop.
- Retry with backoff.
- Track reconnect count.

The API should keep serving health and last-known state even while the source is disconnected, with clear freshness metadata.

## Local Development

Because WSL2 does not currently expose Bluetooth on this machine, local development should use the fake source. Real BLE validation should happen later on a Linux server or Linux machine with BlueZ and a reliable Bluetooth adapter near the grill.

The code should still be structured so the source can be switched with configuration:

- `PITBOSS_SOURCE=fake`
- `PITBOSS_SOURCE=ble`

## Deployment Direction

The target deployment is a Linux host near the grill. Docker is the preferred packaging after the Python service works. The container should use the host Bluetooth stack where possible rather than owning Bluetooth itself.

The eventual deployment will likely need:

- host networking
- D-Bus mount for BlueZ
- a persistent `/data` volume for SQLite

## Verification

Milestone 1 is complete when:

- Automated tests cover state normalization, SQLite writes/reads, and fake collector behavior.
- The FastAPI app runs locally against the fake source.
- `GET /api/health`, `GET /api/state`, and `GET /api/history` return useful data.
- The WebSocket endpoint streams state updates.
- The repository contains Docker-ready files, even if real BLE is not yet validated here.

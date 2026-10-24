# API

Endpoints: `GET /api/health`, `GET /api/state`, `GET /api/history`, `WS /api/live`, `/api/cooks`, and `/api/control` for [grill control](#grill-control). Interactive docs are at `/docs`.

## Login

The password is stored as a scrypt hash in the database. Until it has been
created, every route except `/api/health`, `/api/session`, `/api/setup`, the
login page and static files answers 401 `{"detail": "setup required"}` (`/`
redirects to `/login`, which shows the setup form).

With the password on, the same routes need the `smoker_session` cookie: `/`
redirects to `/login`, other routes answer 401 and `WS /api/live` is refused
(close code 1008). With it turned off, everything is open. A wrong password
answers after a short delay.

```bash
curl localhost:8000/api/session
# {"setup_required": false, "auth_enabled": true, "authenticated": false}
curl -c jar -X POST localhost:8000/api/setup -H 'content-type: application/json' \
  -d '{"password": "..."}'                 # first run only (409 after); 204, signs in
curl -c jar -X POST localhost:8000/api/login -H 'content-type: application/json' \
  -d '{"password": "..."}'                 # 204, sets the cookie; 401 if wrong
curl -b jar localhost:8000/api/state
curl -b jar -X POST localhost:8000/api/logout
```

Managing it (behind the login when it is on; a wrong password answers 403):

- `PUT /api/auth` `{"required": false}` turns the password off.
  `{"required": true, "password": "..."}` turns it back on and signs the caller in.
- `POST /api/auth/password` `{"current": "...", "new": "..."}` changes it (at
  least 8 characters). Every other session is signed out.

Locked out? `python -m smokestack reset-password` (in Docker:
`docker compose exec smokestack smokestack reset-password`) forgets the
password; restart the bridge and the next visit shows the setup form again.

## History

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

## Settings

Bridge settings (not grill settings: nothing here writes to the grill). All
three endpoints sit behind the login.

- `GET /api/settings`: every editable field, grouped, with `value`, `origin`
  (`default`, `env` or `ui`) and `secret`. Secrets come back as
  `{"set": true|false}`, never the value. Also the `sources` this image
  supports and the `env_only` variables.
- `PUT /api/settings`: partial update, e.g. `{"poll_seconds": 5, "mqtt_host": "192.168.1.10"}`.
  Field names are the variable names without the `PITBOSS_` or `SMOKESTACK_`
  prefix, lower case. The whole update is validated first; any bad field
  returns 422 and nothing changes. Otherwise the values are saved and the source, MQTT and alerts are
  restarted with them. Returns the same shape as `GET`. An empty `mqtt_host`
  or `ntfy_url` turns that service off.
  Saving the grill's `password` here also ends a password change the bridge
  hasn't settled: your value wins, and the bridge uses it from then on.
- `DELETE /api/settings/{name}`: drop the saved value so the field falls back
  to the environment or default, and apply.

After applying, `GET /api/health` reports the new source, starting from
`connected: false` until its first reading.

## Probe alert targets

The temperatures at which each probe sends an ntfy alert. Behind the login.

- `GET /api/probe-targets`:
  `{"probe1": 203, "probe1_unit": "F", "probe2": null, "probe2_unit": null, "alerts_enabled": true}`.
  Each target keeps the unit (`"F"` or `"C"`) it was entered in and is
  converted to the unit of each reading it is checked against, so changing the
  grill's unit doesn't change what a target means. A unit of `null` next to a
  target means it was saved before units were recorded: it isn't checked until
  it is entered again. `alerts_enabled` is false while no ntfy URL is set;
  targets are kept either way.
- `PUT /api/probe-targets`: partial update, e.g. `{"probe1": 203}`; `null`
  clears a target. Values must be above 0 and at most 1000. New targets are
  saved in `unit` (`"F"` or `"C"`) when given, e.g.
  `{"probe1": 95, "unit": "C"}`, otherwise in the unit of the grill's latest
  reading; setting one without `unit` returns `409` while that unit is unknown
  (no reading yet, or one without `isFahrenheit`).
  Applied to the running alerts at once, without restarting the source.
  Returns the same shape as `GET`.

## Grill control

The only routes that write to the grill. They send turn-off, set-temperature,
a reboot of the grill's WiFi module, a switch of its unit or a change of the
grill's own password, and nothing else: there is no way to turn the grill
on. Off unless `SMOKESTACK_ENABLE_CONTROL` is on, and only available while
the dashboard login is on; every route needs a session. The design is
[docs/specs/2026-10-19-grill-control.md](specs/2026-10-19-grill-control.md)
and
[docs/specs/2026-10-20-grill-password-wifi.md](specs/2026-10-20-grill-password-wifi.md).

- `GET /api/control`:
  `{"enabled": true, "available": true, "reason": null, "commands": ["turn-off", "set-temperature", "reboot", "set-unit", "set-password"], "unit": "F", "setpoints": [180, 190, ...], "in_flight": null, "last_result": {...}, "pending_password_change": null}`.
  `available` is false, with a `reason`, while control is switched off, the
  login is off, or the source can't send commands. `setpoints` are what a
  set-temperature is snapped to, in the grill's current `unit`.
  `pending_password_change` is `null`, or `{"started_at": "..."}` while a
  password change the bridge hasn't settled yet is in flight.
- `POST /api/control/turn-off`
- `POST /api/control/set-temperature` `{"temp": 250, "unit": "F"}`, in the
  grill's current unit, snapped to the nearest setpoint the board accepts
  (ties go to the lower one). `unit` is optional but recommended: when it no
  longer matches the grill's unit (changed on the grill's panel since you
  looked), the request is refused with `409` instead of being read in the new
  unit. The dashboard always sends it.
- `POST /api/control/reboot`
- `POST /api/control/set-unit` `{"unit": "C"}` switches the unit the grill
  itself works in (its panel and every value it reports), only while it is
  off: `409` unless the latest reading has `moduleIsOn: false`. Confirmed
  when `isFahrenheit` changes; `already_applied` if it already matches.
  Probe alert targets and the drift threshold keep their meaning across a
  switch.
- `POST /api/control/set-password` `{"password": "...", "confirm": "...",
  "dashboard_password": "..."}` changes the grill's own password. The two
  passwords have to be the same (`422` otherwise), and the dashboard password
  is asked for again, because this is the one command whose failure can lock
  the bridge out (`403` if it is wrong).

Each POST must carry an `X-Smokestack-Control: 1` header, and is refused when
its `Origin` header doesn't match the `Host` it was sent to, so another site
can't send one with the dashboard's cookie:

```bash
curl -b jar -X POST localhost:8000/api/control/set-temperature \
  -H 'X-Smokestack-Control: 1' -H 'content-type: application/json' \
  -d '{"temp": 250, "unit": "F"}'
```

The bridge sends the command between polls, then waits for the grill to
report the change: 30 s, or 90 s for a reboot (the no-data alert is held
back meanwhile). A reboot is confirmed once the module is reporting again
after restarting: over BLE the connection drops and comes back; over `http`
the module can restart between two polls without any poll failing, so the
bridge also reads the module's uptime after a reboot and counts a reset as
the restart. The wait is longer
when the source polls slowly, since the grill's answer arrives one poll at a
time. The answer, `200`, is the result:

```json
{"command": "set-temperature", "args": {"temp": 245}, "target": 240, "requested": 245,
 "unit": "F", "outcome": "confirmed", "detail": "The grill is set to 240°F.",
 "sent": true, "attempts": 1, "started_at": "...", "finished_at": "...", "audit_id": 812}
```

`outcome` is one of:

- `confirmed`: the grill reported the change.
- `unconfirmed`: sent, but the grill didn't report the change in time. Check
  the grill; there is no automatic retry.
- `failed`: the grill or the connection refused it. An `Unauthorized` is
  retried once, for every command except a reboot. For a reboot, losing
  the connection after sending is expected and not a failure.
- `already_applied`: nothing was sent because the grill was already off, or
  already at that setpoint. A field missing from the latest reading never
  counts as already applied.

A password change is settled differently: what the command returned is only a
hint, because a reply lost after the grill applied the change looks like a
failure but isn't. So the bridge asks the grill which password it accepts
and keeps that one. `confirmed` means the grill now uses the new password
and the bridge saved it in its settings (so `.env` needs no change, and the
Pit Boss app needs the new password too), `failed` that the grill still uses
the old one, and `unconfirmed` that the bridge cannot tell: the change stays
pending, the dashboard says so, and the bridge keeps trying both passwords
until the grill answers. Saving the password in **Settings** ends it as
well. The passwords never appear in the answer, the log or the audit row.

Refusals send nothing and answer `{"detail": "...", "audit_id": 813}`:

| Status | Why |
| --- | --- |
| `401` | No valid session |
| `403` | Control is off, the login is off, the header is missing, a cross-site `Origin`, or (set-password) the dashboard password is wrong |
| `409` | Another command is still being confirmed; (set-temperature) the grill's unit isn't known yet or isn't the `unit` sent; (set-unit) the grill isn't off; (set-password) a password change is still in flight: save the password in Settings, or wait for it to settle |
| `422` | Bad body (`temp` must be above 0 and at most 1000; the two passwords must be the same) |
| `429` | Less than 5 s since the last command that may have reached the grill; see `Retry-After` |
| `503` | The grill isn't connected, its data is stale, or the source can't send commands |

Every attempt that reaches these routes, sent or refused, is recorded as a
`control_command` health event with the command, its arguments, the client
address and the outcome; `audit_id` is its id. Requests the login turns away
(401 before reaching the route) and malformed bodies (422) are not recorded.
`SMOKESTACK_RETENTION_DAYS` never deletes these events; only pruning the
command log does. A password change is recorded without its arguments: the
row says the command changed, never what it changed to.

### Command log

The `control_command` events, behind the login like the controls (`403` while
the login is off). `GET /api/control` reports their number as `log_count`.

- `GET /api/control-log?limit=100&before=<id>`: newest first.
  `{"count": 2, "total": 57, "next_before": 812, "items": [{"id": 813, "at": "...", "command": "set-temperature", "args": {"temp": 250, "unit": "F"}, "client": "192.168.1.20", "outcome": "rejected", "status_code": 429, "detail": "wait 3 s between commands"}, ...]}`.
  Each item is the result above plus `at` and `client`, or for a refusal
  `outcome: "rejected"` with its `status_code` and `detail`. Pass
  `next_before` as `before` for the next, older page; it is `null` on the last.
- `POST /api/control-log/prune` `{"older_than_days": 90}` deletes entries
  older than that; `{}` deletes them all. Add `"dry_run": true` to only
  count them: the answer, `{"matching": 12, "deleted": 0, "cutoff": "...", "max_id": 857}`,
  fixes the scope. Send `{"cutoff": "...", "max_id": 857}` (or just
  `max_id` for "all") to delete exactly the entries counted, not ones logged
  or aged past the cutoff in between; the dashboard always does. A delete
  answers `{"matching": 12, "deleted": 12, "total": 45}`. Needs
  the same `X-Smokestack-Control: 1` header and same-host `Origin` as the
  controls. Each prune is recorded as a `control_log_pruned` health event
  (outside the log itself).

## Health

`GET /api/health` reports the source state:

- `connected`: true only once a reading has arrived from the current session
- `reconnect_count`: sessions that recovered after a lost connection
- `consecutive_failures`: failed attempts since the last good reading
- `last_error` / `last_error_at`: most recent source error

After a source error the collector waits 5s, then doubles the wait on each
consecutive failure up to 60s, and resets after a good reading. Connects,
errors, and retries are also logged and recorded as health events in SQLite.

## Dashboard

The dashboard at `/` updates live over `WS /api/live`. Its only writes to the grill are the Controls section's commands (see [Grill control](#grill-control)), each confirmed in a dialog first; its settings panel edits bridge settings only. It warns when the bridge is unreachable, the grill is disconnected, or readings are older than `SMOKESTACK_STALE_AFTER_SECONDS`.

It works as an installable app (PWA), but browsers only offer install on `localhost` or HTTPS. Over plain `http://<host>:8000/` it works in the browser and iOS "Add to Home Screen" still gives a full-screen icon; Android/Chrome's install prompt needs HTTPS (for example behind a reverse proxy). The service worker caches only the page shell, never API data.

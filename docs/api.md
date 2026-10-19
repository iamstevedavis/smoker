# API

Endpoints: `GET /api/health`, `GET /api/state`, `GET /api/history`, `WS /api/live`, `/api/cooks`. Interactive docs are at `/docs`.

## Login

Only when `PITBOSS_UI_PASSWORD` is set. Every route except `/api/health`, the
login page and static files then needs the `smoker_session` cookie: `/` redirects
to `/login`, other routes answer 401 and `WS /api/live` is refused (close code
1008). A wrong password answers 401 after a short delay.

```bash
curl -c jar -X POST localhost:8000/api/login -H 'content-type: application/json' \
  -d '{"password": "..."}'                 # 204, sets the cookie
curl -b jar localhost:8000/api/state
curl -b jar -X POST localhost:8000/api/logout
curl localhost:8000/api/session          # {"auth_enabled": true|false}
```

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
three endpoints sit behind the login when `PITBOSS_UI_PASSWORD` is set.

- `GET /api/settings`: every editable field, grouped, with `value`, `origin`
  (`default`, `env` or `ui`) and `secret`. Secrets come back as
  `{"set": true|false}`, never the value. Also `editable`,
  `read_only_reason` (`no_login`, `disabled` or null), the `sources` this
  image supports and the `env_only` variables.
- `PUT /api/settings`: partial update, e.g. `{"poll_seconds": 5, "mqtt_host": "192.168.1.10"}`.
  Field names are the variable names without `PITBOSS_`, lower case. The
  whole update is validated first; any bad field returns 422 and nothing
  changes. Otherwise the values are saved and the source, MQTT and alerts are
  restarted with them. Returns the same shape as `GET`. `null` clears a probe
  target; an empty `mqtt_host` or `ntfy_url` turns that service off.
- `DELETE /api/settings/{name}`: drop the saved value so the field falls back
  to the environment or default, and apply.

`PUT` and `DELETE` return 403 when editing is off: by default without a UI
password, or always with `PITBOSS_SETTINGS_EDITABLE=false`.

After applying, `GET /api/health` reports the new source, starting from
`connected: false` until its first reading.

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

The dashboard at `/` updates live over `WS /api/live` and never writes to the grill; its settings panel edits bridge settings only. It warns when the bridge is unreachable, the grill is disconnected, or readings are older than `PITBOSS_STALE_AFTER_SECONDS`.

It works as an installable app (PWA), but browsers only offer install on `localhost` or HTTPS. Over plain `http://<host>:8000/` it works in the browser and iOS "Add to Home Screen" still gives a full-screen icon; Android/Chrome's install prompt needs HTTPS (for example behind a reverse proxy). The service worker caches only the page shell, never API data.

# Home Assistant and alerts

## Settings

Every setting below can also be changed from the dashboard's settings panel
and applied without a restart. Values saved there are stored in the data volume
and override `.env`; see [Changing settings from the dashboard](../README.md#changing-settings-from-the-dashboard).

- `PITBOSS_MQTT_HOST`, `PITBOSS_MQTT_PORT` (default `1883`), `PITBOSS_MQTT_USER`, `PITBOSS_MQTT_PASSWORD`, `PITBOSS_MQTT_PREFIX` (default `pitboss`): MQTT publishing, disabled when the host is unset
- `PITBOSS_NTFY_URL`, `PITBOSS_NTFY_TOKEN`: ntfy topic URL and optional access token; alerts are off when the URL is unset
- `PITBOSS_ALERT_PROBE1_TARGET`, `PITBOSS_ALERT_PROBE2_TARGET`: probe alert temperatures, in the grill's unit; unset disables that alert
- `PITBOSS_ALERT_DRIFT_DEGREES` (default `25`), `PITBOSS_ALERT_DRIFT_MINUTES` (default `10`): grill-vs-set-temp drift alert
- `PITBOSS_ALERT_STALE_SECONDS` (default `60`): seconds without data before a disconnect alert
- `PITBOSS_ALERT_COOLDOWN_SECONDS` (default `0`): minimum gap before the same alert can fire again

## Home Assistant (MQTT)

The bridge can publish to MQTT with Home Assistant discovery, so the grill shows
up as a device with no custom integration. It is read-only: there are no command
topics. Install the extra and point it at your broker:

```bash
python -m pip install -e ".[mqtt]"
PITBOSS_MQTT_HOST=192.168.1.10 PITBOSS_MQTT_USER=ha PITBOSS_MQTT_PASSWORD=... \
  uvicorn pitboss_bridge.api:create_app --factory
```

Home Assistant (with the MQTT integration and discovery enabled) creates a
**Pit Boss Grill** device with temperature sensors for the grill, set
temperature and both probes (unit follows the grill's `isFahrenheit`), binary
sensors for power, fan, igniter and auger, and a `Problem` sensor that turns on
when any error flag is set. Entities go unavailable when the source is
disconnected or stale (`pitboss/availability`, with a last-will of `offline`).

Topics, with the default prefix: `pitboss/state` (JSON of field values) and
`pitboss/availability`. The unreliable fields are not published. An unreachable
broker is retried with backoff and never affects `/api/*` or collection.

Example automation:

```yaml
automation:
  - alias: Brisket probe done
    trigger:
      - platform: numeric_state
        entity_id: sensor.pit_boss_grill_probe_1
        above: 200
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Probe 1 is at {{ states('sensor.pit_boss_grill_probe_1') }}"
```

To try it locally with a throwaway broker (the overlay also points the bridge
at it, and the image includes the `mqtt` extra):

```bash
docker compose -f docker-compose.yml -f docker-compose.mqtt.yml --profile mqtt up --build
```

## Alerts (ntfy)

The bridge can push notifications through [ntfy](https://ntfy.sh) with a plain
HTTP POST, so any phone with the ntfy app gets them. Set `PITBOSS_NTFY_URL` to
enable it:

```bash
# public ntfy.sh topic
PITBOSS_NTFY_URL=https://ntfy.sh/my-long-random-topic-name
# self-hosted, with an access token
PITBOSS_NTFY_URL=http://ntfy.lan/smoker PITBOSS_NTFY_TOKEN=tk_...
```

Subscribe to the same topic in the ntfy app. **A public topic name is effectively
a password**: anyone who guesses it can read your alerts, so pick a long random
one or self-host.

Alerts, each sent once per incident and re-armed when the condition clears:

- a probe reaches its target (`PITBOSS_ALERT_PROBE1_TARGET` / `..._PROBE2_TARGET`;
  the grill's own `p1Target` is not used). Re-arms 5 degrees below the target.
- the grill is more than 25 degrees from its set temperature for 10 minutes while on
- no data for 60 seconds, plus a "recovered" message
- an error flag turns on (`noPellets` is sent as urgent)

Sending happens in a background task with a timeout. A failed send is logged and
recorded as an `alert_error` health event; sent alerts are recorded as
`alert_sent`. The collector is never affected.

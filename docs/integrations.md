# Home Assistant and alerts

## Settings

Every setting below can also be changed from the dashboard's settings panel
and applied without a restart. Values saved there are stored in the data volume
and override `.env`; see [Changing settings from the dashboard](../README.md#changing-settings-from-the-dashboard).

- `SMOKESTACK_MQTT_HOST`, `SMOKESTACK_MQTT_PORT` (default `1883`), `SMOKESTACK_MQTT_USER`, `SMOKESTACK_MQTT_PASSWORD`, `SMOKESTACK_MQTT_PREFIX` (default `smokestack`): MQTT publishing, disabled when the host is unset
- `SMOKESTACK_NTFY_URL`, `SMOKESTACK_NTFY_TOKEN`: ntfy topic URL and optional access token; alerts are off when the URL is unset
- `SMOKESTACK_ALERT_DRIFT_DEGREES` (default `25`), `SMOKESTACK_ALERT_DRIFT_MINUTES` (default `10`): grill-vs-set-temp drift alert. The degrees are Fahrenheit degrees, scaled by 5/9 while the grill reports Celsius (25 °F is about 14 °C).
- `SMOKESTACK_ALERT_STALE_SECONDS` (default `60`): seconds without data before a disconnect alert
- `SMOKESTACK_ALERT_COOLDOWN_SECONDS` (default `0`): minimum gap before the same alert can fire again
- `SMOKESTACK_ENABLE_CONTROL` (default `false`): lets signed-in dashboard users turn the grill off, set its temperature, switch its unit while it is off and restart its WiFi module. Off by default; it also needs the dashboard login switched on. There is no way to turn the grill on. See [Grill control](#grill-control).
- `SMOKESTACK_BLE_ADAPTER` (default empty, the system default adapter): the Bluetooth adapter the `ble` source scans and connects with, e.g. `hci1`. The settings panel lists the host's adapters to pick from. See [Choosing the Bluetooth adapter](ble.md#choosing-the-bluetooth-adapter).

## Home Assistant (MQTT)

The bridge can publish to MQTT with Home Assistant discovery, so the grill shows
up as a device with no custom integration. It is read-only: there are no command
topics. Install the extra and point it at your broker:

```bash
python -m pip install -e ".[mqtt]"
SMOKESTACK_MQTT_HOST=192.168.1.10 SMOKESTACK_MQTT_USER=ha SMOKESTACK_MQTT_PASSWORD=... \
  uvicorn smokestack.api:create_app --factory
```

Home Assistant (with the MQTT integration and discovery enabled) creates a
**Pit Boss Grill** device with temperature sensors for the grill, set
temperature and both probes (unit follows the grill's `isFahrenheit`), binary
sensors for power, fan, igniter and auger, and a `Problem` sensor that turns on
when any error flag is set. Entities go unavailable when the source is
disconnected or stale (`smokestack/availability`, with a last-will of `offline`).

Topics, with the default prefix: `smokestack/state` (JSON of field values) and
`smokestack/availability`. The unreliable fields are not published. An unreachable
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

## Grill control

With `SMOKESTACK_ENABLE_CONTROL` on (in `.env`, or **Settings → Grill
control**), the dashboard gets a **Controls** section that can:

- turn the grill off (the fan keeps running for about 10 minutes, the normal
  shutdown cycle);
- set the grill temperature, snapped to the setpoints the board accepts
  (180-500 F on the PB850PS2), in the grill's current unit;
- switch the grill between °F and °C, only while it is off (the panel and
  every reading change unit; probe alerts keep working);
- restart the grill's WiFi module, which leaves the grill itself running;
- change the grill's own password, which the bridge then uses to talk to it.

There is no way to turn the grill on: the bridge never sends power-on or
ignition. Control also needs the dashboard login switched on, and each
action asks for confirmation. The bridge then waits for the grill to report
the change and shows whether it was confirmed, unconfirmed, failed or
already applied; an unconfirmed command means: go and check the grill. The
buttons are disabled while readings are stale. Commands go over the same
connection as the readings, so they work with the `http` and `ble` sources
alike (and the `fake` one).

A command goes out between two polls, and the grill's answer comes back in
the frames after it, so the bridge waits for as many polls as it needs. That
works at any **Poll every** setting, but a long interval also means a long
wait before the grill hears anything, and readings that are already stale
when the next poll is due: keep **Poll every** below **Stale after**, or the
controls stay disabled because the readings look old.

Changing the password asks for the dashboard password again, because it is
the one command whose failure can lock the bridge out. The grill and the
bridge both move to the new password, and the bridge saves it in its
settings, so nothing needs changing in `.env`; the Pit Boss app needs the
new password too. What settles the change is which password the grill
accepts afterwards, never what the command returned: a reply lost after the
grill applied the change looks like a failure but isn't. Both passwords are
kept until the change is settled, so a bridge that stops mid-change comes
back knowing both and reconnects on its own. If it still cannot tell, the
Controls card says so, and the bridge keeps trying both until the grill
answers; saving the password in **Settings** ends it as well. The passwords
never appear in the log or the command log.

Every attempt is kept in the database as a `control_command` health event,
and retention pruning leaves those alone. **Command log** on the Controls card
lists them, newest first, with the time, command, client address and outcome
(or why it was refused), and prunes them: all at once, or those older than a
number of days, after a confirmation that says how many will go. The card
stays visible while control is off as long as the log has entries. API details: [docs/api.md](api.md#grill-control).

## Alerts (ntfy)

The bridge can push notifications through [ntfy](https://ntfy.sh) with a plain
HTTP POST, so any phone with the ntfy app gets them. Set `SMOKESTACK_NTFY_URL` to
enable it:

```bash
# public ntfy.sh topic
SMOKESTACK_NTFY_URL=https://ntfy.sh/my-long-random-topic-name
# self-hosted, with an access token
SMOKESTACK_NTFY_URL=http://ntfy.lan/smoker SMOKESTACK_NTFY_TOKEN=tk_...
```

Subscribe to the same topic in the ntfy app. **A public topic name is effectively
a password**: anyone who guesses it can read your alerts, so pick a long random
one or self-host.

Alerts, each sent once per incident and re-armed when the condition clears:

- a probe reaches its alert target, set with the bell button on each probe's
  card on the dashboard (in the grill's unit; the grill's own `p1Target` is not
  used). Changing a target applies at once, without reconnecting to the grill.
  Re-arms 5 degrees below the target.
- the grill is more than 25 degrees from its set temperature for 10 minutes while on
- no data for 60 seconds, plus a "recovered" message (held back for up to 90
  seconds after a WiFi-module restart sent from the dashboard)
- an error flag turns on (`noPellets` is sent as urgent)

Sending happens in a background task with a timeout. A failed send is logged and
recorded as an `alert_error` health event; sent alerts are recorded as
`alert_sent`. The collector is never affected.

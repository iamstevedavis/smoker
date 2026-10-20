# Grill Control Design

Status: **proposed, awaiting owner approval** (issue #17). Nothing here is
implemented, and nothing may be until it is approved.

Revised 2026-10-20 for what has landed since it was written: dashboard login
(#36), settings saved from the dashboard, retention pruning, and bridge-side
probe alert targets (#37). Revised again on 2026-10-20 with the owner's
answers to the open decisions (see [Decisions](#decisions)).

## Goal

Let the bridge, and later the dashboard, send a small set of commands to the
grill so it can replace the vendor app, without ever making it easier to start
or run a fire unattended. The grill is a pellet fire that reaches 500 F; every
decision below favours "do less" over "do more".

## Scope

In scope, all in the first release:

1. **Turn off** (lowest risk: it only ever reduces heat).
2. **Set grill temperature**, snapped to values the board accepts.
3. **Set temperature unit** (F or C), only while the grill is off.
4. **Reboot the WiFi module**, which leaves the grill itself running.

Out of scope, deliberately:

- **Power on / ignition.** `pytboss` can send it (`turn_grill_on`, raw
  `FE0101FF`), but its own docs note it "starts a fire in an appliance nobody
  may be standing next to", and no board in the vendor catalogue declares a
  turn-on command. The bridge will not expose it. Reversing this is the owner's
  call, not an implementation detail.
- **Grill password and WiFi changes.** Deferred, not rejected; tracked in its
  own issue (#54). A wrong WiFi network or password cuts the grill off from the
  bridge, which then cannot undo the change.
- Writing probe targets to the grill (`p1Target` is unreliable, see
  `docs/fields.md`). The probe alert targets from #37 are stored in the bridge
  and never sent to the grill, so they are unaffected. Primer motor, light
  (the PB850PS2 has none).

## What pytboss supports for the PB850PS2

The model uses the `PBL` control board, two probes, setpoints 180-500 F.
Commands relevant here (checked against pytboss 2026.8.6):

| Action | pytboss call | Command | Notes |
| --- | --- | --- | --- |
| Turn off | `turn_grill_off()` | MCU `turn-off` `FE0102FF` | Worked first try in the 2026-10-18 test; the fan runs about 10 min afterwards (normal shutdown cycle) |
| Set temperature | `set_grill_temperature(temp)` | MCU `set-temperature` | Snapped to the nearest of `180/190/.../300/325/350/375/400/450/500`, in the grill's current unit |
| Set unit | `set_temperature_unit(fahrenheit)` | MCU `set-fahrenheit` `FE0901FF` / `set-celsius` `FE0902FF` | The grill's own unit, shown on its panel and used by every value it reports |
| Reboot WiFi module | `reboot()` | RPC `Sys.Reboot` | No answer: the module restarts before it can reply. The grill keeps running |

Every MCU command is an authenticated `PB.SendMCUCommand`. After one, the
firmware clears its status cache, so the next state frame comes back blank.

## Safety

- **Off by default.** `SMOKESTACK_ENABLE_CONTROL` (default off) is an ordinary
  setting: it can be set in `.env` or switched on from the dashboard's
  settings, in a `control` group. While it is off, the control routes answer
  403 and no source command method is ever called; a test asserts both.
- **Logged in only.** Control needs a valid dashboard session (#36). If
  dashboard login is switched off (`PUT /api/auth`), control is unavailable
  even when enabled: the dashboard is read-only to anyone who is not logged in.
  There is no separate control token.
- **No cross-site requests.** The session cookie is `HttpOnly` and
  `SameSite=Strict`, but that alone still lets another service on the same
  site (a sibling subdomain, another port on the same host) send it, and the
  bodyless `turn-off` and `reboot` POSTs would be simple requests with no CORS
  preflight. So every control POST must also carry an
  `X-Smokestack-Control: 1` header, which makes any cross-origin request need
  a preflight the bridge never grants (it has no CORS middleware), and is
  rejected with 403 when its `Origin` header is present and does not match
  the request's own `Host`. The dashboard sends the header; tests cover a
  missing header and a foreign `Origin`.
- **Temperature.** No bounds beyond the board's own: a request is in the
  grill's current unit (as `pytboss` expects), is snapped to the board's
  accepted setpoints, and the snapped value is reported back. If the grill's
  unit is unknown, set-temperature is rejected.
- **Unit only while off.** Set-unit is accepted only when the latest fresh
  snapshot has `moduleIsOn` explicitly `false`; `true` or a missing field
  rejects it, since a snapshot can carry any subset of fields. A running cook
  never switches unit part way.
- **Unit-safe alert thresholds.** Today the bridge's probe alert targets
  (#37) and the drift alert threshold are plain numbers in whatever unit the
  grill reports, and they survive between cooks: a 200 F probe target would
  be compared with Celsius readings after a switch. Converting them when a
  set-unit is confirmed is not enough, because a switch that the grill
  applies but the bridge reports as `unconfirmed` (or one made at the grill's
  panel or in the vendor app) would leave them wrong with nothing to fix
  them. So they stop depending on the grill's unit at all:
  - Each probe target is stored with the unit it was entered in (a `unit`
    column on `probe_targets`). Existing rows were entered in whatever unit
    the grill reported, so the migration takes their unit from the latest
    stored reading with a known `isFahrenheit`. With no such reading, the
    unit stays empty and is filled in from the first reading that has one;
    until then the target is compared in the reading's own unit, as today.
    A new target is saved in the unit of the latest reading, and saving is
    rejected while that unit is unknown.
  - The drift threshold is defined in Fahrenheit degrees and scaled by 5/9
    when comparing Celsius readings.
  - The alert engine converts each threshold to the unit of the reading it is
    comparing against (`isFahrenheit` on that snapshot), every time. Nothing is
    rewritten when the unit changes, however it changes. A reading with no
    known unit is not compared.

  For a grill already in Celsius, this changes what an existing drift setting
  means (25 becomes about 14 C), so the implementation PR carries the
  `breaking` label and says so. Stored readings keep `isFahrenheit` with each
  snapshot, so history stays readable and is not rewritten. Tests cover a
  target saved in F and checked against C readings and the reverse, a unit
  switch with no command at all, and the migration.
- **Only against a live, fresh source.** Rejected with 503 when the source is
  disconnected or data is stale, because the confirmation step needs a live view.
- **Rate limiting.** One command in flight at a time (409 otherwise) and a
  minimum gap of 5 s between commands (429).
- **Audit log.** Every attempt that reaches the control routes, accepted or
  rejected, is a `health_events` row of type `control_command` with command,
  arguments, client address, and outcome. Requests the login gate rejects for
  a missing or expired session never reach the routes and are not audited, the
  same as for every other route. The log is append-only from the control API's
  point of view, and retention pruning (`SMOKESTACK_RETENTION_DAYS`) skips
  `control_command` rows, so the record of what was sent to the grill is never
  deleted automatically. Viewing the log and pruning it by hand from the
  dashboard is a separate issue (#55).
- **No remote ignition**, as above. A request that would require the grill to be
  off-to-on is not representable in the API.

## Confirming a command

A command is not "done" because the RPC returned. Flow:

1. Validate (session, enabled, snapping, unit-while-off, rate limit, source
   health), then take the command lock.
2. **Idempotence check** against the current state: `turn-off` when
   `moduleIsOn` is already false, `set-temperature` when `grillSetTemp` already
   equals the snapped target, or `set-unit` when `isFahrenheit` already matches,
   returns `already_applied` without sending anything. These commands are
   absolute ("be in this state"), not relative, so a repeat is safe. `reboot`
   has no target state and is always sent. A missing field never counts as
   already applied: the command is sent (or, for set-unit, rejected as
   above).
3. Send via the source's own connection (see Source support).
4. **Ignore the blank frame.** A state frame with no known fields right after an
   MCU command is the firmware cache reset, not a reading: it must not be stored
   or published as one, and it must not count as confirmation or failure. This
   needs a small change to the collector, which today would persist an empty
   snapshot.
5. Poll for up to 30 s (about 4 BLE polls) until the expected state appears
   (`moduleIsOn == false`, `grillSetTemp == target`, or `isFahrenheit ==
   requested`). For `reboot`, wait up to 90 s for the source to drop and
   reconnect and for a fresh frame to arrive; the stale-data alert is held
   back for that window so a deliberate reboot does not page anyone.
6. Result, returned and stored as `last_result`:
   - `confirmed`: expected state observed.
   - `unconfirmed`: command sent, state did not change in time. The UI says so
     plainly and tells the user to check the grill. No automatic retry.
   - `failed`: the RPC raised (timeout, Unauthorized, disconnect). One retry is
     allowed only for `Unauthorized`, and only for the idempotent commands.
     For `reboot`, a disconnect after sending is expected, not a failure.
   - `already_applied`: nothing sent.

## Source support

**BLE, `http` and `fake`, together.** Both real sources drive the same
`PitBoss` object, so the commands are identical. The source, not the API layer,
owns the connection, and BLE allows one connection at a time. So commands go
through a small `asyncio.Queue` that the source's polling loop services between
polls; there is never a concurrent GATT or HTTP call. Sources gain an optional
`execute(command)` method; sources without it (or with control disabled) report
control as unavailable. BLE's `Unauthorized` flakiness (#9) is covered by the
single retry above.

## API

Always registered; every route needs a logged-in session:

- `GET /api/control`: `{enabled, available, reason, commands, last_result}`.
  `available` is false, with a `reason`, when control is disabled, login is
  switched off, or the source does not support commands.
- `POST /api/control/turn-off`
- `POST /api/control/set-temperature` with `{"temp": 225}`
- `POST /api/control/set-unit` with `{"unit": "F"}` or `{"unit": "C"}`
- `POST /api/control/reboot`

Each POST returns the result object above plus the audit event id.

## UI

The dashboard shows a **Controls** section when `/api/control` reports control
enabled, and explains `reason` when it is not available. Each action opens a
confirmation dialog stating exactly what will happen ("Turn the grill off. The
fan will run for about 10 minutes."; "Set grill to 250 F, currently 225 F",
showing the snapped value when it differs from what was typed; "Switch the
grill to Celsius"; "Restart the grill's WiFi module. The grill keeps running;
the dashboard goes blank for up to a minute."). After sending, the panel shows
the last command, its time, and its result until replaced, with a spinner
while confirming. Every result state has its own wording: `confirmed`,
`unconfirmed`, `failed`, and `already_applied` ("The grill was already off" /
"already set to 250 F"; nothing was sent), which also clears the spinner. The
control buttons are disabled while the source is stale, and the unit buttons
while the grill is on.

## Testing without a live fire

- The **fake source implements the commands**: `turn-off` flips `moduleIsOn`
  and winds the simulated temperature down, `set-temperature` updates
  `grillSetTemp`, `set-unit` flips `isFahrenheit` and converts its values,
  `reboot` drops the stream briefly and reconnects. It also supports failure
  injection (RPC error, ignored command so the state never changes, blank frame
  after a command, `Unauthorized` once, a reboot that never comes back).
- Unit tests cover the session requirement (including login switched off),
  the enable setting and its save round-trip, snapping (including a Celsius
  grill), set-unit rejection while on or with `moduleIsOn` missing, idempotence
  with missing fields, rate limiting, the command lock,
  idempotence, each result state (in the API and in the dashboard, including
  `already_applied`), the blank-frame handling, the held-back stale alert
  during a reboot, the retention exemption, and the audit rows, using
  `FakeBoss` objects in the style of `tests/test_ble_poll_state.py`.
- CI never talks to a real grill. The one hardware step is a manual
  checklist, run by the owner standing at the grill, over both `http` and BLE:
  `turn-off` first, then a single `set-temperature`, then `set-unit` both ways
  with the grill off, then `reboot`, comparing the grill with each result.

## Decisions

Answered by the owner on 2026-10-20:

1. No configurable temperature bounds; the board's 180-500 F range applies.
2. BLE, `http` and `fake` ship together.
3. Remote power-on stays excluded.
4. No PIN or separate token: a logged-in session plus the confirmation dialog.
5. Control requires dashboard login to be on, and the enable setting is
   editable from the dashboard rather than environment-only.
6. `control_command` audit rows are kept until pruned by hand, from a
   dashboard view tracked in #55.
7. Set-unit and WiFi-module reboot are in scope; grill password and WiFi
   changes are deferred to #54. The hourly rate limit is dropped.

## Follow-up implementation issue (to open after approval)

Title: "Implement grill control (turn-off, set-temperature, set-unit, reboot)
per `docs/specs/2026-10-19-grill-control.md`". Tasks: the `enable_control`
setting (env, `EDITABLE_GROUPS`, `overrides.SettingsUpdate`, dashboard field,
docs); `execute` on sources and the command queue; blank-frame handling in the
collector; control service (validation, rate limit, idempotence, confirmation,
audit); the control header and `Origin` check; unit-safe alert thresholds
(probe targets stored with their unit, drift threshold in F); retention
exemption for audit rows; `/api/control` routes; fake-source
commands with failure injection; dashboard Controls section; README and tests.
It is not opened yet because the design is not approved.

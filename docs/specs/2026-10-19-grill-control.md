# Grill Control Design

Status: **proposed, awaiting owner approval** (issue #17). Nothing here is
implemented, and nothing may be until it is approved.

## Goal

Let the bridge, and later the dashboard, send a small set of commands to the
grill so it can replace the vendor app, without ever making it easier to start
or run a fire unattended. The grill is a pellet fire that reaches 500 F; every
decision below favours "do less" over "do more".

## Scope

In scope, in order of rollout:

1. **Turn off** (lowest risk: it only ever reduces heat).
2. **Set grill temperature**, bounded and snapped to values the board accepts.

Out of scope, deliberately:

- **Power on / ignition.** `pytboss` can send it (`turn_grill_on`, raw
  `FE0101FF`), but its own docs note it "starts a fire in an appliance nobody
  may be standing next to", and no board in the vendor catalogue declares a
  turn-on command. The bridge will not expose it. Reversing this is the owner's
  call, not an implementation detail.
- Probe targets (`p1Target` is unreliable, see `docs/fields.md`), temperature
  unit, primer motor, light (the PB850PS2 has none), password and WiFi changes,
  reboot.

## What pytboss supports for the PB850PS2

The model uses the `PBL` control board, two probes, setpoints 180-500 F. Board
commands relevant here:

| Action | pytboss call | MCU command | Notes |
| --- | --- | --- | --- |
| Turn off | `turn_grill_off()` | `turn-off` `FE0102FF` | Worked first try in the 2026-10-18 test; the fan runs about 10 min afterwards (normal shutdown cycle) |
| Set temperature | `set_grill_temperature(temp)` | `set-temperature` | Snapped to the nearest of `180/190/.../300/325/350/375/400/450/500`, in the grill's current unit |

Every command is an authenticated `PB.SendMCUCommand`. After one, the firmware
clears its status cache, so the next state frame comes back blank.

## Safety

- **Off by default.** Routes are not registered unless `SMOKESTACK_ENABLE_CONTROL=1`,
  so the existing "no write endpoints" test stays true by default. When enabled,
  that test is relaxed to allow exactly `/api/control/*`.
- **Separate auth.** Control requires `Authorization: Bearer <SMOKESTACK_CONTROL_TOKEN>`
  even if the read API is open. If control is enabled and the token is empty or
  shorter than 16 characters, the bridge refuses to start. Compared in constant
  time; the token is never logged.
- **Bounds.** `SMOKESTACK_CONTROL_MIN_TEMP` (default 180) and
  `SMOKESTACK_CONTROL_MAX_TEMP` (default **300**, hard-capped at the model's 500)
  are always in **Fahrenheit**, whatever unit the grill is set to. A request is
  in the grill's current unit (as `pytboss` expects), so it is first snapped to
  the board's accepted setpoints, then the *snapped* target is converted to F
  (using `isFahrenheit`) and compared with the bounds. Checking after snapping
  means a bound that is not itself a setpoint (say 320) can never let a request
  through that snaps past it (to 325); converting means a Celsius grill cannot
  use 260 to get 500 F under a 300 cap. Out-of-bounds requests are rejected,
  not clamped, and the snapped value is reported back. If the grill's unit is
  unknown, set-temperature is rejected. Raising the max is an explicit owner
  decision.
- **Only against a live, fresh source.** Rejected with 503 when the source is
  disconnected or data is stale, because the confirmation step needs a live view.
- **Rate limiting.** One command in flight at a time (409 otherwise), a minimum
  gap of 5 s, and at most 10 commands per hour (429).
- **Audit log.** Every attempt, accepted or rejected, is a `health_events` row
  of type `control_command` with command, arguments, client address, and
  outcome. The log is append-only from the API's point of view.
- **No remote ignition**, as above. A request that would require the grill to be
  off-to-on is not representable in the API.

## Confirming a command

A command is not "done" because the RPC returned. Flow:

1. Validate (auth, snap then bounds, rate limit, source health), then take the
   command lock.
2. **Idempotence check** against the current state: `turn-off` when `moduleIsOn`
   is already false, or `set-temperature` when `grillSetTemp` already equals the
   snapped target, returns `already_applied` without sending anything. Both
   commands are absolute ("be in this state"), not relative, so a repeat is safe.
3. Send via the source's own connection (see Source support).
4. **Ignore the blank frame.** A state frame with no known fields right after an
   MCU command is the firmware cache reset, not a reading: it must not be stored
   or published as one, and it must not count as confirmation or failure. This
   needs a small change to the collector, which today would persist an empty
   snapshot.
5. Poll for up to 30 s (about 4 BLE polls) until the expected state appears
   (`moduleIsOn == false`, or `grillSetTemp == target`).
6. Result, returned and stored as `last_result`:
   - `confirmed`: expected state observed.
   - `unconfirmed`: command sent, state did not change in time. The UI says so
     plainly and tells the user to check the grill. No automatic retry.
   - `failed`: the RPC raised (timeout, Unauthorized, disconnect). One retry is
     allowed only for `Unauthorized`, which is safe because commands are
     idempotent.
   - `already_applied`: nothing sent.

## Source support

**BLE and `http` both, plus `fake`.** Both real sources drive the same `PitBoss`
object, so the commands are identical. The source, not the API layer, owns the
connection, and BLE allows one connection at a time. So commands go through a
small `asyncio.Queue` that the source's polling loop services between polls;
there is never a concurrent GATT or HTTP call. Sources gain an optional
`execute(command)` method; sources without it (or with control disabled) report
control as unavailable. If the owner would rather limit the first release,
`http` plus `fake` is the lower-risk subset, since it avoids BLE's
`Unauthorized` flakiness (#9).

## API

Registered only when control is enabled:

- `GET /api/control`: `{enabled, commands, min_temp, max_temp, last_result}`
  (needs the token too, so the UI can discover capabilities).
- `POST /api/control/turn-off`
- `POST /api/control/set-temperature` with `{"temp": 225}`

Each POST returns the result object above plus the audit event id.

## UI

The dashboard shows a **Controls** section only when `/api/control` reports it
enabled. The token is typed by the user and kept in `sessionStorage`, never in
the page or URL. Each action opens a confirmation dialog stating exactly what
will happen ("Turn the grill off. The fan will run for about 10 minutes."; "Set
grill to 250 F, currently 225 F"). After sending, the panel shows the last
command, its time, and its result until replaced, with a spinner while
confirming. Every result state has its own wording: `confirmed`, `unconfirmed`,
`failed`, and `already_applied` ("The grill was already off" / "already set to
250 F"; nothing was sent), which also clears the spinner. The control buttons are disabled
while the source is stale.

## Testing without a live fire

- The **fake source implements the commands**: `turn-off` flips `moduleIsOn`
  and winds the simulated temperature down, `set-temperature` updates
  `grillSetTemp`. It also supports failure injection (RPC error, ignored command
  so the state never changes, blank frame after a command, `Unauthorized` once).
- Unit tests cover auth, bounds and snapping (including a non-setpoint max and
  a Celsius grill), rate limiting, the command lock, idempotence, each result
  state (in the API and in the dashboard, including `already_applied`), the blank-frame handling, and the audit rows,
  using `FakeBoss` objects in the style of `tests/test_ble_poll_state.py`.
- CI never talks to a real grill. The one hardware step is a manual
  checklist, run by the owner standing at the grill: `turn-off` first, then a
  single `set-temperature`, comparing the grill display with `confirmed`.

## Open decisions for the owner

1. Is `SMOKESTACK_CONTROL_MAX_TEMP` default 300 right, or should it start higher?
2. Roll out BLE and `http` together, or `http` + `fake` first?
3. Confirm remote power-on stays excluded.
4. Is a shared bearer token enough, or should the UI require a second step such
   as a PIN for set-temperature?

## Follow-up implementation issue (to open after approval)

Title: "Implement grill control (turn-off, set-temperature) per
`docs/specs/2026-10-19-grill-control.md`". Tasks: settings and startup checks;
`execute` on sources and the command queue; blank-frame handling in the
collector; control service (validation, rate limit, idempotence, confirmation,
audit); `/api/control` routes; fake-source commands with failure injection;
dashboard Controls section; README and tests. It is not opened yet because the
design is not approved.

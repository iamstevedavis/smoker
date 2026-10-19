# State fields

Fields exposed under `fields` in `GET /api/state`, the websocket and
`GET /api/history`, as reported by the `PB850PS2`. Everything the grill sends is
always kept verbatim in `raw`.

Reliability: **ok** is trustworthy, **hidden** is left out of `fields` for this
model (see `UNUSABLE_FIELDS` / `UNSET_DEFAULTS` in `state.py`), **flag** is a
boolean status.

| Field | Unit / type | Meaning | Reliability |
| --- | --- | --- | --- |
| `grillTemp` | degrees (see `isFahrenheit`) | Current grill chamber temperature | ok |
| `grillSetTemp` | degrees | Target grill temperature | ok |
| `smokerActTemp` | degrees | Never populated by the PB850PS2 | hidden |
| `isFahrenheit` | bool | Temperature unit for all temperature fields | ok |
| `p1Temp`, `p2Temp` | degrees | Meat probe 1 and 2; `null` when no probe is plugged in | ok |
| `p1Target` | degrees | Probe 1 target. Firmware default `50` means unset | hidden when `50`, otherwise ok |
| `p2Target` | degrees | Probe 2 target | ok |
| `p3Temp`, `p4Temp` | degrees | The model has two probe ports; always `null` | hidden |
| `moduleIsOn` | flag | Controller is running | ok |
| `fanState`, `hotState`, `motorState` | flag | Fan, igniter (hot rod) and auger motor currently on | ok |
| `primeState` | flag | Auger priming | ok |
| `lightState` | flag | Hopper light | ok |
| `err1`, `err2`, `err3` | flag | Controller error flags | ok |
| `highTempErr`, `fanErr`, `hotErr`, `motorErr` | flag | High temperature, fan, igniter and auger faults | ok |
| `noPellets` | flag | Hopper is out of pellets | ok |
| `erL` | flag | Low temperature error | ok |
| `recipeStep`, `recipeTime` | number | Built-in recipe progress | ok |

## Changes for API consumers

For the `PB850PS2`, `smokerActTemp`, `p3Temp`, `p4Temp` and a `p1Target` equal
to `50` no longer appear in `fields` (and so not in stored history rows'
`fields`); they remain in `raw`. Other models are unaffected.

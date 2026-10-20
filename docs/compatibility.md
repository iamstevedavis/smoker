# Does my grill work?

## Tested

| Model | Control board | WiFi (`http`) | Bluetooth (`ble`) |
| --- | --- | --- | --- |
| `PB850PS2` | `PBL` | ✅ | ✅ |

Tested with Mongoose OS 2.19.1 and PB firmware 0.5.7: turning the grill on,
heating, both probes, changing the set temperature on the grill or in the
Pit Boss app, and turning it off. Notes from the first cook are in
[testing/2026-10-18-cook-test.md](testing/2026-10-18-cook-test.md).

## Probably works, untested

Smokestack reads the grill through [pytboss](https://github.com/dknowles2/pytboss),
which knows how to read 137 Pit Boss and Louisiana Grills models on 17 control
boards (pytboss 2026.8.6). Any of them may work: set `PITBOSS_MODEL` to its
name, spelled exactly as pytboss lists it. To print the list:

```bash
docker run --rm --entrypoint python ghcr.io/iamstevedavis/smokestack \
  -c "from pytboss import grills; print('\n'.join(sorted({g.name for g in grills.get_grills()})))"
```

A name pytboss doesn't know shows up in `/api/health` as
`"last_error": "Unknown grill name: ..."`.

Some fields the `PB850PS2` never fills in (a third and fourth probe, smoker
temperature) are hidden for that model only ([fields.md](fields.md)). On
another model you may see readings that stay empty or never change.

## WiFi or Bluetooth?

Try WiFi first: it needs no Bluetooth adapter and the bridge can run anywhere
on your network. It only works if the grill's board serves local HTTP:

- Boards running Mongoose OS do once `http.enable` is set, as on the tested
  `PB850PS2`.
- ESP-IDF boards (such as `PBL2` and `PBC2`) never do.

If WiFi can't connect, `/api/health` shows a `last_error` saying the grill may
not serve local HTTP. A wrong `PITBOSS_HOST` gives the same error, so check
the IP first. If the IP is right, switch to [Bluetooth](ble.md).

Over Bluetooth, the grill advertises as its control board followed by a
suffix (for example `PBL-...`), which also tells you the board.

## Tell us how it went

Whether your grill works or not, please open a
[grill report](https://github.com/iamstevedavis/smokestack/issues/new?template=grill-report.yml)
with the model, board, source and what you saw. Working models get added to
the table above.

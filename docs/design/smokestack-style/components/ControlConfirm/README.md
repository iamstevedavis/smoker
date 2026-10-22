# ControlConfirm

The confirmation shown before any command is sent to the grill.

- The title asks the question ("Turn the grill off?"). The body says what will happen, in the words the app uses.
- **Cancel** is `btn-secondary`. The command is `btn-danger` for turning off and `btn-primary` for the others (set temperature, restart WiFi, switch unit).
- Every grill command goes through this dialog. Only the approved commands exist (see the grill control spec in the repo).

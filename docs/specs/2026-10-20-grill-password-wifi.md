# Grill Password and WiFi Design

Status: **proposed**, not approved (issue #54). Nothing here may be built
until the owner approves it; until then AGENTS.md keeps these commands off
the approved list.

## Goal

Let Smokestack replace the vendor app for setup too: change the grill's own
password, and move the grill to another WiFi network. Both are rare,
deliberate operations, and both can cut the bridge off from the grill. So
the design is built around one question: **if this goes wrong, how does the
grill come back without anyone touching it?**

## Scope

In scope, if approved:

1. **Change the grill password** (`PB.SetDevicePassword`), and the bridge's
   `PITBOSS_PASSWORD` to match, in one step.
2. **Scan for WiFi networks** (`PBL.StartWifiScan` / `PBL.GetWifiScanStatus`
   via `scan_wifi_networks()`). A read: it changes nothing on the grill.
3. **Move the grill to another WiFi network** (`PB.SetWifiCredentials`, then
   `Config.Save`), **over Bluetooth only**.

Out of scope: everything else in `Config.Set` (it can rewrite any firmware
setting), enterprise WiFi, power-on (still never).

## What pytboss offers (2026.8.6)

| Action | pytboss call | RPC | Notes |
| --- | --- | --- | --- |
| Change password | `set_grill_password(new)` | `PB.SetDevicePassword` | Authenticated with the current password; pytboss switches its own key to the new one on success |
| Scan | `scan_wifi_networks()` | `PBL.StartWifiScan`, `PBL.GetWifiScanStatus` | Served by the loader, unauthenticated. Reading the results consumes them, so only one caller may poll |
| Set WiFi | `set_wifi_credentials(ssid, pw)` | `PB.SetWifiCredentials` | Password obfuscated in transit. **Writes the config without saving it**: a module restart goes back to the saved network |
| Save | `Config.save_config(reboot)` | `Config.Save` | Commits the config to flash, optionally restarting the module |
| Restart module | `reboot()` | `Sys.Reboot` | Already approved (#59) |

## The key idea: try, then commit

`PB.SetWifiCredentials` doesn't commit. That gives a WiFi change an undo
that needs nobody at the grill:

1. Set the new credentials, **not saved**.
2. Watch, over Bluetooth, whether the module joins **the new network** and
   gets an address. Seeing the old network still connected proves nothing:
   the switch may not have started yet (see the success test below).
3. Joined: `Config.Save`, so it survives restarts. Not joined within the
   window (60 s): **restart the module** (`Sys.Reboot`, already approved),
   which loads the saved config, the old network, again.

This only works if the bridge can still talk to the grill while its WiFi is
being changed, which is why WiFi changes are **Bluetooth only**: over `http`
the bridge's own connection is the thing being changed, so it would see
nothing and could undo nothing.

## Changing the grill password

Works over both `http` and Bluetooth: the connection stays up; only the key
changes.

1. Re-enter the **dashboard** password (as for changing it) and the new
   grill password twice. The grill password must be non-empty; the vendor
   app's limits apply (to be checked on hardware, see Unknowns).
2. Under the command lock, with fresh data, send `PB.SetDevicePassword`.
   **Whatever the send returns is only a hint.** A reply lost after the
   grill applied the change (a timeout, a dropped connection) looks like a
   failure but isn't, so no outcome of the send decides which password the
   bridge keeps.
3. Before anything is decided, keep both passwords in memory and save the
   new one as the `password` setting (a UI override, so it wins over
   `.env`), marked *pending*, so a bridge restart mid-way still knows both.
4. **Reconcile by authenticating**, whatever step 2 returned: an
   authenticated poll with the new password, then, if that is
   `Unauthorized`, with the old one (each once, with the usual uptime
   refresh).
   - New works: keep it, clear *pending*, report `confirmed` (even if the
     send raised).
   - Only old works: the change didn't take; restore the old setting and
     report `failed`.
   - Neither works, or the grill can't be reached: keep the new one saved
     and still *pending*, report `unconfirmed`, and show a banner saying how
     to recover (set the password in Settings, or reset it at the grill).
     While *pending*, an `Unauthorized` poll retries with the other
     password, so whichever the grill ends up with, the bridge reconnects
     without anyone touching it.
5. The password never appears in logs, the audit row or API responses; the
   audit row records only that it changed.

## Moving the grill to another network

Bluetooth source only; the dashboard hides it otherwise and the API answers
409.

1. **Scan** from the dashboard: network name, signal, security. Open and
   WPA/WPA2-PSK only. Typing a hidden network's name is allowed.
2. Enter the WiFi password; re-enter the dashboard password.
3. Confirmation dialog: "Move the grill to *Network*. If it can't join
   within a minute, the bridge restarts its WiFi module and it goes back to
   *Current network*."
4. Under the command lock: read the station state first (SSID, address),
   then `PB.SetWifiCredentials` (unsaved), then watch the station state over
   Bluetooth for up to 60 s.
5. **Success is evidence of the new network, not of "a" network.** It
   counts only when the station reports the requested SSID (and BSSID, if
   one was picked from the scan) with an address. If this firmware can't
   report the SSID (see Unknowns), success instead needs a disconnect seen
   *after* the command followed by a reconnect with an address; the old
   network still connected, at any point, never counts. Only then
   `Config.Save` (without its own restart), report `confirmed` with the new
   address, and offer to update `PITBOSS_HOST` if the bridge is set up for
   `http` later.
6. Not joined: `Sys.Reboot` to revert, wait for the module to come back on
   the old network (as for a reboot, with the no-data alert held back), and
   report `failed: reverted`. If it doesn't come back, `unconfirmed`, with
   recovery steps.

## Safety, as for the existing controls

Same gate as grill control: `SMOKESTACK_ENABLE_CONTROL`, login on, a valid
session, the `X-Smokestack-Control` header and same-host `Origin`, the
command lock, 5 s gap, fresh data, and a `control_command` audit row for
every attempt (never containing a password). Plus **re-entering the
dashboard password**, since these are the only commands whose failure can
lock the bridge out.

## Unknowns to check on the grill before building

These decide whether the design works as written:

1. Does `PB.SetWifiCredentials` make the module try the new network at
   once, or only after a restart? If only after a restart, step 4 becomes
   "save to a staging slot, restart, and fall back", which needs a
   different design.
2. Does `Sys.Reboot` drop unsaved config (the undo above), as Mongoose OS
   normally does?
3. Over Bluetooth, which call reports the station's state and address, and
   **does it include the SSID** (`Sys.GetInfo`'s `wifi` block, or `Wifi.*`
   if this firmware serves it; `list_rpcs()` tells)? Without the SSID,
   success falls back to "disconnected after the command, then
   reconnected".
4. Is `PB.SetDevicePassword` saved at once, and what lengths and characters
   does the grill accept?

A short read-only session at the grill (`list_rpcs()`, `Sys.GetInfo`, a
scan) answers 3. The rest need one deliberate test of each, with the vendor
app at hand to recover.

## Decisions for the owner

1. WiFi changes over Bluetooth only (recommended), or also over `http` with
   no automatic undo?
2. Re-enter the dashboard password for these two (recommended)?
3. The 60 s join window and automatic revert by restarting the module
   (recommended)?
4. Approve the password change first and WiFi after the unknowns are
   answered (recommended), or both together?

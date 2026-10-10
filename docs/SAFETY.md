# Safety design

The app controls heating equipment through a firmware interface it does not own, over a network that can fail.
It treats every heater command as **unconfirmed until telemetry proves it**, and it never relies on itself
as the last line of defence.

## Layers

| Layer | Protects against | Depends on this server? |
|---|---|---|
| Printer firmware thermal protection | heater runaway, sensor faults | no |
| **Independent hardware cutoff** (you install) | server/network/firmware hang with heater on, overheating spool | **no** |
| Controller daemon (systemd, `WatchdogSec=30`) | hung supervisor loop → killed and restarted | yes |
| Startup recovery | crash/reboot during a run → marked FAILED, **never resumed**, critical alert | yes |
| Independent heartbeat timer (`bambu-fc-watchdog.timer`) | controller dead → critical ntfy alert | yes (separate process) |
| `OnFailure=` alert unit | controller restart loop exhausted → critical alert | yes |
| Interlocks (`Printer.heater_control_allowed`) | heating an unverified printer/firmware | yes |
| Limits (min of model spec, printer cap, material max, preset max) | over-temperature targets | yes |

## Recommended hardware cutoff

The app cannot turn a heater off if it is down. Choose one of these, rated for the printer's full load:

* a smart plug or relay with an **on-device** auto-off timer and/or temperature cutoff that runs without this
  server (configured locally on the device, e.g. Tasmota/ESPHome rules with a local DS18B20 probe), or
* a thermostat/thermal cutoff switching mains to the printer, set a safe margin above the drying target.

Note: cutting printer power also drops the printer's own fans and safety monitoring. Choose a trip point that
only fires in an abnormal situation.

Scheduled or unattended starts need **either** a confirmed cutoff **or** the owner's explicit acceptance of running
without one (Settings → Safety → "Allow scheduled/unattended heating WITHOUT an independent cutoff"). Both
choices are written to the event log. Without a cutoff, the uncovered case is a total loss of this server or the
network mid-run: the bed may stay at its target until the controller comes back (it then sends heater-off
automatically) or someone switches it off at the printer.

## Scenario matrix (Phase 8 will test each one on hardware; all are covered by simulated-printer tests)

| Scenario | Software response (when implemented) | Needs hardware safeguard? |
|---|---|---|
| Printer disconnected | heater state unknown → critical alert, process FAILED | yes, if the heater was on |
| Printer already printing | start refused (state check) | no |
| Firmware rejects heater command | no target change in telemetry → FAILED, alert | no |
| Heater does not reach target | timeout → stop, alert | no |
| Temperature exceeds limit | heater-off command, verify, SAFETY_SHUTDOWN, critical alert | yes, if the off command fails |
| Scheduled start/stop | per-session prep confirmation, unattended permission, live pre-start checks; a start missed by >5 min is skipped and reported, never run late; stop verified by telemetry | yes (unattended), or accepted risk |
| Flask (web) restart | none needed: supervision is in the controller daemon | no |
| Controller restart / Debian reboot | process FAILED, never resumed, critical alert | **yes**: the heater may still be on |
| MQTT disconnection | as "printer disconnected" | yes |
| Failed shutdown command | retries, then "Heater shutdown NOT confirmed" alert | **yes** |
| Manual stop | heater-off + verification before the STOPPED state is final | no |
| Notification failure | alert still recorded on the dashboard, failure logged | no |

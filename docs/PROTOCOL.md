# Printer interfaces (P1S and original X1)

What the app uses, what is documented, and what was verified on real printers.
Sources: the community [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) documentation,
Bambu Lab's statements on Authorization Control, and testing on a P1S and an X1.

## Local MQTT

| | X1 (original) | P1S |
|---|---|---|
| Broker | TLS :8883, user `bblp`, password = LAN access code | same |
| Topics | `device/<SN>/report` (subscribe), `device/<SN>/request` (publish) | same |
| Status pushes | full `print` object each push | **deltas only**; full state needs `pushall`, which should not be sent more often than about every 5 min |
| Firmware version | `{"info":{"command":"get_version"}}` → module `ota`, `sw_ver` | same |
| Temperatures | `bed_temper`, `bed_target_temper`, `nozzle_temper`, `chamber_temper` | same keys; **no chamber sensor**, so `chamber_temper` is never displayed |
| Heatbed maximum (spec) | 110 °C | 100 °C |
| Chamber heater | none | none |

The printer's TLS certificate is signed by Bambu's private CA, so the app pins its SHA-256
fingerprint on first connection and refuses a different certificate afterwards.

## Firmware: Authorization Control

Bambu's "Authorization Control" firmware rejects control commands (including temperature changes)
from unauthorised software. Read-only status keeps working. The documented opt-out is
**LAN Only mode + Developer Mode** on the printer, which disables Bambu Cloud and Bambu Handy for that
printer. Without it, the app can monitor but every heater test fails with "firmware did not accept the
heater command", and the app reports that rather than working around it.

## Commands the app can send

All control goes through the MQTT `print.gcode_line` command, built only in
`controller/bambu_control.py`:

| Command | When | Verified on |
|---|---|---|
| `M140 S<n>` (bed target) | heater tests, drying, annealing steps, heater off (`S0`) | P1S (heater test passed; command accepted, heater-off confirmed) |
| `G28` → `G1 Z245` → `G1 X128 Y250` → `M400` | "Prepare printer", only with an empty bed | not yet verified; park position comes from the build volume |

Read-only requests: `pushall`, `get_version`. Nothing else can be built.

**Print hours:** an idle `M140` does not create a print job. Whether it adds to the printer's
lifetime print-hour counter has not been verified. No local MQTT field exposes that counter, so the
way to check is: note the counter on the touchscreen, run a heater test of more than an hour (the
counter shows whole hours), and compare.

**Firmware heater timeout:** whether the firmware switches an idle bed off by itself is unknown.
The app never relies on it.

## Cameras

| | X1 | P1S |
|---|---|---|
| Protocol | RTSPS `rtsps://bblp:<code>@<ip>:322/streaming/live/1`, H.264 1080p | TLS :6000, 80-byte auth packet, then `[16-byte header + JPEG]` frames, 1280×720 |
| Printer setting | **"LAN Only Liveview"** must be on (port 322 refuses connections otherwise; the status report shows `ipcam.rtsp_url = "disable"`) | none beyond Developer Mode |
| App handling | decoded with PyAV in the camera service (access code never on a command line), re-encoded to JPEG at 2 frames/s | frames passed through |

Both are verified working.

## Model differences that matter

- The P1S has no chamber sensor; the X1 reports one (a printer reading, not the air around a spool).
- P1S status reports are deltas; the app merges them into a cached full state.
- The P1S bed's 100 °C maximum limits drying and annealing targets; with the app's +8 °C
  over-temperature trip capped at the bed maximum, targets above about 92 °C leave little margin.

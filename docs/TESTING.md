# Testing

## Automated (no hardware)

```bash
venv/bin/python -m pytest -q
```

Covers: login required on every page, failed login, open-redirect guard, CSRF enforcement, access-code
encryption and keep-on-blank edit, bed cap ≤ model spec, LAN-only IPs, delete-requires-disabled, heater
interlock (including re-test after a firmware change), templates contain no invented values, bed target ≤ lowest
limit, "verified" requires test notes and resets on a temperature change, "reference" requires a source,
alerts recorded when ntfy fails, critical events can't be muted and go out at priority 5, startup recovery never
resumes heating, CSV formula-injection escaping, hardware-cutoff confirmation requires a description, and
security headers.

MQTT integration (`tests/test_mqtt_integration.py`) starts a private TLS mosquitto on 127.0.0.1 and a fake
P1S. It checks: connection, certificate pinning, firmware parsing, delta-report merging, chamber value
suppressed on P1S, only `pushall`/`get_version` ever sent, reconnect after a broker restart, certificate
mismatch blocks the link, wrong access code reported, and disabling stops the link.

Heater supervision (also in `tests/test_mqtt_integration.py`, fake printer with a simulated bed): happy path with
verified shutdown; firmware rejects the command; over-temperature → safety shutdown; printer ignores heater-off →
"NOT confirmed" alert and automatic retry; manual stop; refuses when busy / switch off / target over 45 °C;
expired start request never runs; a print starting mid-test is never sent heater-off; connection loss while
heating → alert, then heater-off sent and confirmed after reconnect.

## After install (manual)

1. `systemctl status bambu-fc-web bambu-fc-controller` shows both active; the navbar shows **Controller OK**.
2. Notifications → configure ntfy → **Send test notification** → a message arrives on your phone.
3. `sudo systemctl stop bambu-fc-controller`, wait about 1–2 min → you get a critical "Controller daemon heartbeat lost"
   ntfy message and the navbar shows **Controller DOWN**. Start it again.
4. Add both printers (they stay **disabled**). Confirm the access code isn't shown anywhere after saving.

## Hardware phases (require your approval each time)

* **Phase 3:** read-only MQTT: connectivity, field names per model, reconnect behaviour, TLS pinning.
* **Phase 4:** supervised, attended, short low-temperature bed test per printer, with heater-off verification
  and a before/after print-hours comparison. Never unattended.

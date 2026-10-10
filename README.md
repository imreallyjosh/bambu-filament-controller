# Bambu Filament Controller

A self-hosted web app that uses a **Bambu Lab P1S or X1** as a filament dryer and an annealing oven, over
the printer's local network connection. No Bambu Cloud needed.

- **Drying:** heat the bed while the printer is idle, without starting a print job. Start now or on a schedule
  (once, daily, weekly). Stop after a drying time or at a set clock time. Get an ntfy reminder halfway through
  to flip the spool.
- **Annealing programs:** ramp up at a set rate, hold, then a controlled ramp down, by stepping the bed
  target a degree at a time.
- **Presets** for common materials, each figure citing its source (Bambu Lab, Polymaker, SUNLU, and
  Siraya Tech data sheets). Separate values for each printer model.
- **Live dashboard:** temperatures, progress, a temperature history graph for each run, and each printer's
  camera (full screen too).
- **Notifications** through [ntfy](https://ntfy.sh) (and optionally email) for start, target reached,
  flip-the-spool, completion, disconnects, over-temperature and failed shutdowns.
- **Print jobs:** progress, layer, time remaining and stage for any print that's running.
- **History** of every run, with CSV export.

> Not affiliated with or endorsed by Bambu Lab. Use at your own risk. See [Safety](#before-you-heat-anything).

## Requirements

- A Bambu Lab **P1S** or original **X1** on your local network. Other models aren't supported yet.
- On each printer's touchscreen, under the network settings:
  - **LAN Only Mode** on, then **Developer Mode** on. Current Bambu firmware rejects temperature commands
    from third-party apps otherwise. This disables Bambu Cloud and the Handy app for that printer; Bambu Studio
    still works over LAN.
  - **X1 only:** turn on **LAN Only Liveview** to use the camera.
- A Linux machine on the same network that's always on (a Raspberry Pi works), with Docker.

## Install

```bash
git clone https://github.com/imreallyjosh/bambu-filament-controller.git
cd bambu-filament-controller
./install.sh
```

`install.sh` installs Docker if it's missing and generates the random secrets `.env` needs. It copies your
server's timezone, builds and starts the containers, and asks you to create a login. Re-running it is safe:
it never overwrites a value you've already set.

Then open `http://<your-server-ip>:8092` and log in.

**Useful commands** (run from the project directory):

```bash
docker compose logs -f controller   # watch printer connections and heating runs
docker compose ps                   # check that all four containers are healthy
docker compose down                 # stop everything
docker compose up -d                # start it again
./update.sh                         # update to the latest version
docker compose exec web /srv/app/docker-entrypoint.sh manage create-user <name>   # add or reset a login
```

Your data lives in the Docker volume `data`. **Back up `.env` as well:** its `BFC_FERNET_KEY` encrypts the
printers' access codes, and without it they can't be decrypted.

## First steps

1. **Printers → Add printer.** Enter the IP address, serial number and LAN access code from the printer's
   screen (Settings → Network / Device). Enable it. Within a few seconds the card shows live temperatures.
2. **Notifications.** Install the ntfy app on your phone, subscribe to a long random topic name, and enter
   `https://ntfy.sh` with that topic. Press **Send test notification**.
3. **Before any heating:** read [Before you heat anything](#before-you-heat-anything). Then set
   `BFC_ALLOW_HEATER_CONTROL=true` in `.env` and run `docker compose up -d`.
4. **Heater test** for each printer (Printers → *printer* → Heater test). The bed heats to 40 °C for 5
   minutes, and the app checks that the printer accepts the command and reports the heater off afterwards.
   Then click **Mark heater control verified**. A firmware update revokes this until you test again.
5. **Presets:** check the values for your materials, and enter your spools' heat ratings if you know them.
6. **Drying / Annealing:** pick the printer and preset, then press **Prepare printer** with the **bed empty**.
   It homes, lowers the bed and parks the toolhead. Confirm the bed is down, load the spool or part, tick the
   checklist and start.

## Before you heat anything

This app switches on a heater in your printer. Please understand what it can and can't protect against.

- **What it does:**
  - Every command is checked against what the printer reports back. A run counts as finished only when the
    printer confirms the heater is off.
  - Over-temperature, lost contact, a print starting, or running past the maximum heating time all switch the
    heater off and send you an alert.
  - After a crash or reboot, a run is **never resumed**. The heater is switched off as soon as the printer is
    reachable.
- **What it can't do:** if the server running this app, or your network, goes down mid-run, nothing can
  switch the bed off until it comes back. The bed would stay at its target temperature. The printer's own
  thermal protection still guards against heater faults, and the app never bypasses it. For unattended or
  scheduled use, consider an independent cutoff, such as a smart plug with its own on-device auto-off timer.
  Scheduled runs require you to either confirm such a cutoff, or accept the risk explicitly, in Settings → Safety.
- **Bed temperature isn't filament temperature.** The app shows the bed heater's reading. The air around a
  spool is much cooler, which is why bed targets differ from the oven figures on data sheets.
- **Spools:** many plastic spools soften at 70–90 °C. Check yours before drying at higher temperatures.
- **"Prepare printer" must only run with an empty bed.** Homing raises the bed until it touches the nozzle.

Details: [docs/SAFETY.md](docs/SAFETY.md).

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `BFC_SECRET_KEY`, `BFC_FERNET_KEY` | generated | Session signing and encryption keys. Back them up |
| `BFC_TIMEZONE`, `TZ` | your server's | Used for schedules and displayed times |
| `BFC_PORT` | `8092` | Port the dashboard is published on |
| `BFC_ENABLE_MQTT` | `true` | Read-only printer monitoring |
| `BFC_ALLOW_HEATER_CONTROL` | `false` | Master switch for heating and printer motion |
| `BFC_COOKIE_SECURE` | `false` | Set `true` behind an HTTPS reverse proxy |

After editing, run `docker compose up -d`. Notifications, safety limits and presets are set in the web UI.

## How it's built

```
browser ─HTTP:8092─▶ web (Flask + Gunicorn)          ─┐
                     controller (MQTT/TLS to printers) ├─ shared SQLite database on the "data" volume
                     camera (on-demand snapshots)      │
                     watchdog (alerts if controller dies) ─┘
```

- **controller** is the only container that talks to the printers' heaters, and it runs the supervisor and
  the scheduler. It exits if its main loop stalls, and Docker restarts it.
- **camera** is kept separate, so video decoding can never affect heater supervision.
- Printer access codes are encrypted at rest. Every page needs a login, and every action is CSRF-protected.

More: [docs/PROTOCOL.md](docs/PROTOCOL.md) (printer interfaces and model differences),
[docs/TESTING.md](docs/TESTING.md), [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Without Docker (systemd)

`deploy/install.sh` installs into `/opt/bambu-filament-controller`, with a dedicated service account, secrets in
`/etc/bambu-filament-controller/controller.env`, and systemd units (`bambu-fc-web`, `-controller`, `-camera`,
plus a watchdog timer). Run it with `sudo ./deploy/install.sh`; re-run it to upgrade.

## Development

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt pytest
venv/bin/python -m pytest -q    # needs openssl, mosquitto and ffmpeg for the integration tests
```

The test suite runs every safety path against a simulated printer: a local TLS MQTT broker and a fake
P1S that heats, rejects commands, ignores heater-off, and drops off the network.

## License

[MIT](LICENSE). Bundled third-party files keep their own licenses: Bootstrap and Chart.js (both MIT) in
`app/static/vendor/`.

# Troubleshooting

Commands below are for the systemd install. **With Docker**, use `docker compose logs <service>` instead of
`journalctl -u bambu-fc-<service>`, and `docker compose ps` instead of `systemctl status` (services: `web`,
`controller`, `camera`, `watchdog`). Settings in `/etc/bambu-filament-controller/controller.env` live in `.env`.

| Symptom | Check |
|---|---|
| Web UI unreachable | `systemctl status bambu-fc-web`; `journalctl -u bambu-fc-web -n 50`; port 8092 free? (`ss -ltnp 'sport = :8092'`) |
| "BFC_SECRET_KEY and BFC_FERNET_KEY must be set" | `/etc/bambu-filament-controller/controller.env` missing or unreadable by group `bambufc` (must be 0640 root:bambufc) |
| "Stored secret cannot be decrypted" | `BFC_FERNET_KEY` changed. Restore the old key from backup or re-enter the access codes |
| Navbar shows **Controller DOWN** | `journalctl -u bambu-fc-controller -n 100`. A watchdog kill shows as `Watchdog timeout`, often a locked or slow DB |
| `database is locked` | WAL is enabled; check nothing else holds the DB open (e.g. an open `sqlite3` shell) |
| CSRF error (400) on a form | session expired or cookies blocked; reload the page. If behind HTTPS, set `BFC_COOKIE_SECURE=true` |
| ntfy test fails | the result is shown on Notifications → Alert history. |
| Login locked out | 5 failures per 15 min per IP per worker; wait, or `sudo systemctl restart bambu-fc-web` |
| Forgot password | `sudo -u bambufc /opt/bambu-filament-controller/deploy/manage.sh create-user <name>` resets it |
| Printer shows "offline", card says monitoring is switched off | set `BFC_ENABLE_MQTT=true` in the env file, then restart both services |
| "MQTT connect refused: Not authorized" | wrong LAN access code. It can change when LAN Only mode is toggled; re-read it on the printer screen and edit the printer |
| Offline with "Disconnected" / no error | check the IP (DHCP may have moved it), that the printer is awake, and `nc -zv <ip> 8883` from the server |
| "TLS certificate does not match the pinned fingerprint" | a different device answered at that IP, or the printer was replaced or reset. Check, then disable the printer → Reset (TLS cert pinned) → enable |
| X1 chamber value looks wrong / P1S has none | the P1S has no chamber sensor, so it is hidden deliberately. The X1 value is the printer's own sensor, not the air around the spool |
| Camera says "Connecting…" forever | `systemctl status bambu-fc-camera`; `journalctl -u bambu-fc-camera -n 50`. The printer must be in LAN Only mode with Developer Mode; on some firmware there is also a "LAN Only Liveview" toggle under the network settings |
| Camera: "Camera closed the connection" | wrong access code (it changes when LAN mode is toggled), or the camera/liveview is disabled on the printer |
| X1 camera slow or choppy | the X1 stream is 1080p H.264 decoded on the server and limited to 2 snapshots/s; the camera service is capped at 150% CPU |

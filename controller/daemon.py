"""Controller daemon: the single process that will own printer connections.

Runs as its own systemd service (bambu-fc-controller.service), separate from
the Gunicorn web workers, so a web restart never interrupts supervision.

Phase 2 scope:
  * heartbeat to the DB so the UI can show controller health
  * systemd watchdog pings (Type=notify, WatchdogSec) - if this loop hangs,
    systemd kills and restarts the daemon, and OnFailure= sends an alert
  * startup recovery: any process left in an active heating state by a crash
    or reboot is marked failed and is NEVER resumed automatically

Phase 3: read-only MQTT telemetry when BFC_ENABLE_MQTT=true.
Phase 4: supervised, attended heater tests (process_runner) when BFC_ALLOW_HEATER_CONTROL=true.
Phase 5: drying runs and the persistent scheduler (controller/scheduler.py).
"""
import logging
import os
import signal
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import CommandRequest, Process, PrinterStatus, ProcessState, Setting, log_event, utcnow  # noqa: E402
from app.notifications import notify  # noqa: E402
from controller.process_runner import ProcessRunner  # noqa: E402
from controller.scheduler import Scheduler  # noqa: E402
from controller.telemetry import TelemetryManager  # noqa: E402

HEARTBEAT_S = 5
FLUSH_S = 2
SYNC_S = 10
RUNNER_S = 2
SCHEDULER_S = 15
log = logging.getLogger("bambu-fc-controller")


def _guarded(label, fn, last, key, interval, now) -> None:
    """Run a periodic task; a failure is logged but never stops the loop or the heartbeat."""
    if now - last[key] < interval:
        return
    last[key] = now
    try:
        fn()
    except Exception:
        db.session.rollback()
        log.exception("%s failed", label)


def sd_notify(msg: str) -> None:
    """Minimal sd_notify without a libsystemd dependency."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(addr)
        sock.sendall(msg.encode())


LOOP_STALL_EXIT_S = 60
LOOP_WATCH_INTERVAL_S = 10


def start_loop_watchdog(last: dict) -> None:
    """Without systemd's watchdog (e.g. in Docker), exit if the main loop stops heart-beating.

    The container's restart policy then restarts the daemon, whose startup recovery marks any active
    process failed and switches its heater off once the printer is reachable.
    """
    def watch():
        while True:
            time.sleep(LOOP_WATCH_INTERVAL_S)
            if last["heartbeat"] and time.monotonic() - last["heartbeat"] > LOOP_STALL_EXIT_S:
                log.critical("Main loop stalled for over %s s; exiting so the supervisor restarts us.",
                             LOOP_STALL_EXIT_S)
                os._exit(70)

    threading.Thread(target=watch, daemon=True, name="loop-watchdog").start()


def recover_interrupted_processes() -> list[int]:
    """Fail-safe on startup: never resume heating after a restart.

    Active processes are marked failed; queued start requests and pending processes are cancelled.
    Returns the printer ids whose heater must be switched off once they are reachable.
    """
    for cmd in CommandRequest.query.filter_by(status="queued").all():
        cmd.status, cmd.processed_at = "failed", utcnow()
        cmd.result = "Controller restarted before this request ran; not executed."
    for proc in Process.query.filter_by(state=ProcessState.PENDING).all():
        proc.state, proc.ended_at, proc.heater_off_verified = ProcessState.STOPPED, utcnow(), True
        proc.error = "Controller restarted before start; cancelled (heating never started)."
    db.session.commit()
    orphaned = Process.query.filter(Process.state.in_(ProcessState.ACTIVE)).all()
    for proc in orphaned:
        prev = proc.state
        proc.state = ProcessState.FAILED
        proc.ended_at = utcnow()
        proc.error = (f"Controller restarted while process was '{prev}'. Not resumed. "
                      "Heater state unknown until telemetry confirms it is off.")
        log_event("safety", proc.error, level="critical", printer_id=proc.printer_id, process_id=proc.id,
                  actor="controller")
        db.session.commit()
        notify("process_ended_unexpectedly", printer=proc.printer_name, material=proc.material_label,
               process=f"{proc.kind} #{proc.id}", details=proc.error)
        notify("heater_off_unconfirmed", printer=proc.printer_name, material=proc.material_label,
               process=f"{proc.kind} #{proc.id}",
               details="Check the printer physically. The controller will send heater-off and verify it as soon "
                       "as the printer is reachable.")
    return [p.printer_id for p in orphaned if p.printer_id]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    with app.app_context():
        recovered = recover_interrupted_processes()
        telemetry = TelemetryManager() if app.config["ENABLE_MQTT"] else None
        runner = ProcessRunner(telemetry) if telemetry else None
        scheduler = Scheduler() if telemetry else None
        if runner:
            runner.pending_off.update(recovered)
        mqtt_mode = "read-only, starting" if telemetry else "disabled (BFC_ENABLE_MQTT=false)"
        Setting.set("controller.started_at", utcnow().isoformat())
        Setting.set("controller.mqtt_mode", mqtt_mode)
        log_event("system", f"Controller started (MQTT: {mqtt_mode}); {len(recovered)} interrupted process(es) "
                  "marked failed.", actor="controller")
        db.session.commit()
        sd_notify("READY=1")
        log.info("Controller ready (MQTT: %s)", mqtt_mode)

        last = {"heartbeat": 0.0, "flush": 0.0, "sync": 0.0, "runner": 0.0, "scheduler": 0.0}
        if not os.environ.get("NOTIFY_SOCKET"):
            start_loop_watchdog(last)
        while not stop.is_set():
            now = time.monotonic()
            if telemetry:
                telemetry.drain(timeout=1.0)
                _guarded("telemetry flush", telemetry.flush, last, "flush", FLUSH_S, now)
                _guarded("printer sync", lambda: (telemetry.sync_printers(), telemetry.periodic()),
                         last, "sync", SYNC_S, now)
                _guarded("scheduler", scheduler.tick, last, "scheduler", SCHEDULER_S, now)
                _guarded("process supervision", runner.tick, last, "runner", RUNNER_S, now)
            else:
                stop.wait(1.0)
            if now - last["heartbeat"] >= HEARTBEAT_S:
                try:
                    Setting.set("controller.heartbeat_at", utcnow().isoformat())
                    if telemetry:
                        Setting.set("controller.mqtt_mode", telemetry.mode_label())
                    db.session.commit()
                    # Only ping the watchdog after a successful DB write; a wedged DB => restart.
                    sd_notify("WATCHDOG=1")
                    last["heartbeat"] = now
                except Exception:
                    db.session.rollback()
                    log.exception("Heartbeat failed")

        sd_notify("STOPPING=1")
        if telemetry:
            telemetry.stop_all()
            for status in PrinterStatus.query.all():
                status.online = False
                status.last_error = "Controller stopped"
        log_event("system", "Controller stopped.", actor="controller")
        db.session.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())

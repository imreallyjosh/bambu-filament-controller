"""Read-only telemetry manager. Runs in the controller daemon's main thread
(inside a Flask app context) and is the only code that writes printer status."""
import json
import logging
import queue
import time
from datetime import timedelta

from app.capabilities import has_chamber_sensor, profile_for
from app.crypto import SecretError
from app.extensions import db
from app.models import Printer, PrinterStatus, Setting, TelemetrySample, log_event, utcnow
from app.notifications import notify

from . import bambu_protocol as proto
from .printer_link import LinkConfig, LinkEvent, PrinterLink

log = logging.getLogger("bambu-fc-controller.telemetry")

SAMPLE_INTERVAL_S = 30
ACTIVE_SAMPLE_INTERVAL_S = 10  # while a heating process is running
RETENTION_DAYS = 30
RAW_JSON_MAX = 64_000


class TelemetryManager:
    def __init__(self):
        self.events: "queue.Queue[LinkEvent]" = queue.Queue()
        self.links: dict[int, PrinterLink] = {}
        self._pending: dict[int, dict] = {}       # printer_id -> latest merged print state
        self._last_sample: dict[int, float] = {}
        self._offline_since: dict[int, float] = {}
        self._disconnect_notified: set[int] = set()
        self._last_prune = 0.0
        # Set by the process runner: printer_id -> active process id (faster sampling, tagged samples).
        self.active_process: dict[int, int] = {}
        # Printer responses to control commands, consumed by the process runner.
        self.command_results: dict[int, list[dict]] = {}

    # -- connection management ------------------------------------------
    def sync_printers(self) -> None:
        """Start/stop/rebuild links to match enabled printers in the DB."""
        wanted: dict[int, LinkConfig] = {}
        for p in Printer.query.filter_by(enabled=True).all():
            try:
                code = p.access_code
            except SecretError as exc:
                self._set_error(p.id, str(exc))
                continue
            wanted[p.id] = LinkConfig(p.id, p.name, p.model, p.ip_address, profile_for(p.model).mqtt_port,
                                      p.serial, code, p.tls_fingerprint)

        for pid in list(self.links):
            link = self.links[pid]
            if pid not in wanted or wanted[pid].identity() != link.cfg.identity():
                log.info("[%s] stopping link (disabled or settings changed)", link.cfg.name)
                link.stop()
                del self.links[pid]
                self._mark_offline(pid, "Monitoring stopped (printer disabled or settings changed)")
        for pid, cfg in wanted.items():
            if pid not in self.links:
                link = PrinterLink(cfg, self.events)
                self.links[pid] = link
                self._offline_since.setdefault(pid, time.monotonic())
                link.start()
        db.session.commit()

    def stop_all(self) -> None:
        for link in self.links.values():
            link.stop()
        self.links.clear()

    # -- event handling -------------------------------------------------
    def drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ev = self.events.get(timeout=remaining)
            except queue.Empty:
                break
            try:
                self._handle(ev)
            except Exception:
                db.session.rollback()
                log.exception("Failed to handle %s event for printer %s", ev.kind, ev.printer_id)

    def _handle(self, ev: LinkEvent) -> None:
        printer = db.session.get(Printer, ev.printer_id)
        if printer is None:
            return
        if ev.kind == "report":
            self._pending[ev.printer_id] = ev.data["state"]
            return
        if ev.kind == "connected":
            self._offline_since.pop(ev.printer_id, None)
            status = self._status(ev.printer_id)
            status.online = True
            status.last_error = None
            status.last_seen_at = utcnow()
            log_event("printer", f"'{printer.name}' connected (read-only MQTT).", printer_id=printer.id,
                      actor="controller")
            if ev.printer_id in self._disconnect_notified:
                self._disconnect_notified.discard(ev.printer_id)
                log_event("printer", f"'{printer.name}' reconnected.", printer_id=printer.id, actor="controller")
        elif ev.kind == "disconnected":
            self._mark_offline(ev.printer_id, f"Disconnected: {ev.data.get('reason')}")
            log_event("printer", f"'{printer.name}' disconnected ({ev.data.get('reason')}); reconnecting.",
                      level="warning", printer_id=printer.id, actor="controller")
        elif ev.kind == "pinned":
            printer.tls_fingerprint = ev.data["fingerprint"]
            log_event("printer", f"'{printer.name}' TLS certificate pinned: {ev.data['fingerprint'][:23]}…",
                      printer_id=printer.id, actor="controller")
        elif ev.kind == "firmware":
            version = ev.data["version"]
            if printer.firmware_version_reported != version:
                log_event("printer", f"'{printer.name}' reports firmware {version} "
                          f"(was {printer.firmware_version_reported or 'unknown'}).",
                          level="warning" if printer.firmware_version_reported else "info",
                          printer_id=printer.id, actor="controller")
                printer.firmware_version_reported = version
        elif ev.kind == "command_result":
            self.command_results.setdefault(ev.printer_id, []).append(ev.data)
            level = "info" if str(ev.data.get("result")).lower() == "success" else "warning"
            log_event("printer", f"'{printer.name}' command response: result={ev.data.get('result')} "
                      f"reason={ev.data.get('reason') or '-'}", level=level, printer_id=printer.id, actor="controller")
        elif ev.kind == "error":
            self._set_error(ev.printer_id, ev.data["message"])
            log_event("printer", f"'{printer.name}': {ev.data['message']}", level="error",
                      printer_id=printer.id, actor="controller")
        db.session.commit()

    # -- periodic work --------------------------------------------------
    def flush(self) -> None:
        """Write the latest state per printer (throttled; reports can arrive every second)."""
        now = time.monotonic()
        for pid, state in list(self._pending.items()):
            printer = db.session.get(Printer, pid)
            if printer is None:
                continue
            fields = proto.extract_status(state, has_chamber_sensor(printer.model))
            status = self._status(pid)
            self._log_transition(printer, status, fields, state)
            for key in ("gcode_state", "stage", "bed_temp_c", "bed_target_c", "nozzle_temp_c",
                        "nozzle_target_c", "chamber_temp_c"):
                setattr(status, key, fields[key])
            status.hms_json = json.dumps(fields["hms"])
            raw = json.dumps(state)
            status.raw_json = raw if len(raw) <= RAW_JSON_MAX else None
            status.online = True
            status.last_seen_at = utcnow()
            proc_id = self.active_process.get(pid)
            interval = ACTIVE_SAMPLE_INTERVAL_S if proc_id else SAMPLE_INTERVAL_S
            if now - self._last_sample.get(pid, 0) >= interval:
                db.session.add(TelemetrySample(
                    printer_id=pid, process_id=proc_id, bed_temp_c=fields["bed_temp_c"], bed_target_c=fields["bed_target_c"],
                    nozzle_temp_c=fields["nozzle_temp_c"], chamber_temp_c=fields["chamber_temp_c"]))
                self._last_sample[pid] = now
        self._pending.clear()
        db.session.commit()

    def periodic(self) -> None:
        now = time.monotonic()
        timeout = int(Setting.get("safety.disconnect_timeout_s", 60))
        for pid, link in self.links.items():
            # P1-series only sends changes, so refresh full state at the model's safe interval.
            interval = profile_for(link.cfg.model).pushall_min_interval_s
            if link.connected and now - link.last_pushall_at >= interval:
                link.request("pushall")
            # A connected link that has gone silent far beyond the refresh interval is not trustworthy.
            if link.connected and link.last_message_at and now - link.last_message_at > interval + timeout:
                self._mark_offline(pid, f"No data for {int(now - link.last_message_at)} s")
                self._offline_since.setdefault(pid, link.last_message_at)
            since = self._offline_since.get(pid)
            if since and not link.connected and now - since > timeout and pid not in self._disconnect_notified \
                    and not link.blocked:
                self._disconnect_notified.add(pid)
                printer = db.session.get(Printer, pid)
                notify("printer_disconnected", printer=link.cfg.name, process="monitoring",
                       details=f"No MQTT connection to {printer.ip_address} for over {timeout} s. "
                               f"Last error: {self._status(pid).last_error or 'none'}")
        if now - self._last_prune > 3600:
            cutoff = utcnow() - timedelta(days=RETENTION_DAYS)
            TelemetrySample.query.filter(TelemetrySample.ts < cutoff, TelemetrySample.process_id.is_(None)).delete()
            self._last_prune = now
        db.session.commit()

    def mode_label(self) -> str:
        n = len(self.links)
        up = sum(1 for link in self.links.values() if link.connected)
        return f"read-only, {up}/{n} printer(s) connected"

    # -- helpers --------------------------------------------------------
    def _log_transition(self, printer: Printer, status: PrinterStatus, fields: dict, state: dict) -> None:
        """Record state and bed-target changes, with the job-identifying fields, for later analysis
        (e.g. whether the firmware's own drying function runs as a print job)."""
        old_state, new_state = status.gcode_state, fields["gcode_state"]
        old_target, new_target = status.bed_target_c, fields["bed_target_c"]
        state_changed = new_state is not None and old_state != new_state
        target_changed = new_target is not None and old_target is not None and abs(new_target - old_target) >= 1
        if not (state_changed or target_changed):
            return
        if state_changed and new_state in ("PREPARE", "RUNNING") and printer.motion_prepared_at is not None:
            printer.motion_prepared_at = None  # the bed moves during a print; prepare again before drying
        context = proto.job_context(state)
        parts = []
        if state_changed:
            parts.append(f"state {old_state or '?'} → {new_state}")
        if target_changed or state_changed:
            parts.append(f"bed target {old_target if old_target is not None else '?'} → {new_target}")
        if context:
            parts.append("; ".join(f"{k}={v}" for k, v in context.items()))
        log_event("printer", f"'{printer.name}' " + ", ".join(parts), printer_id=printer.id, actor="controller")

    def _status(self, pid: int) -> PrinterStatus:
        status = db.session.get(PrinterStatus, pid)
        if status is None:
            status = PrinterStatus(printer_id=pid, online=False)
            db.session.add(status)
        return status

    def _mark_offline(self, pid: int, reason: str) -> None:
        status = self._status(pid)
        status.online = False
        status.last_error = reason
        self._offline_since.setdefault(pid, time.monotonic())

    def _set_error(self, pid: int, message: str) -> None:
        status = self._status(pid)
        status.online = False
        status.last_error = message

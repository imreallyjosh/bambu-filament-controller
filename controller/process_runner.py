"""Supervises heating processes: heater tests (Phase 4), drying runs (Phase 5), annealing programs (Phase 7).

Principles:
  * The daemon re-validates everything; the web UI's checks are only for convenience.
  * A command is never assumed to have worked. "Accepted" means the printer
    reported the new bed target; "off" means a report received AFTER the off
    command shows target 0.
  * When in doubt, switch the heater off and say so loudly.
  * If the printer can't be reached, it's impossible to switch it off. That is
    reported as "heater shutdown NOT confirmed", and the off command is retried
    automatically when the printer comes back (pending_off).
"""
import logging
import math
import time
from datetime import timedelta
from dataclasses import dataclass, field

from flask import current_app

from app.extensions import db
from app.capabilities import profile_for
from app.limits import effective_bed_max, overtemp_trip_c
from app.models import (AnnealingPreset, CommandRequest, DryingPreset, Printer, Process, ProcessState, Setting,
                        log_event, utcnow)
from app.notifications import notify
from app.safety import unattended_heating_allowed
from app.timeutil import fmt

from . import bambu_control as control

log = logging.getLogger("bambu-fc-controller.runner")

IDLE_STATES = {"IDLE", "FINISH", "FAILED"}
PRINT_STATES = {"RUNNING", "PREPARE", "PAUSE", "SLICING"}

# Heater-test limits (attended tests only).
TEST_MAX_TARGET_C = 50
TEST_MAX_DURATION_MIN = 90


START_COMMANDS = {"start_heater_test": "heater_test", "start_drying": "drying", "start_annealing": "annealing"}


def _annealing_plan(proc: Process) -> str:
    up = f"ramp {proc.heat_ramp_c_per_h:g} °C/h" if proc.heat_ramp_c_per_h else "heat at full rate"
    down = (f"ramp down {proc.cool_ramp_c_per_h:g} °C/h to {proc.cool_end_c:g} °C" if proc.cool_ramp_c_per_h
            else "heater off to cool naturally")
    return f"{up} to {proc.target_bed_c:g} °C, hold {proc.planned_duration_min} min, {down}"


def _label(proc: Process) -> str:
    return f"{'heater test' if proc.kind == 'heater_test' else proc.kind} #{proc.id}"


@dataclass
class Shutdown:
    final_state: str
    reason: str
    attempts: int = 0
    last_sent: float = 0.0
    sent_at: float | None = None  # monotonic time of the first successfully published off command
    started: float = field(default_factory=time.monotonic)


class ProcessRunner:
    ACCEPT_TIMEOUT_S = 45
    REACH_TIMEOUT_S = 20 * 60
    REACH_TIMEOUT_DRYING_S = 40 * 60  # higher targets take longer to reach
    TARGET_TOLERANCE_C = 2.0
    OFF_RETRY_S = 10
    OFF_MAX_ATTEMPTS = 6
    FRESH_DATA_S = 120
    COMM_LOSS_FAIL_S = 15 * 60
    START_REQUEST_TTL_S = 120
    MINUTE_S = 60  # tests shorten this; production is always 60
    MIN_START_MARGIN_C = 5

    def __init__(self, telemetry):
        self.tm = telemetry
        self.shutdowns: dict[int, Shutdown] = {}       # process_id -> pending verified shutdown
        self.pending_off: set[int] = set()             # printer ids that must be switched off when reachable
        self._comm_lost_at: dict[int, float] = {}      # process_id -> monotonic
        self._start_wait: dict[int, float] = {}        # command id -> deadline for fresh telemetry
        self._accept_deadline: dict[int, float] = {}   # process_id -> deadline for printer to report new target
        self._pending_last: dict[int, float] = {}      # printer_id -> last off attempt for pending_off
        self._accepted_once: set[int] = set()          # processes whose first command was confirmed
        self._motion_wait: dict[int, float] = {}       # prepare-motion command id -> result deadline

    # ------------------------------------------------------------------
    def tick(self) -> None:
        self._process_commands()
        self._check_motion_results()
        for proc in Process.query.filter(Process.state.in_(ProcessState.ACTIVE)).all():
            try:
                self._supervise(proc)
            except Exception:
                db.session.rollback()
                log.exception("Supervision error for process %s", proc.id)
                self._begin_shutdown(proc, ProcessState.FAILED, "Internal supervision error; switching heater off.")
        self._handle_pending_off()
        self.tm.active_process = {
            p.printer_id: p.id for p in Process.query.filter(Process.state.in_(ProcessState.ACTIVE)).all()
        }
        db.session.commit()

    # -- commands from the web UI ----------------------------------------
    def _process_commands(self) -> None:
        for cmd in CommandRequest.query.filter_by(status="queued").order_by(CommandRequest.id).all():
            proc = db.session.get(Process, cmd.process_id) if cmd.process_id else None
            if cmd.command == "stop":
                if proc and proc.state in ProcessState.ACTIVE:
                    self._begin_shutdown(proc, ProcessState.STOPPED, f"Stopped by {cmd.requested_by}.")
                    self._finish_cmd(cmd, "sent", "Heater off requested; verifying.")
                elif proc and proc.state == ProcessState.PENDING:
                    proc.state, proc.ended_at, proc.error = ProcessState.STOPPED, utcnow(), "Cancelled before start."
                    proc.heater_off_verified = True
                    self._finish_cmd(cmd, "confirmed", "Cancelled before heating.")
                else:
                    self._finish_cmd(cmd, "failed", "No active process to stop.")
            elif cmd.command == "prepare_motion":
                self._prepare_motion(cmd)
            elif cmd.command in START_COMMANDS:
                self._start(cmd, proc)
            else:
                self._finish_cmd(cmd, "failed", f"Unknown command '{cmd.command}'.")
        db.session.commit()

    def _prepare_motion(self, cmd: CommandRequest) -> None:
        """Home, lower the bed and park the toolhead. The owner confirmed the bed is empty when requesting it."""
        if (utcnow() - cmd.created_at).total_seconds() > self.START_REQUEST_TTL_S:
            return self._finish_cmd(cmd, "failed", "Request expired; motion is never started late. Try again.")
        if not current_app.config["ALLOW_HEATER_CONTROL"]:
            return self._finish_cmd(cmd, "failed", "Printer control is disabled on the server.")
        printer = db.session.get(Printer, cmd.printer_id)
        link = self.tm.links.get(cmd.printer_id)
        if printer is None or not printer.enabled or link is None or not link.connected:
            return self._finish_cmd(cmd, "failed", "Printer is not connected.")
        if Process.query.filter(Process.printer_id == printer.id,
                                Process.state.in_(ProcessState.ACTIVE | {ProcessState.PENDING})).count():
            return self._finish_cmd(cmd, "failed", "An operation is active on this printer; motion refused.")
        live = link.live()
        if live["gcode_state"] not in IDLE_STATES:
            return self._finish_cmd(cmd, "failed", f"Printer is busy (state {live['gcode_state']}).")
        x, y, z = profile_for(printer.model).park_xyz
        self.tm.command_results.pop(printer.id, None)
        if not link.send_motion(control.prepare_sequence(x, y, z)):
            return self._finish_cmd(cmd, "failed", "Could not publish the command (connection dropped).")
        printer.motion_prepared_at = None  # only the owner's visual confirmation sets it
        self._motion_wait[cmd.id] = time.monotonic() + self.ACCEPT_TIMEOUT_S
        self._finish_cmd(cmd, "sent", "Sent: home, lower bed, park toolhead. Watch the printer, then confirm.")
        log_event("printer", f"'{printer.name}': prepare motion sent (home, bed to Z{z}, toolhead to X{x} Y{y}).",
                  level="warning", printer_id=printer.id, actor=cmd.requested_by)
        return None

    def _check_motion_results(self) -> None:
        for cmd_id, deadline in list(self._motion_wait.items()):
            cmd = db.session.get(CommandRequest, cmd_id)
            results = self.tm.command_results.get(cmd.printer_id, []) if cmd else []
            failed = [r for r in results if str(r.get("result")).lower() not in ("success", "none")]
            if cmd is None:
                self._motion_wait.pop(cmd_id)
            elif failed:
                self._finish_cmd(cmd, "failed", f"Printer rejected the motion command: result={failed[-1].get('result')} "
                                                f"reason={failed[-1].get('reason')}.")
                self._motion_wait.pop(cmd_id)
            elif any(str(r.get("result")).lower() == "success" for r in results):
                self._finish_cmd(cmd, "confirmed", "Printer accepted the motion command. Check that the bed is at "
                                                   "the bottom and the toolhead is parked, then confirm.")
                self._motion_wait.pop(cmd_id)
            elif time.monotonic() > deadline:
                cmd.result = "Sent; the printer did not report a result. Check it visually, then confirm."
                self._motion_wait.pop(cmd_id)

    def _finish_cmd(self, cmd: CommandRequest, status: str, result: str) -> None:
        cmd.status, cmd.result, cmd.processed_at = status, result, utcnow()

    def _fail_start(self, cmd, proc, reason: str) -> None:
        self._finish_cmd(cmd, "failed", reason)
        if proc:
            proc.state, proc.ended_at, proc.error = ProcessState.FAILED, utcnow(), f"Not started: {reason}"
            proc.heater_off_verified = True  # heater was never commanded on
            log_event("process", f"{_label(proc)} not started: {reason}", level="warning",
                      printer_id=proc.printer_id, process_id=proc.id, actor="controller")
            db.session.commit()
            notify("schedule_failed", printer=proc.printer_name, material=proc.material_label,
                   process=_label(proc), details=reason)

    def _start(self, cmd: CommandRequest, proc: Process | None) -> None:
        """Validate a start request against live printer data, then send the bed target."""
        kind = START_COMMANDS[cmd.command]
        if proc is None or proc.kind != kind or proc.state != ProcessState.PENDING:
            return self._fail_start(cmd, proc, "Process missing or not pending.")
        if (utcnow() - cmd.created_at).total_seconds() > self.START_REQUEST_TTL_S:
            return self._fail_start(cmd, proc, "Start request expired; heating is never started late. Start again.")
        if not current_app.config["ALLOW_HEATER_CONTROL"]:
            return self._fail_start(cmd, proc, "Heater control is disabled on the server (BFC_ALLOW_HEATER_CONTROL).")
        printer = db.session.get(Printer, proc.printer_id)
        if printer is None or not printer.enabled:
            return self._fail_start(cmd, proc, "Printer missing or disabled.")
        others = Process.query.filter(Process.printer_id == printer.id, Process.id != proc.id,
                                      Process.state.in_(ProcessState.ACTIVE)).count()
        if others or printer.id in self.pending_off:
            return self._fail_start(cmd, proc, "Another operation is active on this printer.")
        link = self.tm.links.get(printer.id)
        if link is None or not link.connected:
            return self._fail_start(cmd, proc, "Printer is not connected.")
        live = link.live()
        stale = live["age_s"] is None or live["age_s"] > self.FRESH_DATA_S or live["bed_target_c"] is None
        if stale or not printer.firmware_version_reported:
            # P1-series idle printers may be quiet: request fresh data once, then wait briefly.
            deadline = self._start_wait.get(cmd.id)
            if deadline is None:
                if stale:
                    link.request("pushall")
                if not printer.firmware_version_reported:
                    link.request("get_version")
                self._start_wait[cmd.id] = time.monotonic() + 15
                return None
            if time.monotonic() < deadline:
                return None
            self._start_wait.pop(cmd.id, None)
            return self._fail_start(cmd, proc, "No fresh status from the printer." if stale else
                                    "Printer firmware version unknown; results must be tied to a firmware version.")
        self._start_wait.pop(cmd.id, None)
        if live["gcode_state"] not in IDLE_STATES:
            return self._fail_start(cmd, proc, f"Printer is busy (state {live['gcode_state']}).")
        if live["bed_target_c"] and live["bed_target_c"] > 0:
            return self._fail_start(cmd, proc, f"Bed heater is already on (target {live['bed_target_c']} °C).")
        target = int(proc.target_bed_c or 0)
        hard_max = int(Setting.get("safety.max_heating_minutes", 720))

        if kind == "heater_test":
            if live["bed_c"] is None or live["bed_c"] > target - self.MIN_START_MARGIN_C:
                return self._fail_start(cmd, proc, f"Bed is at {live['bed_c']} °C, too close to the {target} °C "
                                                   "target for heating to be observed. Let it cool first.")
            effective_max = min(TEST_MAX_TARGET_C, effective_bed_max(printer))
            max_minutes = min(TEST_MAX_DURATION_MIN, hard_max)
        elif kind == "annealing":
            reason = self._annealing_preconditions(printer, proc)
            if reason:
                return self._fail_start(cmd, proc, reason)
            preset = db.session.get(AnnealingPreset, proc.annealing_preset_id)
            effective_max = min([effective_bed_max(printer)] + ([preset.max_temp_c] if preset.max_temp_c else []))
            if target > effective_max:
                return self._fail_start(cmd, proc, f"Annealing temperature {target} °C exceeds the limit "
                                                   f"({effective_max:.0f} °C).")
            # Ramped heating starts just above the current bed temperature and steps up.
            start_c = int(live["bed_c"] or 25) + 1 if proc.heat_ramp_c_per_h else target
            proc.ramp_start_c = min(start_c, target)
            total_min = self._annealing_total_minutes(proc)
            if not (1 <= (proc.planned_duration_min or 0)) or total_min > hard_max:
                return self._fail_start(cmd, proc, f"Program needs about {total_min:.0f} min of heating; the safety "
                                                   f"limit is {hard_max} min (Settings → Safety).")
            target = int(proc.ramp_start_c)  # first setpoint
            max_minutes = hard_max
        else:
            reason = self._drying_preconditions(printer, proc)
            if reason:
                return self._fail_start(cmd, proc, reason)
            preset = db.session.get(DryingPreset, proc.drying_preset_id)
            effective_max = effective_bed_max(printer, preset, preset.setting_for(printer.model))
            max_minutes = hard_max
        if kind != "annealing" and proc.scheduled_end_at is None \
                and not (1 <= (proc.planned_duration_min or 0) <= max_minutes):
            return self._fail_start(cmd, proc, f"Duration must be 1–{max_minutes} minutes.")
        if proc.scheduled_end_at is not None and proc.scheduled_end_at <= utcnow():
            return self._fail_start(cmd, proc, "The requested stop time has already passed.")
        try:
            bed_cmd = control.bed_target(target, effective_max)
        except ValueError as exc:
            return self._fail_start(cmd, proc, str(exc))

        self.tm.command_results.pop(printer.id, None)
        proc.firmware_at_start = printer.firmware_version_reported
        if not link.send_bed(bed_cmd):
            return self._fail_start(cmd, proc, "Could not publish the command (connection dropped).")
        proc.state, proc.started_at = ProcessState.HEATING, utcnow()
        proc.current_setpoint_c = target
        # Over-temperature trip is relative to the final (hold) target, so it is fixed for the whole program.
        proc.max_temp_c = overtemp_trip_c(printer, int(proc.target_bed_c))
        self._accept_deadline[proc.id] = time.monotonic() + self.ACCEPT_TIMEOUT_S
        self._finish_cmd(cmd, "sent", f"M140 S{target} sent; waiting for the printer to report the new target.")
        plan = (f"until {fmt(proc.scheduled_end_at)}" if proc.scheduled_end_at
                else _annealing_plan(proc) if kind == "annealing"
                else f"for {proc.planned_duration_min} min" + (" after reaching target" if kind == "drying" else ""))
        log_event("process", f"{_label(proc)}: bed target {target} °C sent, {plan}.", level="warning",
                  printer_id=printer.id, process_id=proc.id, actor="controller")
        db.session.commit()
        notify("process_started", printer=printer.name, material=proc.material_label, process=_label(proc),
               details=f"Bed target {target} °C {plan}. Over-temperature trip {proc.max_temp_c:.0f} °C.")
        return None

    def _drying_preconditions(self, printer: Printer, proc: Process) -> str | None:
        allowed, reason = printer.heater_control_allowed(current_app.config["ALLOW_HEATER_CONTROL"])
        if not allowed:
            return reason
        preset = db.session.get(DryingPreset, proc.drying_preset_id) if proc.drying_preset_id else None
        if preset is None:
            return "Drying preset missing."
        if proc.prep_confirmed_at is None:
            return "Physical preparation was not confirmed for this session."
        if printer.motion_prepared_at is None:
            return "Printer not prepared (home, lower bed, park toolhead) since its last print."
        if proc.schedule_id is not None:
            ok, why = unattended_heating_allowed()
            if not ok:
                return why
        return None

    def _annealing_preconditions(self, printer: Printer, proc: Process) -> str | None:
        allowed, reason = printer.heater_control_allowed(current_app.config["ALLOW_HEATER_CONTROL"])
        if not allowed:
            return reason
        if proc.annealing_preset_id is None or db.session.get(AnnealingPreset, proc.annealing_preset_id) is None:
            return "Annealing preset missing."
        if proc.prep_confirmed_at is None:
            return "Physical preparation was not confirmed for this session."
        if printer.motion_prepared_at is None:
            return "Printer not prepared (home, lower bed, park toolhead) since its last print."
        return None

    @staticmethod
    def _annealing_total_minutes(proc: Process) -> float:
        target = proc.target_bed_c
        ramp_up = ((target - (proc.ramp_start_c or target)) / proc.heat_ramp_c_per_h * 60) if proc.heat_ramp_c_per_h else 0
        cool = ((target - (proc.cool_end_c or target)) / proc.cool_ramp_c_per_h * 60) if proc.cool_ramp_c_per_h else 0
        return max(0, ramp_up) + (proc.planned_duration_min or 0) + max(0, cool)

    def _send_setpoint(self, proc: Process, value: int) -> None:
        """Step the bed target during a temperature program; acceptance is checked like the first command."""
        link = self.tm.links.get(proc.printer_id)
        cmd = control.bed_target(value, proc.target_bed_c)  # never above the program's hold temperature
        self.tm.command_results.pop(proc.printer_id, None)
        if link and link.send_bed(cmd):
            proc.current_setpoint_c = value
            self._accept_deadline[proc.id] = time.monotonic() + self.ACCEPT_TIMEOUT_S

    def _anneal_program(self, proc: Process, bed: float | None) -> None:
        now = utcnow()
        hours_since = lambda t: (now - t).total_seconds() / (60 * self.MINUTE_S)  # noqa: E731
        target = int(proc.target_bed_c)
        setpoint = int(proc.current_setpoint_c or target)
        if proc.state == ProcessState.HEATING:
            if proc.heat_ramp_c_per_h and setpoint < target:
                desired = min(target, int(proc.ramp_start_c + proc.heat_ramp_c_per_h * hours_since(proc.started_at)))
                if desired > setpoint:
                    self._send_setpoint(proc, desired)
                return None
            if bed is not None and bed >= target - self.TARGET_TOLERANCE_C:
                proc.state, proc.target_reached_at = ProcessState.DRYING, now
                log_event("process", f"Annealing temperature reached ({bed:.1f} °C); holding.",
                          printer_id=proc.printer_id, process_id=proc.id, actor="controller")
                db.session.commit()
                notify("target_reached", printer=proc.printer_name, material=proc.material_label, process=_label(proc),
                       details=f"Bed {bed:.1f} °C. Holding {target} °C for {proc.planned_duration_min} min.")
            elif (now - proc.started_at).total_seconds() > self._reach_timeout(proc):
                return self._begin_shutdown(proc, ProcessState.FAILED, f"Bed did not reach {target} °C in time "
                                                                       f"(last {bed} °C).")
            return None
        if proc.state == ProcessState.DRYING:  # holding
            if hours_since(proc.target_reached_at) * 60 >= (proc.planned_duration_min or 0):
                if proc.cool_ramp_c_per_h and proc.cool_end_c is not None and proc.cool_end_c < target:
                    proc.state, proc.cooldown_started_at = ProcessState.COOLING, now
                    log_event("process", f"Hold complete; controlled cool-down at {proc.cool_ramp_c_per_h:g} °C/h "
                              f"to {proc.cool_end_c:g} °C.", printer_id=proc.printer_id, process_id=proc.id,
                              actor="controller")
                    return None
                return self._begin_shutdown(proc, ProcessState.COMPLETED, "Hold complete; heater off to cool naturally.")
            return None
        if proc.state == ProcessState.COOLING:  # controlled ramp down
            desired = max(int(proc.cool_end_c), math.ceil(target - proc.cool_ramp_c_per_h
                                                          * hours_since(proc.cooldown_started_at)))
            if desired <= proc.cool_end_c:
                return self._begin_shutdown(proc, ProcessState.COMPLETED,
                                            f"Controlled cool-down reached {proc.cool_end_c:g} °C.")
            if desired < setpoint:
                self._send_setpoint(proc, desired)
        return None

    # -- supervision ------------------------------------------------------
    def _supervise(self, proc: Process) -> None:
        if proc.id in self.shutdowns:
            return self._drive_shutdown(proc)
        link = self.tm.links.get(proc.printer_id)
        live = link.live() if link else None
        now = time.monotonic()
        stale_s = int(Setting.get("safety.disconnect_timeout_s", 60))

        # 1. Communication: can we still see and command the printer?
        if live is None or not live["connected"] or live["age_s"] is None or live["age_s"] > max(stale_s, 30):
            first = proc.id not in self._comm_lost_at
            lost = self._comm_lost_at.setdefault(proc.id, now)
            if first:
                log_event("safety", f"Process #{proc.id}: lost contact with the printer while heating.",
                          level="critical", printer_id=proc.printer_id, process_id=proc.id, actor="controller")
                db.session.commit()
                notify("heater_off_unconfirmed", printer=proc.printer_name, material=proc.material_label,
                       process=_label(proc),
                       details="Contact with the printer was lost while the bed heater was on. The app cannot switch "
                               "it off until the printer is reachable again; it will retry automatically. "
                               "Check the printer physically.")
            self.pending_off.add(proc.printer_id)
            if now - lost > self.COMM_LOSS_FAIL_S:
                proc.state, proc.ended_at = ProcessState.FAILED, utcnow()
                proc.error = "Lost contact with the printer while heating; heater shutdown not confirmed."
            return None
        if proc.id in self._comm_lost_at:
            del self._comm_lost_at[proc.id]
            return self._begin_shutdown(proc, ProcessState.FAILED,
                                        "Contact with the printer was lost during heating; stopping as a precaution.")

        bed, target_now, gstate = live["bed_c"], live["bed_target_c"], live["gcode_state"]
        if bed is not None:
            proc.max_bed_observed_c = max(proc.max_bed_observed_c or bed, bed)

        # 2. A print started: the bed now belongs to the print job. Do not touch it.
        if gstate in PRINT_STATES:
            proc.state, proc.ended_at = ProcessState.FAILED, utcnow()
            proc.error = f"A print started on the printer (state {gstate}); supervision ended without sending heater-off."
            log_event("process", proc.error, level="warning", printer_id=proc.printer_id, process_id=proc.id,
                      actor="controller")
            db.session.commit()
            notify("process_ended_unexpectedly", printer=proc.printer_name, material=proc.material_label,
                   process=_label(proc), details=proc.error)
            return None

        # 3. Over-temperature: switch off immediately.
        limit = proc.max_temp_c  # over-temperature trip point, fixed at start
        if bed is not None and bed > limit:
            db.session.commit()
            notify("temp_limit_exceeded", printer=proc.printer_name, material=proc.material_label,
                   process=_label(proc),
                   details=f"Bed {bed:.1f} °C exceeded the limit {limit:.1f} °C (target {proc.target_bed_c:.0f} °C). "
                           "Switching heater off.")
            return self._begin_shutdown(proc, ProcessState.SAFETY_SHUTDOWN,
                                        f"Bed {bed:.1f} °C exceeded limit {limit:.1f} °C.")

        accept_deadline = self._accept_deadline.get(proc.id)
        setpoint = proc.current_setpoint_c if proc.current_setpoint_c is not None else proc.target_bed_c
        accepted = target_now is not None and abs(target_now - setpoint) < 0.5

        # 4. Did the firmware accept the command (first target, or a program step)?
        if accept_deadline is not None:
            if accepted:
                self._accept_deadline.pop(proc.id, None)
                if proc.id not in self._accepted_once:
                    self._accepted_once.add(proc.id)
                    log_event("process", f"Printer reported bed target {target_now:.0f} °C: command accepted.",
                              printer_id=proc.printer_id, process_id=proc.id, actor="controller")
            else:
                rejected = [r for r in self.tm.command_results.get(proc.printer_id, [])
                            if str(r.get("result")).lower() not in ("success", "none")]
                if rejected or now > accept_deadline:
                    reason = (f"printer replied result={rejected[-1].get('result')} reason={rejected[-1].get('reason')}"
                              if rejected else f"bed target still {target_now} after {self.ACCEPT_TIMEOUT_S} s")
                    self._accept_deadline.pop(proc.id, None)
                    return self._begin_shutdown(
                        proc, ProcessState.FAILED,
                        f"Firmware did not accept the heater command ({reason}). On Authorization-Control firmware "
                        "this requires LAN Only + Developer Mode.")
                return None
        elif not accepted:
            # Someone changed the target (e.g. on the touchscreen) - stop supervising this run safely.
            return self._begin_shutdown(proc, ProcessState.STOPPED,
                                        f"Bed target changed outside the app (now {target_now}); stopping.")

        hard_max = int(Setting.get("safety.max_heating_minutes", 720))
        if (utcnow() - proc.started_at).total_seconds() / self.MINUTE_S >= hard_max:
            return self._begin_shutdown(proc, ProcessState.SAFETY_SHUTDOWN,
                                        f"Absolute maximum heating time ({hard_max} min) reached.")
        if proc.kind == "annealing":
            return self._anneal_program(proc, bed)

        # 5. Heating -> holding.
        if proc.state == ProcessState.HEATING and bed is not None and bed >= proc.target_bed_c - self.TARGET_TOLERANCE_C:
            proc.state, proc.target_reached_at = ProcessState.DRYING, utcnow()
            log_event("process", f"Target reached ({bed:.1f} °C).", printer_id=proc.printer_id, process_id=proc.id,
                      actor="controller")
            db.session.commit()
            notify("target_reached", printer=proc.printer_name, material=proc.material_label,
                   process=_label(proc), details=f"Bed {bed:.1f} °C, target {proc.target_bed_c:.0f} °C.")
        elif proc.state == ProcessState.HEATING and (utcnow() - proc.started_at).total_seconds() > self._reach_timeout(proc):
            return self._begin_shutdown(proc, ProcessState.FAILED,
                                        f"Bed did not reach {proc.target_bed_c:.0f} °C within "
                                        f"{self._reach_timeout(proc) // 60:.0f} min (last {bed} °C).")

        # 6. Halfway reminder to flip the spool (drying only, once).
        if proc.kind == "drying" and proc.flip_reminder and proc.flip_notified_at is None \
                and proc.state == ProcessState.DRYING and proc.target_reached_at is not None:
            if proc.scheduled_end_at is not None:
                halfway = proc.target_reached_at + (proc.scheduled_end_at - proc.target_reached_at) / 2
            else:
                halfway = proc.target_reached_at + timedelta(seconds=(proc.planned_duration_min or 0) * self.MINUTE_S / 2)
            if utcnow() >= halfway:
                proc.flip_notified_at = utcnow()
                log_event("process", "Halfway point: flip-spool reminder sent.", printer_id=proc.printer_id,
                          process_id=proc.id, actor="controller")
                db.session.commit()
                notify("flip_spool", printer=proc.printer_name, material=proc.material_label, process=_label(proc),
                       details="Halfway through drying. Flip the spool over so the other side faces the bed, then "
                               "close the cover, door and lid. The heater stays on; the bed reading will dip and recover.")

        # 7. Planned duration (the absolute heating limit is checked above for every kind).
        elapsed_min = (utcnow() - proc.started_at).total_seconds() / self.MINUTE_S
        if proc.scheduled_end_at is not None:
            if utcnow() >= proc.scheduled_end_at:
                return self._begin_shutdown(proc, ProcessState.COMPLETED, "Scheduled stop time reached.")
        elif proc.kind == "drying":
            # Drying time counts from reaching the target, like a dryer/oven timer.
            if proc.target_reached_at is not None and \
                    (utcnow() - proc.target_reached_at).total_seconds() / self.MINUTE_S >= (proc.planned_duration_min or 0):
                return self._begin_shutdown(proc, ProcessState.COMPLETED, "Planned drying time completed.")
        elif elapsed_min >= (proc.planned_duration_min or 0):
            return self._begin_shutdown(proc, ProcessState.COMPLETED, "Planned duration completed.")
        return None

    def _reach_timeout(self, proc: Process) -> float:
        if proc.kind == "annealing":
            ramp_s = ((proc.target_bed_c - (proc.ramp_start_c or proc.target_bed_c)) / proc.heat_ramp_c_per_h
                      * 3600 * self.MINUTE_S / 60) if proc.heat_ramp_c_per_h else 0
            return ramp_s + self.REACH_TIMEOUT_DRYING_S
        return self.REACH_TIMEOUT_DRYING_S if proc.kind == "drying" else self.REACH_TIMEOUT_S

    # -- verified shutdown ----------------------------------------------
    def _begin_shutdown(self, proc: Process, final_state: str, reason: str) -> None:
        if proc.id in self.shutdowns:
            return
        self._accept_deadline.pop(proc.id, None)
        self.shutdowns[proc.id] = Shutdown(final_state, reason)
        proc.state = ProcessState.COOLING
        log_event("process" if final_state in (ProcessState.COMPLETED, ProcessState.STOPPED) else "safety",
                  f"Process #{proc.id}: switching heater off ({reason})",
                  level="info" if final_state == ProcessState.COMPLETED else "warning",
                  printer_id=proc.printer_id, process_id=proc.id, actor="controller")
        self._drive_shutdown(proc)

    def _drive_shutdown(self, proc: Process) -> None:
        sd = self.shutdowns[proc.id]
        link = self.tm.links.get(proc.printer_id)
        now = time.monotonic()
        live = link.live() if link else None

        if sd.sent_at is not None and live and live["connected"] and live["last_message_at"] \
                and live["last_message_at"] > sd.sent_at and live["bed_target_c"] == 0:
            return self._shutdown_confirmed(proc, sd)
        if sd.attempts >= self.OFF_MAX_ATTEMPTS and now - sd.last_sent >= self.OFF_RETRY_S:
            return self._shutdown_unconfirmed(proc, sd)
        if now - sd.last_sent >= self.OFF_RETRY_S or sd.attempts == 0:
            sd.attempts += 1
            sd.last_sent = now
            if link and link.send_bed(control.bed_off()):
                sd.sent_at = sd.sent_at or now
        return None

    def _shutdown_confirmed(self, proc: Process, sd: Shutdown) -> None:
        del self.shutdowns[proc.id]
        proc.state, proc.ended_at, proc.heater_off_verified = sd.final_state, utcnow(), True
        if sd.final_state != ProcessState.COMPLETED:
            proc.error = sd.reason
        self.pending_off.discard(proc.printer_id)
        log_event("process", f"Process #{proc.id} {sd.final_state}: heater off confirmed by printer report "
                  f"(attempt {sd.attempts}).", printer_id=proc.printer_id, process_id=proc.id, actor="controller")
        db.session.commit()
        event = {ProcessState.COMPLETED: "process_completed", ProcessState.STOPPED: "process_stopped"}.get(
            sd.final_state, "process_ended_unexpectedly")
        if sd.final_state == ProcessState.SAFETY_SHUTDOWN:
            event = "process_ended_unexpectedly"
        notify(event, printer=proc.printer_name, material=proc.material_label, process=_label(proc),
               details=f"{sd.reason} Heater off verified (bed target 0 reported). "
                       f"Max bed observed {proc.max_bed_observed_c or 0:.1f} °C.")

    def _shutdown_unconfirmed(self, proc: Process, sd: Shutdown) -> None:
        del self.shutdowns[proc.id]
        proc.state = ProcessState.SAFETY_SHUTDOWN if sd.final_state == ProcessState.SAFETY_SHUTDOWN \
            else ProcessState.FAILED
        proc.ended_at, proc.heater_off_verified = utcnow(), False
        proc.error = f"{sd.reason} Heater-off NOT confirmed after {sd.attempts} attempts."
        self.pending_off.add(proc.printer_id)
        log_event("safety", proc.error, level="critical", printer_id=proc.printer_id, process_id=proc.id,
                  actor="controller")
        db.session.commit()
        notify("heater_off_unconfirmed", printer=proc.printer_name, material=proc.material_label,
               process=_label(proc),
               details=proc.error + " The app will keep trying. Check the printer and switch the bed off on its "
                                    "touchscreen if needed.")

    def _handle_pending_off(self) -> None:
        """Printers that may still be heating from an unconfirmed/interrupted run: switch off when reachable."""
        for pid in list(self.pending_off):
            if any(p.printer_id == pid for p in Process.query.filter(Process.state.in_(ProcessState.ACTIVE))):
                continue  # an active process is handling it
            link = self.tm.links.get(pid)
            if not link or not link.connected:
                continue
            live = link.live()
            if live["gcode_state"] in PRINT_STATES:
                self.pending_off.discard(pid)  # a print owns the bed now
                continue
            if live["bed_target_c"] == 0 and live["age_s"] is not None and live["age_s"] < 30:
                self.pending_off.discard(pid)
                printer = db.session.get(Printer, pid)
                log_event("safety", f"'{printer.name if printer else pid}': heater confirmed off (bed target 0).",
                          level="warning", printer_id=pid, actor="controller")
                continue
            if time.monotonic() - self._pending_last.get(pid, 0) >= self.OFF_RETRY_S:
                self._pending_last[pid] = time.monotonic()
                link.send_bed(control.bed_off())
                if live["age_s"] is None or live["age_s"] > 30:
                    link.request("pushall")

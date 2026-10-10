"""Drying: review a printer + preset, then start now or schedule.

The web app only records the request; the controller daemon re-validates everything against live
printer data before sending anything, and supervises the run (process_runner).
"""
from dataclasses import dataclass
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..capabilities import has_chamber_sensor
from ..controller_status import controller_health
from ..extensions import db
from ..forms import DryingStartForm
from ..limits import MIN_BED_TARGET_C, bed_limits, effective_bed_max
from ..models import (CommandRequest, DryingPreset, Printer, Process, ProcessState, Schedule, VerificationStatus,
                      log_event, utcnow)
from ..safety import unattended_heating_allowed
from ..timeutil import fmt, local_to_utc

bp = Blueprint("drying", __name__, url_prefix="/drying")

SPOOL_SIZES = [250, 500, 1000]
IDLE_STATES = {"IDLE", "FINISH", "FAILED"}
HEATUP_BUDGET = timedelta(hours=1)  # used when checking schedules for overlap


@dataclass
class Gate:
    label: str
    ok: bool
    detail: str = ""
    kind: str = "required"  # required | scheduled | advisory


def open_process(printer_id: int):
    return Process.query.filter(Process.printer_id == printer_id,
                                Process.state.in_(ProcessState.ACTIVE | {ProcessState.PENDING})).first()


def readiness_gates(printer: Printer, preset: DryingPreset | None) -> list[Gate]:
    status = printer.status
    allowed, reason = printer.heater_control_allowed(current_app.config["ALLOW_HEATER_CONTROL"])
    gates = [
        Gate("Printer enabled", printer.enabled),
        Gate("Controller daemon running", controller_health().alive, "Heartbeat from bambu-fc-controller"),
        Gate("Printer online", bool(status and status.online)),
        Gate("Printer idle", bool(status and status.gcode_state in IDLE_STATES),
             f"Reported state: {status.gcode_state if status else 'unknown'}", kind="now"),
        Gate("Heater control verified on this printer + firmware", allowed, "" if allowed else reason),
        Gate("No other operation on this printer", open_process(printer.id) is None),
        Gate("Printer prepared: homed, bed lowered, toolhead parked", printer.motion_prepared_at is not None,
             "Use Prepare printer below with an empty bed" if printer.motion_prepared_at is None else ""),
    ]
    if preset:
        setting = preset.setting_for(printer.model)
        gates += [
            Gate(f"{printer.model} bed target entered for this preset", bool(setting and setting.bed_target_c),
                 "Set it on the preset page" if not (setting and setting.bed_target_c) else ""),
            Gate("Preset has a sourced reference", preset.verification_status != VerificationStatus.UNSOURCED,
                 f"Status: {preset.verification_status}", kind="advisory"),
            Gate(f"{printer.model} setting verified by test",
                 bool(setting and setting.verification_status == VerificationStatus.VERIFIED),
                 "You can still start by accepting it as unverified", kind="advisory"),
        ]
    unattended_ok, unattended_reason = unattended_heating_allowed()
    gates.append(Gate("Unattended heating permitted", unattended_ok, unattended_reason, kind="scheduled"))
    return gates


def _context(printer, preset):
    setting = preset.setting_for(printer.model) if preset else None
    gates = readiness_gates(printer, preset)
    return {
        "setting": setting,
        "limits": bed_limits(printer, preset, setting),
        "effective_max": effective_bed_max(printer, preset, setting),
        "gates": gates,
        "ready_now": all(g.ok for g in gates if g.kind in ("required", "now")),
        "ready_scheduled": all(g.ok for g in gates if g.kind in ("required", "scheduled")),
        "chamber_sensor": has_chamber_sensor(printer.model),
    }


@bp.route("/")
def index():
    printers = Printer.query.order_by(Printer.name).all()
    presets = DryingPreset.query.order_by(DryingPreset.manufacturer, DryingPreset.product_name).all()
    printer = db.session.get(Printer, request.args.get("printer_id", type=int) or 0)
    preset = db.session.get(DryingPreset, request.args.get("preset_id", type=int) or 0)
    spool = request.args.get("spool_g", type=int)
    review = _context(printer, preset) if printer else None
    form = None
    if printer and preset:
        form = DryingStartForm(formdata=None, printer_id=printer.id, preset_id=preset.id, spool_size_g=spool,
                               target_bed_c=int(review["setting"].bed_target_c) if review["setting"]
                               and review["setting"].bed_target_c else None,
                               # Bed-drying time for this printer; the oven time is not a substitute.
                               duration_h=review["setting"].drying_time_h if review["setting"] else None)
    active = Process.query.filter(Process.kind == "drying",
                                  Process.state.in_(ProcessState.ACTIVE | {ProcessState.PENDING})).all()
    return render_template("drying/index.html", printers=printers, presets=presets, printer=printer, preset=preset,
                           spool=spool, spool_sizes=SPOOL_SIZES, review=review, form=form, active=active)


@bp.route("/start", methods=["POST"])
def start():
    form = DryingStartForm()
    printer = db.session.get(Printer, int(form.printer_id.data or 0)) or abort(404)
    preset = db.session.get(DryingPreset, int(form.preset_id.data or 0)) or abort(404)
    back = redirect(url_for("drying.index", printer_id=printer.id, preset_id=preset.id,
                            spool_g=form.spool_size_g.data or ""))
    if not form.validate_on_submit():
        for name, errs in form.errors.items():
            flash(f"{getattr(form, name).label.text}: {'; '.join(errs)}", "danger")
        return back
    ctx = _context(printer, preset)
    setting = ctx["setting"]
    scheduled = form.start_mode.data == "scheduled"
    errors = []
    if not (ctx["ready_scheduled"] if scheduled else ctx["ready_now"]):
        errors.append("Not all required checks are met (see Safety gates).")
    target = form.target_bed_c.data
    if not (MIN_BED_TARGET_C <= target <= ctx["effective_max"]):
        errors.append(f"Bed target must be {MIN_BED_TARGET_C}–{ctx['effective_max']:.0f} °C (lowest applicable limit).")
    if not (setting and setting.verification_status == VerificationStatus.VERIFIED) and not form.accept_unverified.data:
        errors.append("Tick the box accepting the unverified printer setting.")
    now = utcnow()
    start_at = local_to_utc(form.start_at.data) if scheduled and form.start_at.data else None
    end_at = local_to_utc(form.end_at.data) if form.stop_mode.data == "end_time" and form.end_at.data else None
    duration_min = int(round(form.duration_h.data * 60)) if form.stop_mode.data == "duration" and form.duration_h.data \
        else None
    if scheduled and (start_at is None or start_at <= now + timedelta(minutes=1)):
        errors.append("Choose a start time in the future.")
    if form.stop_mode.data == "duration" and not duration_min:
        errors.append("Enter a drying time.")
    if form.stop_mode.data == "end_time":
        if end_at is None or end_at <= (start_at or now) + timedelta(minutes=10):
            errors.append("The stop time must be at least 10 minutes after the start.")
        if form.repeat_rule.data:
            errors.append("Repeating schedules must use a drying time, not a fixed stop time.")
    if errors:
        for e in errors:
            flash(e, "danger")
        return back

    actor = current_user.username
    if not scheduled:
        proc = Process(kind="drying", printer_id=printer.id, drying_preset_id=preset.id, printer_name=printer.name,
                       material_label=preset.label, spool_size_g=form.spool_size_g.data, state=ProcessState.PENDING,
                       target_bed_c=target, planned_duration_min=duration_min, scheduled_end_at=end_at,
                       prep_confirmed_at=now, prep_confirmed_by=actor, flip_reminder=form.flip_reminder.data)
        db.session.add(proc)
        db.session.flush()
        db.session.add(CommandRequest(printer_id=printer.id, process_id=proc.id, command="start_drying",
                                      requested_by=actor))
        log_event("process", f"Drying #{proc.id} requested: {preset.label} at {target} °C "
                  f"{'until ' + fmt(end_at) if end_at else f'for {duration_min} min after reaching target'}.",
                  level="warning", printer_id=printer.id, process_id=proc.id, actor=actor)
        db.session.commit()
        flash("Drying requested. The controller re-checks the printer before switching the heater on.", "info")
        return redirect(url_for("drying.index"))

    window_end = (end_at or start_at + timedelta(minutes=duration_min)) + HEATUP_BUDGET
    for other in Schedule.query.filter_by(printer_id=printer.id, enabled=True).all():
        other_end = (other.end_at or other.start_at + timedelta(minutes=other.duration_min or 0)) + HEATUP_BUDGET
        if other.start_at < window_end and start_at < other_end:
            flash(f"Overlaps schedule #{other.id} on this printer ({fmt(other.start_at)}).", "danger")
            return back
    sch = Schedule(printer_id=printer.id, drying_preset_id=preset.id, start_at=start_at, duration_min=duration_min,
                   end_at=end_at, repeat_rule=form.repeat_rule.data or None, target_bed_c=target,
                   spool_size_g=form.spool_size_g.data, flip_reminder=form.flip_reminder.data,
                   prep_confirmed_for=start_at, prep_confirmed_by=actor, created_by=actor)
    db.session.add(sch)
    db.session.flush()
    log_event("process", f"Schedule #{sch.id} created: {preset.label} at {target} °C, first start {fmt(start_at)}"
              f"{', repeats ' + sch.repeat_rule if sch.repeat_rule else ''}.", printer_id=printer.id, actor=actor)
    db.session.commit()
    flash(f"Scheduled for {fmt(start_at)}." + (" Each repeat needs its own preparation confirmation on the "
                                                "Schedules page." if sch.repeat_rule else ""), "success")
    return redirect(url_for("schedules.index"))


@bp.route("/<int:process_id>/stop", methods=["POST"])
def stop(process_id):
    proc = db.session.get(Process, process_id) or abort(404)
    db.session.add(CommandRequest(printer_id=proc.printer_id, process_id=proc.id, command="stop",
                                  requested_by=current_user.username))
    log_event("process", f"Stop requested for {proc.kind} #{proc.id}.", level="warning", printer_id=proc.printer_id,
              process_id=proc.id, actor=current_user.username)
    db.session.commit()
    flash("Stop requested. The heater is switched off and the printer's report is checked to confirm it.", "warning")
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url)
                    else url_for("drying.index"))

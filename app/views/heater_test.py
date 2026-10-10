"""Phase 4: attended heater tests. The web app only queues requests; the controller daemon
re-validates everything and is the only process that talks to the printer."""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, url_for
from flask_login import current_user

from ..controller_status import controller_health
from ..extensions import db
from ..forms import HeaterTestForm, PrintHoursAfterForm
from ..models import CommandRequest, Printer, Process, ProcessState, log_event, utcnow

bp = Blueprint("heater", __name__, url_prefix="/printers/<int:printer_id>/heater-test")

IDLE_STATES = {"IDLE", "FINISH", "FAILED"}
# The bed must start at least 5 °C below the target so heating is actually observed (checked on submit
# against the chosen target); this precondition uses the highest allowed test target (50 °C).
MAX_START_BED_C = 45


def _printer(printer_id) -> Printer:
    return db.session.get(Printer, printer_id) or abort(404)


def _open_process(printer: Printer):
    return (Process.query.filter(Process.printer_id == printer.id,
                                 Process.state.in_(ProcessState.ACTIVE | {ProcessState.PENDING}))
            .order_by(Process.id.desc()).first())


def preconditions(printer: Printer) -> list[tuple[str, bool, str]]:
    s = printer.status
    return [
        ("Heater control switched on for the server", current_app.config["ALLOW_HEATER_CONTROL"],
         "BFC_ALLOW_HEATER_CONTROL=true in the env file"),
        ("Controller daemon running", controller_health().alive, ""),
        ("Printer enabled", printer.enabled, ""),
        ("Printer online", bool(s and s.online), s.last_error if s and s.last_error else ""),
        ("Printer idle", bool(s and s.gcode_state in IDLE_STATES), f"state: {s.gcode_state if s else 'unknown'}"),
        ("Bed heater currently off", bool(s and (s.bed_target_c or 0) == 0),
         f"bed target: {s.bed_target_c if s else '?'}"),
        (f"Bed cool enough to observe heating (≤ {MAX_START_BED_C} °C)",
         bool(s and s.bed_temp_c is not None and s.bed_temp_c <= MAX_START_BED_C),
         f"bed now: {s.bed_temp_c:.1f} °C" if s and s.bed_temp_c is not None else "bed: unknown"),
        ("Firmware version reported", bool(printer.firmware_version_reported), printer.firmware_version_reported or ""),
        ("No other operation on this printer", _open_process(printer) is None, ""),
    ]


@bp.route("/", methods=["GET", "POST"])
def index(printer_id):
    printer = _printer(printer_id)
    form = HeaterTestForm()
    checks = preconditions(printer)
    ready = all(ok for _, ok, _ in checks)
    if form.validate_on_submit():
        bed = printer.status.bed_temp_c if printer.status else None
        if ready and bed is not None and bed > form.target_c.data - 5:
            flash(f"The bed is at {bed:.1f} °C; choose a target at least 5 °C above it, or let it cool.", "danger")
        elif not ready:
            flash("Preconditions not met; nothing was sent.", "danger")
        else:
            proc = Process(kind="heater_test", printer_id=printer.id, printer_name=printer.name,
                           material_label="(heater test, empty bed)", state=ProcessState.PENDING,
                           target_bed_c=form.target_c.data, planned_duration_min=form.minutes.data,
                           print_hours_before=form.print_hours_before.data,
                           prep_confirmed_at=utcnow(), prep_confirmed_by=current_user.username)
            db.session.add(proc)
            db.session.flush()
            db.session.add(CommandRequest(printer_id=printer.id, process_id=proc.id, command="start_heater_test",
                                          requested_by=current_user.username))
            log_event("process", f"Heater test #{proc.id} requested: {form.target_c.data} °C for "
                      f"{form.minutes.data} min (print hours before: {form.print_hours_before.data}).",
                      level="warning", printer_id=printer.id, process_id=proc.id, actor=current_user.username)
            db.session.commit()
            flash("Test requested. The controller re-checks everything before sending anything.", "info")
            return redirect(url_for("heater.index", printer_id=printer.id))
    tests = Process.query.filter_by(printer_id=printer.id, kind="heater_test").order_by(Process.id.desc()).all()
    return render_template("printers/heater_test.html", printer=printer, form=form, checks=checks, ready=ready,
                           active=_open_process(printer), tests=tests, hours_form=PrintHoursAfterForm(),
                           can_verify=_verifiable_test(printer))


@bp.route("/<int:process_id>/stop", methods=["POST"])
def stop(printer_id, process_id):
    proc = db.session.get(Process, process_id) or abort(404)
    if proc.printer_id != printer_id:
        abort(404)
    db.session.add(CommandRequest(printer_id=printer_id, process_id=proc.id, command="stop",
                                  requested_by=current_user.username))
    log_event("process", f"Stop requested for process #{proc.id}.", level="warning", printer_id=printer_id,
              process_id=proc.id, actor=current_user.username)
    db.session.commit()
    flash("Stop requested. The heater is switched off and the printer's report is checked to confirm it.", "warning")
    return redirect(url_for("heater.index", printer_id=printer_id))


@bp.route("/<int:process_id>/hours", methods=["POST"])
def record_hours(printer_id, process_id):
    proc = db.session.get(Process, process_id) or abort(404)
    form = PrintHoursAfterForm()
    if proc.printer_id != printer_id or proc.state not in ProcessState.TERMINAL or not form.validate_on_submit():
        flash("Could not record print hours.", "danger")
        return redirect(url_for("heater.index", printer_id=printer_id))
    proc.print_hours_after = form.print_hours_after.data
    delta = proc.print_hours_after - (proc.print_hours_before or 0)
    printer = _printer(printer_id)
    minutes = (proc.ended_at - proc.started_at).total_seconds() / 60 if proc.started_at and proc.ended_at else 0
    verdict = ("UNCHANGED: idle heating did not add print hours" if delta == 0 else
               f"CHANGED by {delta:g} h: idle heating appears to add print hours")
    note = (f"Test #{proc.id} ({minutes:.0f} min heated, firmware {proc.firmware_at_start}): "
            f"{proc.print_hours_before:g} → {proc.print_hours_after:g} h, {verdict}.")
    if delta == 0 and minutes < 60:
        note += " (Heated < 60 min, so an unchanged whole-hour counter is not conclusive.)"
    printer.print_hours_test_notes = "\n".join(filter(None, [printer.print_hours_test_notes, note]))
    log_event("process", note, level="info", printer_id=printer_id, process_id=proc.id, actor=current_user.username)
    db.session.commit()
    flash(note, "info" if delta == 0 else "warning")
    return redirect(url_for("heater.index", printer_id=printer_id))


def _verifiable_test(printer: Printer):
    """Latest successful test on the printer's current firmware, if heater control isn't verified for it yet."""
    if printer.heater_control_verified and printer.heater_control_verified_firmware == printer.firmware_version_reported:
        return None
    return (Process.query.filter_by(printer_id=printer.id, kind="heater_test", state=ProcessState.COMPLETED,
                                    heater_off_verified=True, firmware_at_start=printer.firmware_version_reported)
            .order_by(Process.id.desc()).first()) if printer.firmware_version_reported else None


@bp.route("/verify", methods=["POST"])
def mark_verified(printer_id):
    printer = _printer(printer_id)
    test = _verifiable_test(printer)
    if test is None:
        flash("No completed test with confirmed heater shutdown on the current firmware.", "danger")
        return redirect(url_for("heater.index", printer_id=printer_id))
    printer.heater_control_verified = True
    printer.heater_control_verified_firmware = printer.firmware_version_reported
    printer.heater_control_verified_at = utcnow()
    log_event("safety", f"Heater control marked VERIFIED for '{printer.name}' on firmware "
              f"{printer.firmware_version_reported} based on test #{test.id}.", level="warning",
              printer_id=printer.id, process_id=test.id, actor=current_user.username)
    db.session.commit()
    flash("Heater control marked verified for this printer and firmware. A firmware update revokes it.", "success")
    return redirect(url_for("printers.detail", printer_id=printer_id))

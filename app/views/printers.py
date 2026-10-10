import json
import time
from pathlib import Path

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_file,
                   url_for)
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from ..capabilities import bed_max_c, has_chamber_sensor, profile_for
from ..extensions import db
from ..forms import PrinterForm
from ..models import CommandRequest, EventLog, Printer, log_event, utcnow

bp = Blueprint("printers", __name__, url_prefix="/printers")


def _get(printer_id: int) -> Printer:
    return db.session.get(Printer, printer_id) or abort(404)


@bp.route("/")
def index():
    printers = Printer.query.order_by(Printer.name).all()
    return render_template("printers/index.html", printers=printers)


@bp.route("/<int:printer_id>")
def detail(printer_id):
    printer = _get(printer_id)
    allowed, reason = printer.heater_control_allowed(current_app.config["ALLOW_HEATER_CONTROL"])
    events = EventLog.query.filter_by(printer_id=printer.id).order_by(EventLog.ts.desc()).limit(25).all()
    return render_template(
        "printers/detail.html",
        printer=printer,
        profile=profile_for(printer.model),
        chamber_sensor=has_chamber_sensor(printer.model),
        heater_allowed=allowed,
        heater_reason=reason,
        events=events,
    )


def _save(form: PrinterForm, printer: Printer, is_new: bool):
    if is_new and not form.access_code.data:
        form.access_code.errors.append("Required for a new printer.")
        return None
    spec_max = bed_max_c(form.model.data)
    if form.max_bed_temp_c.data and form.max_bed_temp_c.data > spec_max:
        form.max_bed_temp_c.errors.append(f"Cannot exceed the {form.model.data} spec maximum of {spec_max} °C.")
        return None

    model_changed = not is_new and printer.model != form.model.data
    printer.name = form.name.data.strip()
    printer.model = form.model.data
    printer.ip_address = form.ip_address.data.strip()
    if not is_new and printer.serial != form.serial.data:
        printer.tls_fingerprint = None  # different device; re-pin certificate
    printer.serial = form.serial.data
    if form.access_code.data:
        printer.access_code = form.access_code.data
    printer.firmware_version = form.firmware_version.data or None
    printer.network_mode = form.network_mode.data or None
    printer.max_bed_temp_c = form.max_bed_temp_c.data
    printer.notes = form.notes.data or None
    if model_changed and printer.heater_control_verified:
        printer.heater_control_verified = False
        flash("Model changed: heater-control verification revoked.", "warning")
    db.session.add(printer)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        flash("A printer with that name or serial number already exists.", "danger")
        return None
    log_event("printer", f"Printer '{printer.name}' {'added' if is_new else 'updated'}.",
              printer_id=printer.id, actor=current_user.username)
    db.session.commit()
    return printer


@bp.route("/new", methods=["GET", "POST"])
def create():
    form = PrinterForm()
    if form.validate_on_submit():
        printer = _save(form, Printer(enabled=False), is_new=True)
        if printer:
            flash(f"Printer '{printer.name}' added (disabled). Enable it when ready for read-only connection.",
                  "success")
            return redirect(url_for("printers.detail", printer_id=printer.id))
    return render_template("printers/form.html", form=form, printer=None)


@bp.route("/<int:printer_id>/edit", methods=["GET", "POST"])
def edit(printer_id):
    printer = _get(printer_id)
    form = PrinterForm(obj=printer)
    if form.validate_on_submit():
        if _save(form, printer, is_new=False):
            flash("Printer updated.", "success")
            return redirect(url_for("printers.detail", printer_id=printer.id))
    form.access_code.data = ""
    return render_template("printers/form.html", form=form, printer=printer)


@bp.route("/<int:printer_id>/toggle", methods=["POST"])
def toggle(printer_id):
    printer = _get(printer_id)
    printer.enabled = not printer.enabled
    log_event("printer", f"Printer '{printer.name}' {'enabled' if printer.enabled else 'disabled'}.",
              printer_id=printer.id, actor=current_user.username)
    db.session.commit()
    flash(f"'{printer.name}' {'enabled' if printer.enabled else 'disabled'}.", "info")
    return redirect(url_for("printers.index"))


@bp.route("/<int:printer_id>/reset-pin", methods=["POST"])
def reset_pin(printer_id):
    printer = _get(printer_id)
    if printer.enabled:
        flash("Disable the printer first; the new certificate is pinned when you re-enable it.", "warning")
        return redirect(url_for("printers.detail", printer_id=printer.id))
    printer.tls_fingerprint = None
    log_event("printer", f"Pinned TLS certificate for '{printer.name}' cleared.", level="warning",
              printer_id=printer.id, actor=current_user.username)
    db.session.commit()
    flash("Pinned certificate cleared. It will be re-pinned on the next connection.", "info")
    return redirect(url_for("printers.detail", printer_id=printer.id))


@bp.route("/<int:printer_id>/delete", methods=["POST"])
def delete(printer_id):
    printer = _get(printer_id)
    name = printer.name
    if printer.enabled:
        flash("Disable the printer before deleting it.", "warning")
        return redirect(url_for("printers.detail", printer_id=printer.id))
    db.session.delete(printer)
    log_event("printer", f"Printer '{name}' deleted.", actor=current_user.username)
    db.session.commit()
    flash(f"Deleted '{name}'.", "info")
    return redirect(url_for("printers.index"))


def latest_prepare(printer_id: int):
    return (CommandRequest.query.filter_by(printer_id=printer_id, command="prepare_motion")
            .order_by(CommandRequest.id.desc()).first())


@bp.route("/<int:printer_id>/prepare", methods=["POST"])
def prepare(printer_id):
    printer = _get(printer_id)
    back = request.referrer if request.referrer and request.referrer.startswith(request.host_url) \
        else url_for("printers.detail", printer_id=printer.id)
    if not request.form.get("bed_empty"):
        flash("Confirm the bed is empty first: homing raises the bed up to the nozzle.", "danger")
        return redirect(back)
    db.session.add(CommandRequest(printer_id=printer.id, command="prepare_motion", requested_by=current_user.username))
    log_event("printer", f"Prepare (home, lower bed, park) requested for '{printer.name}' with the bed confirmed empty.",
              printer_id=printer.id, actor=current_user.username)
    db.session.commit()
    flash("Preparing the printer: it will home, lower the bed and park the toolhead. Watch it, then confirm below.",
          "info")
    return redirect(back)


@bp.route("/<int:printer_id>/prepare/confirm", methods=["POST"])
def prepare_confirm(printer_id):
    printer = _get(printer_id)
    back = request.referrer if request.referrer and request.referrer.startswith(request.host_url) \
        else url_for("printers.detail", printer_id=printer.id)
    cmd = latest_prepare(printer.id)
    if not request.form.get("bed_lowered") or cmd is None or cmd.status not in ("sent", "confirmed"):
        flash("Nothing to confirm: prepare the printer first, and tick the box once the bed is down.", "danger")
        return redirect(back)
    printer.motion_prepared_at, printer.motion_prepared_by = utcnow(), current_user.username
    log_event("printer", f"'{printer.name}' confirmed homed, bed lowered and toolhead parked.", printer_id=printer.id,
              actor=current_user.username)
    db.session.commit()
    flash("Printer prepared. Load the bed, then start.", "success")
    return redirect(back)


@bp.route("/<int:printer_id>/camera.jpg")
def camera_snapshot(printer_id):
    """Latest camera frame. Each request also tells the camera service someone is watching."""
    printer = _get(printer_id)
    cam_dir = Path(current_app.config["CAMERA_DIR"])
    try:
        cam_dir.mkdir(parents=True, exist_ok=True)
        (cam_dir / f"{printer.id}.want").touch()
        status = json.loads((cam_dir / f"{printer.id}.json").read_text())
    except (OSError, ValueError):
        status = {"state": "starting"}
    if not printer.enabled:
        status = {"state": "disabled", "error": "Printer is disabled."}
    frame = cam_dir / f"{printer.id}.jpg"
    fresh = frame.exists() and time.time() - frame.stat().st_mtime < 10 and status.get("state") == "streaming"
    if not fresh:
        msg = status.get("error") or {"connecting": "Connecting to the camera…", "starting": "Starting the camera…",
                                      "reconnecting": "Reconnecting…", "stopped": "Starting the camera…"}.get(
            status.get("state"), "Waiting for the camera…")
        return Response(msg, status=503, mimetype="text/plain", headers={"Cache-Control": "no-store"})
    resp = send_file(frame, mimetype="image/jpeg", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp

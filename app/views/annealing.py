"""Annealing: presets, and best-effort annealing programs run on a printer bed
(ramp up, hold, controlled ramp down). The controller daemon re-validates and supervises each run."""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..forms import AnnealingPresetForm, AnnealingStartForm
from ..limits import MIN_BED_TARGET_C, OVERSHOOT_C, bed_limits
from ..models import AnnealingPreset, CommandRequest, Printer, Process, ProcessState, Setting, log_event, utcnow
from .drying import IDLE_STATES, Gate, open_process

bp = Blueprint("annealing", __name__, url_prefix="/annealing")


def annealing_limits(printer: Printer, preset: AnnealingPreset | None) -> dict[str, float]:
    limits = bed_limits(printer)
    if preset is not None and preset.max_temp_c:
        limits["Preset maximum"] = float(preset.max_temp_c)
    return limits


def plan_minutes(temp, hold_min, heat_ramp, cool_ramp, cool_end, start_c=25.0) -> dict[str, float]:
    ramp_up = (temp - start_c) / heat_ramp * 60 if heat_ramp else 0
    cool = (temp - cool_end) / cool_ramp * 60 if cool_ramp and cool_end is not None and cool_end < temp else 0
    return {"ramp_up": max(0, ramp_up), "hold": hold_min, "cool": max(0, cool),
            "total": max(0, ramp_up) + hold_min + max(0, cool)}


def gates(printer: Printer) -> list[Gate]:
    s = printer.status
    allowed, reason = printer.heater_control_allowed(current_app.config["ALLOW_HEATER_CONTROL"])
    return [
        Gate("Printer online", bool(s and s.online)),
        Gate("Printer idle", bool(s and s.gcode_state in IDLE_STATES), f"state: {s.gcode_state if s else 'unknown'}"),
        Gate("Heater control verified on this printer + firmware", allowed, "" if allowed else reason),
        Gate("No other operation on this printer", open_process(printer.id) is None),
        Gate("Printer prepared: homed, bed lowered, toolhead parked", printer.motion_prepared_at is not None,
             "Use Prepare printer below with an empty bed" if printer.motion_prepared_at is None else ""),
    ]


@bp.route("/")
def index():
    presets = AnnealingPreset.query.order_by(AnnealingPreset.is_template.desc(), AnnealingPreset.manufacturer,
                                             AnnealingPreset.material).all()
    printers = Printer.query.order_by(Printer.name).all()
    printer = db.session.get(Printer, request.args.get("printer_id", type=int) or 0)
    preset = db.session.get(AnnealingPreset, request.args.get("preset_id", type=int) or 0)
    review = form = None
    if printer:
        limits = annealing_limits(printer, preset)
        g = gates(printer)
        review = {"limits": limits, "effective_max": min(limits.values()), "gates": g, "ready": all(x.ok for x in g)}
    if printer and preset:
        temp = int(min(preset.anneal_temp_c, review["effective_max"])) if preset.anneal_temp_c else None
        form = AnnealingStartForm(formdata=None, printer_id=printer.id, preset_id=preset.id, anneal_temp_c=temp,
                                  hold_h=round(preset.hold_minutes / 60, 2) if preset.hold_minutes else None,
                                  heat_ramp_c_per_h=preset.heat_ramp_c_per_h,
                                  cool_ramp_c_per_h=preset.cool_ramp_c_per_h, cool_end_c=40)
        if preset.anneal_temp_c and preset.anneal_temp_c > review["effective_max"]:
            review["clamped"] = preset.anneal_temp_c
        if temp and preset.hold_minutes:
            review["plan"] = plan_minutes(temp, preset.hold_minutes, preset.heat_ramp_c_per_h,
                                          preset.cool_ramp_c_per_h, 40)
    active = Process.query.filter(Process.kind == "annealing",
                                  Process.state.in_(ProcessState.ACTIVE | {ProcessState.PENDING})).all()
    recent = (Process.query.filter_by(kind="annealing").filter(Process.state.in_(ProcessState.TERMINAL))
              .order_by(Process.id.desc()).limit(10).all())
    return render_template("annealing/index.html", presets=presets, printers=printers, printer=printer, preset=preset,
                           review=review, form=form, active=active, recent=recent,
                           max_heating=int(Setting.get("safety.max_heating_minutes", 720)), overshoot=OVERSHOOT_C)


@bp.route("/start", methods=["POST"])
def start():
    form = AnnealingStartForm()
    printer = db.session.get(Printer, int(form.printer_id.data or 0)) or abort(404)
    preset = db.session.get(AnnealingPreset, int(form.preset_id.data or 0)) or abort(404)
    back = redirect(url_for("annealing.index", printer_id=printer.id, preset_id=preset.id))
    if not form.validate_on_submit():
        for name, errs in form.errors.items():
            flash(f"{getattr(form, name).label.text}: {'; '.join(errs)}", "danger")
        return back
    limits = annealing_limits(printer, preset)
    errors = []
    if not all(g.ok for g in gates(printer)):
        errors.append("Not all required checks are met.")
    temp = form.anneal_temp_c.data
    if not (MIN_BED_TARGET_C <= temp <= min(limits.values())):
        errors.append(f"Annealing temperature must be {MIN_BED_TARGET_C}–{min(limits.values()):.0f} °C "
                      "(lowest applicable limit).")
    cool_end = form.cool_end_c.data if form.cool_end_c.data is not None else 40
    if form.cool_ramp_c_per_h.data and cool_end >= temp:
        errors.append("The cool-down end temperature must be below the annealing temperature.")
    hold_min = int(round(form.hold_h.data * 60))
    plan = plan_minutes(temp, hold_min, form.heat_ramp_c_per_h.data, form.cool_ramp_c_per_h.data, cool_end,
                        start_c=(printer.status.bed_temp_c if printer.status and printer.status.bed_temp_c else 25))
    max_heating = int(Setting.get("safety.max_heating_minutes", 720))
    if plan["total"] > max_heating:
        errors.append(f"This program heats for about {plan['total'] / 60:.1f} h; the safety limit is "
                      f"{max_heating / 60:.1f} h. Raise it in Settings → Safety or shorten the program.")
    if errors:
        for e in errors:
            flash(e, "danger")
        return back
    proc = Process(kind="annealing", printer_id=printer.id, annealing_preset_id=preset.id, printer_name=printer.name,
                   material_label=f"{preset.manufacturer} {preset.material} (annealing)", state=ProcessState.PENDING,
                   target_bed_c=temp, planned_duration_min=hold_min, heat_ramp_c_per_h=form.heat_ramp_c_per_h.data,
                   cool_ramp_c_per_h=form.cool_ramp_c_per_h.data,
                   cool_end_c=cool_end if form.cool_ramp_c_per_h.data else None,
                   prep_confirmed_at=utcnow(), prep_confirmed_by=current_user.username)
    db.session.add(proc)
    db.session.flush()
    db.session.add(CommandRequest(printer_id=printer.id, process_id=proc.id, command="start_annealing",
                                  requested_by=current_user.username))
    log_event("process", f"Annealing #{proc.id} requested: {preset.manufacturer} {preset.material}, {temp} °C hold "
              f"{hold_min} min, about {plan['total'] / 60:.1f} h total.", level="warning", printer_id=printer.id,
              process_id=proc.id, actor=current_user.username)
    db.session.commit()
    flash("Annealing requested. The controller re-checks the printer before switching the heater on.", "info")
    return redirect(url_for("annealing.index"))


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<int:preset_id>/edit", methods=["GET", "POST"])
def edit(preset_id=None):
    preset = (db.session.get(AnnealingPreset, preset_id) or abort(404)) if preset_id else AnnealingPreset()
    form = AnnealingPresetForm(obj=preset)
    if form.validate_on_submit():
        form.populate_obj(preset)
        db.session.add(preset)
        log_event("preset", f"Annealing preset '{preset.manufacturer} {preset.material}' saved.",
                  actor=current_user.username)
        db.session.commit()
        flash("Annealing preset saved.", "success")
        return redirect(url_for("annealing.index"))
    return render_template("annealing/form.html", form=form, preset=preset)


@bp.route("/<int:preset_id>/delete", methods=["POST"])
def delete(preset_id):
    preset = db.session.get(AnnealingPreset, preset_id) or abort(404)
    if preset.is_template:
        flash("Templates cannot be deleted.", "warning")
    else:
        db.session.delete(preset)
        db.session.commit()
        flash("Deleted.", "info")
    return redirect(url_for("annealing.index"))

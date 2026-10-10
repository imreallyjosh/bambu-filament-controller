"""JSON endpoints for live page updates (login required like every page)."""
from datetime import timedelta

from flask import Blueprint, abort, jsonify, request

from ..capabilities import has_chamber_sensor
from ..extensions import db
from ..models import Printer, Process, ProcessState, TelemetrySample, utcnow
from ..printjob import job_info
from ..timeutil import ago, fmt, to_local

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.route("/status")
def status():
    out = []
    busy = {pid for (pid,) in db.session.query(Process.printer_id).filter(
        Process.state.in_(ProcessState.ACTIVE | {ProcessState.PENDING}))}
    for p in Printer.query.order_by(Printer.name).all():
        s = p.status
        job = job_info(s.raw_json) if (s and p.enabled) else None
        if job and not job["active"] and p.id in busy:
            job = None  # an old finished print is just clutter during a drying/annealing run
        out.append({
            "id": p.id,
            "enabled": p.enabled,
            "online": bool(s and s.online),
            "gcode_state": s.gcode_state if s else None,
            "bed_temp_c": s.bed_temp_c if s else None,
            "bed_target_c": s.bed_target_c if s else None,
            "nozzle_temp_c": s.nozzle_temp_c if s else None,
            "chamber_temp_c": s.chamber_temp_c if (s and has_chamber_sensor(p.model)) else None,
            "last_seen": ago(s.last_seen_at) if s else "never",
            "last_error": s.last_error if s else None,
            "firmware": p.firmware_version_reported,
            "job": job,
        })
    return jsonify(printers=out, server_time=fmt(utcnow()))


@bp.route("/printers/<int:printer_id>/telemetry")
def telemetry(printer_id):
    printer = db.session.get(Printer, printer_id) or abort(404)
    hours = min(max(request.args.get("hours", 6, type=int), 1), 24 * 7)
    rows = (TelemetrySample.query.filter(TelemetrySample.printer_id == printer.id,
                                         TelemetrySample.ts >= utcnow() - timedelta(hours=hours))
            .order_by(TelemetrySample.ts).all())
    chamber = has_chamber_sensor(printer.model)
    return jsonify(
        labels=[to_local(r.ts).strftime("%m-%d %H:%M") for r in rows],
        series={
            "Bed (heater reading)": [r.bed_temp_c for r in rows],
            "Bed target": [r.bed_target_c for r in rows],
            "Nozzle": [r.nozzle_temp_c for r in rows],
            **({"Chamber (printer sensor)": [r.chamber_temp_c for r in rows]} if chamber else {}),
            **({"Enclosure (independent sensor)": [r.enclosure_temp_c for r in rows]}
               if any(r.enclosure_temp_c is not None for r in rows) else {}),
        },
    )


def process_timing(proc: Process) -> dict:
    """Elapsed/remaining/progress. Drying time counts from reaching the target unless a stop time is set."""
    now = utcnow()
    end = proc.ended_at or now
    out = {"elapsed_s": None, "remaining_s": None, "progress": None, "scheduled_end": None, "note": ""}
    if not proc.started_at:
        return out
    out["elapsed_s"] = int((end - proc.started_at).total_seconds())
    if proc.scheduled_end_at:
        total = (proc.scheduled_end_at - proc.started_at).total_seconds()
        out["scheduled_end"] = fmt(proc.scheduled_end_at)
        if not proc.ended_at:
            out["remaining_s"] = max(0, int((proc.scheduled_end_at - now).total_seconds()))
        out["progress"] = min(100.0, 100 * out["elapsed_s"] / total) if total > 0 else None
    elif proc.kind == "annealing":
        out.update(_annealing_timing(proc, now))
    elif proc.kind == "drying":
        planned = (proc.planned_duration_min or 0) * 60
        if proc.target_reached_at:
            held = int((end - proc.target_reached_at).total_seconds())
            out["scheduled_end"] = fmt(proc.target_reached_at + timedelta(seconds=planned))
            if not proc.ended_at:
                out["remaining_s"] = max(0, planned - held)
            out["progress"] = min(100.0, 100 * held / planned) if planned else None
        else:
            out["note"] = "Drying timer starts when the bed reaches the target."
            out["progress"] = 0.0
    else:
        planned = (proc.planned_duration_min or 0) * 60
        out["scheduled_end"] = fmt(proc.started_at + timedelta(seconds=planned))
        if not proc.ended_at:
            out["remaining_s"] = max(0, planned - out["elapsed_s"])
        out["progress"] = min(100.0, 100 * out["elapsed_s"] / planned) if planned else None
    return out


def _annealing_timing(proc: Process, now) -> dict:
    """Per-phase view of a ramp/hold/cool program."""
    target, sp = proc.target_bed_c, proc.current_setpoint_c
    hold_s = (proc.planned_duration_min or 0) * 60
    cool_s = ((target - proc.cool_end_c) / proc.cool_ramp_c_per_h * 3600
              if proc.cool_ramp_c_per_h and proc.cool_end_c is not None else 0)
    out = {}
    if proc.state == ProcessState.HEATING:
        out["note"] = (f"Ramping up at {proc.heat_ramp_c_per_h:g} °C/h: setpoint {sp:g} → {target:g} °C"
                       if proc.heat_ramp_c_per_h and sp is not None and sp < target else f"Heating to {target:g} °C")
        out["progress"] = 0.0
    elif proc.state == ProcessState.DRYING and proc.target_reached_at:
        held = (now - proc.target_reached_at).total_seconds()
        out["remaining_s"] = int(max(0, hold_s - held) + cool_s)
        out["progress"] = min(100.0, 100 * held / hold_s) if hold_s else None
        out["scheduled_end"] = fmt(proc.target_reached_at + timedelta(seconds=hold_s + cool_s))
        out["note"] = f"Holding {target:g} °C" + (f", then ramp down to {proc.cool_end_c:g} °C" if cool_s else "")
    elif proc.state == ProcessState.COOLING and proc.cooldown_started_at:
        done = (now - proc.cooldown_started_at).total_seconds()
        out["remaining_s"] = int(max(0, cool_s - done))
        out["progress"] = min(100.0, 100 * done / cool_s) if cool_s else None
        out["note"] = f"Controlled cool-down: setpoint {sp:g} °C, ending at {proc.cool_end_c:g} °C"
    elif proc.state == ProcessState.COOLING:
        out["note"] = "Switching the heater off and confirming it."
    return out


@bp.route("/processes/<int:process_id>")
def process(process_id):
    proc = db.session.get(Process, process_id) or abort(404)
    printer = db.session.get(Printer, proc.printer_id) if proc.printer_id else None
    status = printer.status if printer else None
    return jsonify(
        id=proc.id, kind=proc.kind, state=proc.state, terminal=proc.state in ProcessState.TERMINAL,
        target_bed_c=proc.target_bed_c, bed_temp_c=status.bed_temp_c if status else None,
        bed_target_c=status.bed_target_c if status else None, heater_off_verified=proc.heater_off_verified,
        error=proc.error, max_bed_observed_c=proc.max_bed_observed_c, **process_timing(proc),
    )


@bp.route("/processes/<int:process_id>/telemetry")
def process_telemetry(process_id):
    proc = db.session.get(Process, process_id) or abort(404)
    rows = TelemetrySample.query.filter_by(process_id=proc.id).order_by(TelemetrySample.ts).all()
    return jsonify(
        labels=[to_local(r.ts).strftime("%m-%d %H:%M") for r in rows],
        series={"Bed (heater reading)": [r.bed_temp_c for r in rows], "Bed target": [r.bed_target_c for r in rows],
                **({"Chamber (printer sensor)": [r.chamber_temp_c for r in rows]}
                   if any(r.chamber_temp_c is not None for r in rows) else {})},
    )

import csv
import io

from flask import Blueprint, Response, render_template, request

from ..extensions import db
from ..models import Printer, Process
from ..timeutil import fmt

bp = Blueprint("history", __name__, url_prefix="/history")

_COLUMNS = [
    ("Date", lambda p: fmt(p.started_at or p.created_at)),
    ("Printer", lambda p: p.printer_name or ""),
    ("Material", lambda p: p.material_label or ""),
    ("Spool size (g)", lambda p: p.spool_size_g or ""),
    ("Process type", lambda p: p.kind),
    ("Target bed (°C)", lambda p: p.target_bed_c if p.target_bed_c is not None else ""),
    ("Max bed observed (°C)", lambda p: p.max_bed_observed_c if p.max_bed_observed_c is not None else ""),
    ("Max enclosure measured (°C)",
     lambda p: p.max_enclosure_observed_c if p.max_enclosure_observed_c is not None else ""),
    ("Duration (min)", lambda p: _duration_min(p)),
    ("Status", lambda p: p.state),
    ("Heater off verified", lambda p: "yes" if p.heater_off_verified else "no"),
    ("Errors", lambda p: p.error or ""),
]


def _duration_min(p: Process):
    if p.started_at and p.ended_at:
        return round((p.ended_at - p.started_at).total_seconds() / 60)
    return ""


def _query():
    q = Process.query
    if pid := request.args.get("printer_id", type=int):
        q = q.filter_by(printer_id=pid)
    if kind := request.args.get("kind"):
        q = q.filter_by(kind=kind)
    return q.order_by(Process.created_at.desc())


@bp.route("/")
def index():
    return render_template("history/index.html", processes=_query().limit(500).all(),
                           printers=Printer.query.order_by(Printer.name).all(),
                           columns=[c[0] for c in _COLUMNS], getters=[c[1] for c in _COLUMNS])


@bp.route("/export.csv")
def export_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c[0] for c in _COLUMNS])
    for proc in _query().all():
        # Guard against spreadsheet formula injection from free-text fields.
        writer.writerow([("'" + v) if isinstance(v, str) and v[:1] in "=+-@" else v
                         for v in (getter(proc) for _, getter in _COLUMNS)])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=bambu-process-history.csv"})


@bp.route("/<int:process_id>")
def detail(process_id):
    from flask import abort

    proc = db.session.get(Process, process_id) or abort(404)
    return render_template("history/detail.html", proc=proc)

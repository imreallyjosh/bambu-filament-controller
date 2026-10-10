from flask import Blueprint, render_template

from ..models import Alert, EventLog, Printer, Process, ProcessState
from ..printjob import job_info

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    printers = Printer.query.order_by(Printer.name).all()
    active = {
        p.printer_id: p
        for p in Process.query.filter(Process.state.in_(ProcessState.ACTIVE | {ProcessState.PENDING})).all()
    }
    alerts = Alert.query.filter_by(acknowledged=False).order_by(Alert.ts.desc()).limit(10).all()
    events = EventLog.query.order_by(EventLog.ts.desc()).limit(10).all()
    return render_template("dashboard.html", printers=printers, active=active, alerts=alerts, events=events,
                           job_info=job_info)

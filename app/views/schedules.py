from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask_login import current_user

from ..extensions import db
from ..forms import SchedulePrepForm
from ..models import Schedule, log_event, utcnow
from ..safety import unattended_heating_allowed
from ..timeutil import fmt

bp = Blueprint("schedules", __name__, url_prefix="/schedules")


def _get(schedule_id) -> Schedule:
    return db.session.get(Schedule, schedule_id) or abort(404)


@bp.route("/")
def index():
    schedules = Schedule.query.order_by(Schedule.enabled.desc(), Schedule.start_at).all()
    return render_template("schedules/index.html", schedules=schedules, unattended=unattended_heating_allowed(),
                           prep_form=SchedulePrepForm(), now=utcnow())


@bp.route("/<int:schedule_id>/prepare", methods=["POST"])
def prepare(schedule_id):
    sch = _get(schedule_id)
    form = SchedulePrepForm()
    if not form.validate_on_submit():
        flash("Tick all three preparation boxes.", "danger")
    elif not sch.enabled or sch.start_at <= utcnow():
        flash("This schedule has no upcoming session to confirm.", "warning")
    else:
        sch.prep_confirmed_for, sch.prep_confirmed_by = sch.start_at, current_user.username
        log_event("process", f"Preparation confirmed for schedule #{sch.id} session at {fmt(sch.start_at)}.",
                  printer_id=sch.printer_id, actor=current_user.username)
        db.session.commit()
        flash(f"Preparation confirmed for {fmt(sch.start_at)}.", "success")
    return redirect(url_for("schedules.index"))


@bp.route("/<int:schedule_id>/toggle", methods=["POST"])
def toggle(schedule_id):
    sch = _get(schedule_id)
    if not sch.enabled and sch.start_at <= utcnow():
        flash("Its start time has passed; create a new schedule instead.", "warning")
        return redirect(url_for("schedules.index"))
    sch.enabled = not sch.enabled
    log_event("process", f"Schedule #{sch.id} {'enabled' if sch.enabled else 'disabled'}.", printer_id=sch.printer_id,
              actor=current_user.username)
    db.session.commit()
    return redirect(url_for("schedules.index"))


@bp.route("/<int:schedule_id>/delete", methods=["POST"])
def delete(schedule_id):
    sch = _get(schedule_id)
    db.session.delete(sch)
    log_event("process", f"Schedule #{schedule_id} deleted.", printer_id=sch.printer_id, actor=current_user.username)
    db.session.commit()
    flash("Schedule deleted. Any session it already started keeps running until stopped.", "info")
    return redirect(url_for("schedules.index"))

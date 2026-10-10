from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..forms import PasswordChangeForm, SafetySettingsForm
from ..models import EventLog, Setting, log_event

bp = Blueprint("settings", __name__, url_prefix="/settings")

DEFAULT_MAX_HEATING_MIN = 12 * 60
DEFAULT_DISCONNECT_S = 60


def _safety_form() -> SafetySettingsForm:
    form = SafetySettingsForm(prefix="safety")
    if not form.is_submitted():
        form.max_heating_minutes.data = int(Setting.get("safety.max_heating_minutes", DEFAULT_MAX_HEATING_MIN))
        form.disconnect_timeout_s.data = int(Setting.get("safety.disconnect_timeout_s", DEFAULT_DISCONNECT_S))
        form.hardware_cutoff_description.data = Setting.get("safety.hardware_cutoff_description")
        form.hardware_cutoff_confirmed.data = Setting.get_bool("safety.hardware_cutoff_confirmed")
        form.no_cutoff_risk_accepted.data = Setting.get_bool("safety.no_cutoff_risk_accepted")
    return form


@bp.route("/")
def index():
    level = request.args.get("level")
    q = EventLog.query
    if level:
        q = q.filter_by(level=level)
    return render_template(
        "settings/index.html", pw_form=PasswordChangeForm(prefix="pw"), safety=_safety_form(),
        events=q.order_by(EventLog.ts.desc()).limit(200).all(), level=level,
        config=current_app.config,
    )


@bp.route("/password", methods=["POST"])
def change_password():
    form = PasswordChangeForm(prefix="pw")
    if form.validate_on_submit() and current_user.check_password(form.current.data):
        current_user.set_password(form.new.data)
        log_event("auth", "Password changed.", actor=current_user.username)
        db.session.commit()
        flash("Password changed.", "success")
    else:
        flash("Password not changed: check the current password and that the new one is at least 10 characters "
              "and matches.", "danger")
    return redirect(url_for("settings.index"))


@bp.route("/safety", methods=["POST"])
def save_safety():
    form = _safety_form()
    if not form.validate_on_submit():
        flash(f"Safety settings not saved: {form.errors}", "danger")
        return redirect(url_for("settings.index"))
    if form.hardware_cutoff_confirmed.data and not (form.hardware_cutoff_description.data or "").strip():
        flash("Describe the hardware cutoff (device, rating, trip temperature) before confirming it.", "danger")
        return redirect(url_for("settings.index"))
    Setting.set("safety.max_heating_minutes", form.max_heating_minutes.data)
    Setting.set("safety.disconnect_timeout_s", form.disconnect_timeout_s.data)
    Setting.set("safety.hardware_cutoff_description", form.hardware_cutoff_description.data)
    was = Setting.get_bool("safety.hardware_cutoff_confirmed")
    Setting.set("safety.hardware_cutoff_confirmed", form.hardware_cutoff_confirmed.data)
    was_accepted = Setting.get_bool("safety.no_cutoff_risk_accepted")
    Setting.set("safety.no_cutoff_risk_accepted", form.no_cutoff_risk_accepted.data)
    log_event("safety", f"Safety settings saved (max heating {form.max_heating_minutes.data} min, "
              f"hardware cutoff confirmed: {form.hardware_cutoff_confirmed.data}, "
              f"unattended heating without cutoff accepted: {form.no_cutoff_risk_accepted.data}).",
              level="warning" if (was, was_accepted) != (form.hardware_cutoff_confirmed.data,
                                                         form.no_cutoff_risk_accepted.data) else "info",
              actor=current_user.username)
    db.session.commit()
    flash("Safety settings saved.", "success")
    return redirect(url_for("settings.index"))

import json

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..forms import EmailForm, NtfyForm
from ..models import Alert, Setting, log_event
from ..notifications import EVENT_TYPES, notify

bp = Blueprint("notify", __name__, url_prefix="/notifications")


def _ntfy_form() -> NtfyForm:
    form = NtfyForm(prefix="ntfy")
    if not form.is_submitted():
        form.enabled.data = Setting.get_bool("notify.ntfy.enabled")
        form.url.data = Setting.get("notify.ntfy.url")
        form.topic.data = Setting.get("notify.ntfy.topic")
        form.username.data = Setting.get("notify.ntfy.username")
        form.priority.data = Setting.get("notify.ntfy.priority", "3")
    return form


def _email_form() -> EmailForm:
    form = EmailForm(prefix="email")
    if not form.is_submitted():
        form.enabled.data = Setting.get_bool("notify.email.enabled")
        form.host.data = Setting.get("notify.email.host")
        form.port.data = int(Setting.get("notify.email.port", "587"))
        form.starttls.data = Setting.get_bool("notify.email.starttls", True)
        form.username.data = Setting.get("notify.email.username")
        form.sender.data = Setting.get("notify.email.from")
        form.to.data = Setting.get("notify.email.to")
    return form


@bp.route("/")
def index():
    alerts = Alert.query.order_by(Alert.ts.desc()).limit(100).all()
    events = {k: (e, Setting.get_bool(f"notify.event.{k}", True)) for k, e in EVENT_TYPES.items() if k != "test"}
    return render_template(
        "notifications/index.html", ntfy=_ntfy_form(), email=_email_form(), alerts=alerts, events=events,
        ntfy_has_token=bool(Setting.get("notify.ntfy.token")),
        ntfy_has_password=bool(Setting.get("notify.ntfy.password")),
        delivery=lambda a: json.loads(a.delivery_json or "{}"),
    )


@bp.route("/ntfy", methods=["POST"])
def save_ntfy():
    form = _ntfy_form()
    if form.validate_on_submit():
        Setting.set("notify.ntfy.enabled", form.enabled.data)
        Setting.set("notify.ntfy.url", form.url.data)
        Setting.set("notify.ntfy.topic", form.topic.data)
        Setting.set("notify.ntfy.username", form.username.data)
        Setting.set("notify.ntfy.priority", form.priority.data)
        if form.clear_credentials.data:
            Setting.set("notify.ntfy.token", None, secret=True)
            Setting.set("notify.ntfy.password", None, secret=True)
        if form.token.data:
            Setting.set("notify.ntfy.token", form.token.data, secret=True)
        if form.password.data:
            Setting.set("notify.ntfy.password", form.password.data, secret=True)
        log_event("notify", "ntfy settings updated.", actor=current_user.username)
        db.session.commit()
        flash("ntfy settings saved.", "success")
    else:
        flash(f"ntfy settings not saved: {form.errors}", "danger")
    return redirect(url_for("notify.index"))


@bp.route("/email", methods=["POST"])
def save_email():
    form = _email_form()
    if form.validate_on_submit():
        Setting.set("notify.email.enabled", form.enabled.data)
        Setting.set("notify.email.host", form.host.data)
        Setting.set("notify.email.port", form.port.data or 587)
        Setting.set("notify.email.starttls", form.starttls.data)
        Setting.set("notify.email.username", form.username.data)
        Setting.set("notify.email.from", form.sender.data)
        Setting.set("notify.email.to", form.to.data)
        if form.password.data:
            Setting.set("notify.email.password", form.password.data, secret=True)
        log_event("notify", "Email settings updated.", actor=current_user.username)
        db.session.commit()
        flash("Email settings saved.", "success")
    else:
        flash(f"Email settings not saved: {form.errors}", "danger")
    return redirect(url_for("notify.index"))


@bp.route("/events", methods=["POST"])
def save_events():
    for key, event in EVENT_TYPES.items():
        if key == "test" or event.severity == "critical":
            continue
        Setting.set(f"notify.event.{key}", bool(request.form.get(key)))
    db.session.commit()
    flash("Event selection saved. Critical safety events are always sent.", "success")
    return redirect(url_for("notify.index"))


@bp.route("/test", methods=["POST"])
def test():
    alert = notify("test", printer="(test)", material="—", process="notification test",
                   details=f"Requested by {current_user.username} from the web UI.")
    results = json.loads(alert.delivery_json or "{}")
    if not results:
        flash("Test recorded as a dashboard alert. No external channel is enabled.", "warning")
    elif any(str(v).startswith("FAILED") for v in results.values()):
        flash(f"Test delivery had failures: {results}", "danger")
    else:
        flash(f"Test delivered: {results}", "success")
    return redirect(url_for("notify.index"))


@bp.route("/alerts/ack", methods=["POST"])
def ack_all():
    Alert.query.filter_by(acknowledged=False).update({"acknowledged": True})
    db.session.commit()
    return redirect(url_for("notify.index"))


@bp.route("/alerts/<int:alert_id>/ack", methods=["POST"])
def ack_one(alert_id):
    alert = db.session.get(Alert, alert_id) or abort(404)
    alert.acknowledged = True
    db.session.commit()
    # Return to the page the button was on (dashboard or notifications), same-site only.
    ref = request.referrer
    return redirect(ref if ref and ref.startswith(request.host_url) else url_for("notify.index"))

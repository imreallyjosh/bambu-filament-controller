import getpass
import json
import os

import click
from flask import Flask

from . import timeutil
from .capabilities import PROFILES
from .config import Config, DevConfig
from .extensions import csrf, db, login_manager, migrate
from .models import Alert, Setting, User, VerificationStatus


def create_app(config_object=None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    if config_object is None:
        config_object = DevConfig if os.environ.get("BFC_ENV") == "development" else Config
    app.config.from_object(config_object)

    if not app.config.get("SECRET_KEY") or not app.config.get("FERNET_KEY"):
        raise RuntimeError(
            "BFC_SECRET_KEY and BFC_FERNET_KEY must be set "
            "(see /etc/bambu-filament-controller/controller.env)"
        )

    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(os.path.dirname(__file__), "..", "migrations"))
    csrf.init_app(app)
    login_manager.init_app(app)

    from .views import register_blueprints
    register_blueprints(app)

    _register_template_helpers(app)
    _register_cli(app)

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        return resp

    return app


@login_manager.user_loader
def _load_user(user_id: str):
    return db.session.get(User, int(user_id))


def _register_template_helpers(app: Flask) -> None:
    app.jinja_env.filters["localtime"] = timeutil.fmt
    app.jinja_env.filters["ago"] = timeutil.ago

    @app.template_filter("tojson_pretty")
    def _tojson_pretty(raw):
        try:
            return json.dumps(json.loads(raw), indent=2, sort_keys=True)
        except (TypeError, ValueError):
            return raw

    @app.template_filter("temp")
    def _temp(value):
        return "—" if value is None else f"{value:.1f} °C"

    @app.context_processor
    def _globals():
        from .controller_status import controller_health

        try:
            unacked = Alert.query.filter_by(acknowledged=False).count()
            health = controller_health()
        except Exception:  # DB not migrated yet
            unacked, health = 0, None
        from .views.printers import latest_prepare

        return {
            "latest_prepare": latest_prepare,
            "tz_label": timeutil.tz_label(),
            "unacked_alerts": unacked,
            "controller": health,
            "PROFILES": PROFILES,
            "VS": VerificationStatus,
            "heater_global": app.config["ALLOW_HEATER_CONTROL"],
        }


def _register_cli(app: Flask) -> None:
    @app.cli.command("create-user")
    @click.argument("username")
    def create_user(username):
        """Create or reset a login user (prompts for password)."""
        pw = getpass.getpass("Password: ")
        if len(pw) < 10:
            raise click.ClickException("Password must be at least 10 characters.")
        if pw != getpass.getpass("Repeat: "):
            raise click.ClickException("Passwords do not match.")
        user = User.query.filter_by(username=username).first() or User(username=username)
        user.set_password(pw)
        db.session.add(user)
        db.session.commit()
        click.echo(f"User '{username}' saved.")

    @app.cli.command("notify-event")
    @click.argument("event_key")
    @click.option("--details", default="")
    def notify_event(event_key, details):
        """Send a notification from outside the app (used by systemd OnFailure=)."""
        from .models import log_event
        from .notifications import EVENT_TYPES, notify

        if event_key not in EVENT_TYPES:
            raise click.ClickException(f"Unknown event; choose from {', '.join(EVENT_TYPES)}")
        log_event("system", f"External event '{event_key}': {details}", level="critical", actor="systemd")
        alert = notify(event_key, process="controller daemon", details=details)
        click.echo(alert.delivery_json)

    @app.cli.command("check-controller")
    def check_controller():
        """Independent watchdog (systemd timer): alert once when the controller heartbeat goes stale."""
        from .controller_status import controller_health
        from .models import log_event
        from .notifications import notify

        health = controller_health()
        was_down = Setting.get_bool("controller.down_notified")
        if not health.alive and not was_down:
            details = f"Last heartbeat: {timeutil.fmt(health.heartbeat_at)}. Printer supervision is not running."
            log_event("safety", "Controller heartbeat stale. " + details, level="critical", actor="watchdog-timer")
            Setting.set("controller.down_notified", True)
            notify("controller_down", process="controller daemon", details=details)
        elif health.alive and was_down:
            Setting.set("controller.down_notified", False)
            log_event("system", "Controller heartbeat restored.", actor="watchdog-timer")
            db.session.commit()
        click.echo("alive" if health.alive else "STALE")

    @app.cli.command("user-count")
    def user_count():
        """Print how many login users exist (used by install.sh)."""
        click.echo(User.query.count())

    @app.cli.command("controller-alive")
    def controller_alive():
        """Exit 0 if the controller heartbeat is fresh, 1 otherwise (container health check). Sends nothing."""
        from .controller_status import controller_health

        alive = controller_health().alive
        click.echo("alive" if alive else "STALE")
        raise SystemExit(0 if alive else 1)

    @app.cli.command("seed")
    def seed():
        """Insert preset templates (idempotent)."""
        from .seed import seed_presets

        click.echo(f"Added {seed_presets()} template(s).")
        if Setting.get("notify.ntfy.priority") is None:
            Setting.set("notify.ntfy.priority", "3")
            db.session.commit()

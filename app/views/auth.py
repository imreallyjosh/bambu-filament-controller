import time
from collections import defaultdict, deque

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..forms import LoginForm
from ..models import User, log_event, utcnow

bp = Blueprint("auth", __name__)

# Simple per-worker brute-force throttle: 5 failures per 15 minutes per client IP.
_FAILS: dict[str, deque] = defaultdict(deque)
_WINDOW_S, _MAX_FAILS = 900, 5


def _throttled(ip: str) -> bool:
    q = _FAILS[ip]
    while q and q[0] < time.monotonic() - _WINDOW_S:
        q.popleft()
    return len(q) >= _MAX_FAILS


@bp.before_app_request
def _require_login():
    """Every page requires login except the login page and static assets."""
    if request.endpoint in ("auth.login", "static") or request.endpoint is None:
        return None
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    ip = request.remote_addr or "?"
    if form.validate_on_submit():
        if _throttled(ip):
            flash("Too many failed attempts. Try again later.", "danger")
            return render_template("auth/login.html", form=form), 429
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            user.last_login_at = utcnow()
            log_event("auth", f"Login from {ip}", actor=user.username)
            db.session.commit()
            nxt = request.args.get("next")
            if not nxt or not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("dashboard.index")
            return redirect(nxt)
        _FAILS[ip].append(time.monotonic())
        log_event("auth", f"Failed login for '{form.username.data}' from {ip}", level="warning")
        db.session.commit()
        flash("Invalid username or password.", "danger")
    return render_template("auth/login.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))

"""Notification dispatch: web dashboard alerts (always), ntfy (primary), email (optional).

Every event is recorded as an Alert row first, so a failed ntfy/email delivery
never loses the event. Delivery results are stored on the alert and failures
are written to the event log.
"""
import json
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import requests

from .extensions import db
from .models import Alert, Setting, log_event, utcnow
from .timeutil import fmt


@dataclass(frozen=True)
class EventType:
    key: str
    label: str
    severity: str  # info | warning | critical
    ntfy_tags: str
    min_priority: int = 1  # floor for events that need attention without being warnings


EVENT_TYPES = {
    e.key: e
    for e in [
        EventType("schedule_approaching", "Scheduled session approaching", "info", "alarm_clock"),
        EventType("process_started", "Process started", "info", "fire"),
        EventType("target_reached", "Target temperature reached", "info", "thermometer"),
        EventType("flip_spool", "Halfway: flip the spool", "info", "arrows_counterclockwise", min_priority=4),
        EventType("process_completed", "Process completed (heater off verified)", "info", "white_check_mark"),
        EventType("process_stopped", "Process stopped manually", "warning", "stop_sign"),
        EventType("printer_disconnected", "Printer unexpectedly disconnected", "warning", "electric_plug"),
        EventType("temp_limit_exceeded", "Temperature safety limit exceeded", "critical", "rotating_light,fire"),
        EventType("schedule_failed", "Scheduled process failed to start", "warning", "x"),
        EventType("process_ended_unexpectedly", "Operation ended unexpectedly", "critical", "warning"),
        EventType("heater_off_unconfirmed", "Heater shutdown NOT confirmed", "critical", "rotating_light,sos"),
        EventType("controller_down", "Controller daemon heartbeat lost", "critical", "skull"),
        EventType("test", "Test notification", "info", "test_tube"),
    ]
}

# ntfy priorities: 1=min 2=low 3=default 4=high 5=urgent. Critical events always go urgent.
_SEVERITY_FLOOR = {"info": 1, "warning": 4, "critical": 5}


def _event_enabled(key: str) -> bool:
    if EVENT_TYPES[key].severity == "critical":
        return True  # safety events cannot be muted
    return Setting.get_bool(f"notify.event.{key}", True)


def _ntfy_priority(event: EventType) -> int:
    configured = int(Setting.get("notify.ntfy.priority", "3"))
    return max(configured, _SEVERITY_FLOOR[event.severity], event.min_priority)


def send_ntfy(title: str, body: str, priority: int, tags: str) -> str:
    url = (Setting.get("notify.ntfy.url") or "").rstrip("/")
    topic = Setting.get("notify.ntfy.topic") or ""
    if not url or not topic:
        raise ValueError("ntfy server URL and topic must be configured")
    headers = {"Title": title.encode("utf-8"), "Priority": str(priority), "Tags": tags}
    auth = None
    token = Setting.get("notify.ntfy.token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif Setting.get("notify.ntfy.username"):
        auth = (Setting.get("notify.ntfy.username"), Setting.get("notify.ntfy.password") or "")
    resp = requests.post(f"{url}/{topic}", data=body.encode("utf-8"), headers=headers, auth=auth, timeout=10)
    resp.raise_for_status()
    return f"HTTP {resp.status_code}"


def send_email(subject: str, body: str) -> str:
    host = Setting.get("notify.email.host")
    to_addr = Setting.get("notify.email.to")
    from_addr = Setting.get("notify.email.from") or to_addr
    if not host or not to_addr:
        raise ValueError("SMTP host and recipient must be configured")
    port = int(Setting.get("notify.email.port", "587"))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if Setting.get_bool("notify.email.starttls", True):
            smtp.starttls()
        user = Setting.get("notify.email.username")
        if user:
            smtp.login(user, Setting.get("notify.email.password") or "")
        smtp.send_message(msg)
    return "sent"


def notify(event_key: str, *, printer: str | None = None, material: str | None = None,
           process: str | None = None, details: str = "") -> Alert:
    """Record and deliver a notification. Never raises on delivery failure."""
    event = EVENT_TYPES[event_key]
    now = utcnow()
    title = f"[{printer}] {event.label}" if printer else event.label
    lines = [
        f"Event: {event.label}",
        f"Printer: {printer or '—'}",
        f"Material: {material or '—'}",
        f"Process: {process or '—'}",
        f"Time: {fmt(now)}",
    ]
    if details:
        lines += ["", details]
    body = "\n".join(lines)

    alert = Alert(ts=now, event_type=event_key, severity=event.severity, title=title, body=body)
    db.session.add(alert)
    delivery: dict[str, str] = {}

    if _event_enabled(event_key) or event_key == "test":
        if Setting.get_bool("notify.ntfy.enabled"):
            try:
                delivery["ntfy"] = send_ntfy(title, body, _ntfy_priority(event), event.ntfy_tags)
            except Exception as exc:  # delivery failure must not break callers
                delivery["ntfy"] = f"FAILED: {exc}"
                log_event("notify", f"ntfy delivery failed for '{event_key}': {exc}", level="error")
        if Setting.get_bool("notify.email.enabled"):
            try:
                delivery["email"] = send_email(title, body)
            except Exception as exc:
                delivery["email"] = f"FAILED: {exc}"
                log_event("notify", f"Email delivery failed for '{event_key}': {exc}", level="error")
    else:
        delivery["skipped"] = "event disabled in notification settings"

    alert.delivery_json = json.dumps(delivery)
    db.session.commit()
    return alert

"""Web-side view of the controller daemon's health (read from its heartbeat)."""
from dataclasses import dataclass
from datetime import datetime

from flask import current_app

from .models import Setting, utcnow


@dataclass
class ControllerHealth:
    alive: bool
    heartbeat_at: datetime | None
    started_at: datetime | None
    mqtt_mode: str


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def controller_health() -> ControllerHealth:
    hb = _parse(Setting.get("controller.heartbeat_at"))
    stale = current_app.config["CONTROLLER_HEARTBEAT_STALE_S"]
    alive = hb is not None and (utcnow() - hb).total_seconds() < stale
    return ControllerHealth(
        alive=alive,
        heartbeat_at=hb,
        started_at=_parse(Setting.get("controller.started_at")),
        mqtt_mode=Setting.get("controller.mqtt_mode", "unknown"),
    )

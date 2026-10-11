"""Timezone handling: store naive UTC, display in the server's local zone."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app
from tzlocal import get_localzone_name


def local_tz() -> ZoneInfo:
    name = current_app.config.get("TIMEZONE") or get_localzone_name() or "UTC"
    return ZoneInfo(name)


def tz_label() -> str:
    tz = local_tz()
    return f"{tz.key} ({datetime.now(tz).strftime('%Z, UTC%z')})"


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(local_tz())


def local_to_utc(dt: datetime) -> datetime:
    """Interpret a naive datetime from a form as local time, return naive UTC."""
    return dt.replace(tzinfo=local_tz()).astimezone(timezone.utc).replace(tzinfo=None)


def fmt(dt: datetime | None, with_tz: bool = True) -> str:
    loc = to_local(dt)
    if loc is None:
        return "—"
    return loc.strftime("%Y-%m-%d %H:%M:%S %Z" if with_tz else "%Y-%m-%d %H:%M:%S")


def ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    secs = int((datetime.now(timezone.utc).replace(tzinfo=None) - dt).total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def friendly(dt: datetime | None) -> str | None:
    """'Today 3:45 PM', 'Tomorrow 1:10 AM', else 'Sat Oct 12, 9:00 AM' (server timezone)."""
    loc = to_local(dt)
    if loc is None:
        return None
    today = datetime.now(local_tz()).date()
    clock = loc.strftime("%-I:%M %p")
    if loc.date() == today:
        return f"Today {clock}"
    if (loc.date() - today).days == 1:
        return f"Tomorrow {clock}"
    if (loc.date() - today).days == -1:
        return f"Yesterday {clock}"
    return loc.strftime("%a %b %-d, ") + clock

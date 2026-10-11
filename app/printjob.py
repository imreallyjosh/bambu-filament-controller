"""Current print job, derived from the printer's latest full status report (PrinterStatus.raw_json)."""
import json
from datetime import datetime, timedelta, timezone

from .models import utcnow
from .timeutil import friendly

ACTIVE_STATES = {"RUNNING", "PAUSE", "PREPARE", "SLICING"}
ENDED_STATES = {"FINISH", "FAILED"}

STATE_LABELS = {"RUNNING": "Printing", "PAUSE": "Paused", "PREPARE": "Preparing", "SLICING": "Slicing",
                "FINISH": "Finished", "FAILED": "Failed"}

# stg_cur values (as mapped by the ha-bambulab integration); unknown codes are shown as "Stage N".
STAGE_LABELS = {
    0: "Printing", 1: "Auto bed leveling", 2: "Heatbed preheating", 3: "Sweeping XY mech mode",
    4: "Changing filament", 5: "M400 pause", 6: "Paused: filament runout", 7: "Heating hotend",
    8: "Calibrating extrusion", 9: "Scanning bed surface", 10: "Inspecting first layer",
    11: "Identifying build plate type", 12: "Calibrating micro lidar", 13: "Homing toolhead",
    14: "Cleaning nozzle tip", 15: "Checking extruder temperature", 16: "Paused by user",
    17: "Paused: front cover falling", 18: "Calibrating micro lidar", 19: "Calibrating extrusion flow",
    20: "Paused: nozzle temperature malfunction", 21: "Paused: heatbed temperature malfunction",
}

SPEED_LABELS = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}


def _int(value):
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


# Fan speeds are reported on a 0-15 scale.
FAN_FIELDS = (("cooling_fan_speed", "Part"), ("big_fan1_speed", "Aux"), ("big_fan2_speed", "Chamber"))


def _start_time(state: dict) -> datetime | None:
    """Print start from gcode_start_time (epoch seconds), when the printer reports a plausible one."""
    ts = _int(state.get("gcode_start_time"))
    if not ts or ts < 1_500_000_000:
        return None
    started = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
    return started if started <= utcnow() + timedelta(minutes=5) else None


def _filament(state: dict) -> dict | None:
    """The filament feeding the print: an AMS slot (tray_now = ams_index * 4 + slot) or the external spool."""
    ams = state.get("ams") if isinstance(state.get("ams"), dict) else {}
    now = _int(ams.get("tray_now"))
    tray, source = None, None
    if now == 254 or (now is None and isinstance(state.get("vt_tray"), dict)):
        tray, source = state.get("vt_tray"), "External spool"
    elif now is not None and 0 <= now < 64:
        for unit in ams.get("ams") or []:
            if _int(unit.get("id")) == now // 4:
                tray = next((t for t in unit.get("tray") or [] if _int(t.get("id")) == now % 4), None)
                source = f"AMS {now // 4 + 1}, slot {now % 4 + 1}"
    if not isinstance(tray, dict) or not tray.get("tray_type"):
        return None
    color = str(tray.get("tray_color") or "")
    remain = _int(tray.get("remain"))
    return {
        "type": tray.get("tray_sub_brands") or tray.get("tray_type"),
        "color": f"#{color[:6]}" if len(color) >= 6 and all(c in "0123456789abcdefABCDEF" for c in color[:6]) else None,
        "remain": remain if remain is not None and 0 <= remain <= 100 else None,
        "source": source,
    }


def _fans(state: dict) -> str | None:
    parts = []
    for key, label in FAN_FIELDS:
        v = _int(state.get(key))
        if v is not None and 0 <= v <= 15:
            parts.append(f"{label} {round(v / 15 * 100)}%")
    return " · ".join(parts) or None


def job_info(raw_json: str | None) -> dict | None:
    """Job summary while printing (or just finished/failed); None when idle or unknown."""
    try:
        state = json.loads(raw_json) if raw_json else {}
    except ValueError:
        return None
    gstate = state.get("gcode_state")
    if gstate not in ACTIVE_STATES | ENDED_STATES:
        return None
    name = state.get("subtask_name") or state.get("gcode_file") or "Print job"
    if name.endswith(".gcode") and "/" in name:
        name = name.rsplit("/", 1)[-1]
    percent = _int(state.get("mc_percent"))
    remaining = _int(state.get("mc_remaining_time"))  # minutes
    stage = _int(state.get("stg_cur"))
    active = gstate in ACTIVE_STATES
    now = utcnow()
    started = _start_time(state)
    elapsed_min = int((now - started).total_seconds() // 60) if started and active else None
    eta = now + timedelta(minutes=remaining) if active and remaining is not None else None
    error = _int(state.get("print_error"))
    hms = state.get("hms") if isinstance(state.get("hms"), list) else []
    return {
        "active": active,
        "state": gstate,
        "state_label": STATE_LABELS.get(gstate, gstate.title()),
        "name": name,
        "percent": max(0, min(100, percent)) if percent is not None else None,
        "remaining_min": remaining if active else None,
        "remaining": _duration(remaining) if active and remaining is not None else None,
        "eta": friendly(eta),
        "started": friendly(started),
        "elapsed": _duration(elapsed_min) if elapsed_min is not None else None,
        "total": _duration(elapsed_min + remaining) if elapsed_min is not None and remaining is not None else None,
        "filament": _filament(state) if active else None,
        "fans": _fans(state) if active else None,
        "error": f"0x{error:08X}" if error else None,
        "hms_count": len(hms),
        "layer": _int(state.get("layer_num")),
        "total_layers": _int(state.get("total_layer_num")),
        "stage": (STAGE_LABELS.get(stage, f"Stage {stage}") if active and stage not in (None, -1, 255) else None),
        "speed": SPEED_LABELS.get(_int(state.get("spd_lvl"))) if active else None,
    }


def _duration(minutes: int) -> str:
    h, m = divmod(max(0, minutes), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"

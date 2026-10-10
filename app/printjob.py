"""Current print job, derived from the printer's latest full status report (PrinterStatus.raw_json)."""
import json
from datetime import timedelta

from .models import utcnow
from .timeutil import fmt

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
    return {
        "active": active,
        "state": gstate,
        "state_label": STATE_LABELS.get(gstate, gstate.title()),
        "name": name,
        "percent": max(0, min(100, percent)) if percent is not None else None,
        "remaining_min": remaining if active else None,
        "remaining": _duration(remaining) if active and remaining is not None else None,
        "eta": fmt(utcnow() + timedelta(minutes=remaining), with_tz=False)[:-3]
        if active and remaining is not None else None,
        "layer": _int(state.get("layer_num")),
        "total_layers": _int(state.get("total_layer_num")),
        "stage": (STAGE_LABELS.get(stage, f"Stage {stage}") if active and stage not in (None, -1, 255) else None),
        "speed": SPEED_LABELS.get(_int(state.get("spd_lvl"))) if active else None,
    }


def _duration(minutes: int) -> str:
    h, m = divmod(max(0, minutes), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"

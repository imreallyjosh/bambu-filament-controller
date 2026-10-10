"""Bambu local MQTT protocol: payload construction and report parsing.

Pure functions only (no I/O) so they can be unit-tested without a printer.

READ-ONLY: the only requests this module can build are "pushall" (request a
full status report) and 'get_version' (firmware/module versions). There is no
code path that builds a G-code, temperature, fan, or print command.
"""
import itertools
import json

_seq = itertools.count(1)


def report_topic(serial: str) -> str:
    return f"device/{serial}/report"


def request_topic(serial: str) -> str:
    return f"device/{serial}/request"


def _pushall() -> dict:
    return {"pushing": {"sequence_id": str(next(_seq)), "command": "pushall", "version": 1, "push_target": 1}}


def _get_version() -> dict:
    return {"info": {"sequence_id": str(next(_seq)), "command": "get_version"}}


READONLY_REQUESTS = {"pushall": _pushall, "get_version": _get_version}


def build_request(name: str) -> str:
    """Return the JSON for an allow-listed read-only request; anything else raises."""
    try:
        builder = READONLY_REQUESTS[name]
    except KeyError:
        raise PermissionError(f"Request '{name}' is not in the read-only allow-list") from None
    return json.dumps(builder())


def deep_merge(base: dict, update: dict) -> dict:
    """Merge a report into cached state. P1-series reports carry only changed fields."""
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def extract_status(print_state: dict, has_chamber_sensor: bool) -> dict:
    """Map the merged 'print' object to PrinterStatus columns."""
    return {
        "gcode_state": print_state.get("gcode_state"),
        "stage": None if print_state.get("stg_cur") is None else str(print_state.get("stg_cur")),
        "bed_temp_c": _num(print_state.get("bed_temper")),
        "bed_target_c": _num(print_state.get("bed_target_temper")),
        "nozzle_temp_c": _num(print_state.get("nozzle_temper")),
        "nozzle_target_c": _num(print_state.get("nozzle_target_temper")),
        # Never surface a chamber value from a model without a chamber sensor.
        "chamber_temp_c": _num(print_state.get("chamber_temper")) if has_chamber_sensor else None,
        "hms": print_state.get("hms") or [],
    }


# Report fields that identify what job (if any) the printer considers itself to be running.
JOB_FIELDS = ("print_type", "gcode_file", "subtask_name", "task_id", "mc_remaining_time", "mc_percent", "stg_cur")


def job_context(print_state: dict) -> dict:
    """Job-identifying fields that are present. Empty strings and a task_id of '0' (no task) are omitted;
    numeric progress fields are kept even when zero."""
    out = {}
    for key in JOB_FIELDS:
        value = print_state.get(key)
        if value is None or value == "" or (key == "task_id" and str(value) == "0"):
            continue
        out[key] = value
    return out


def extract_firmware(payload: dict) -> str | None:
    """Firmware version from a get_version response (module named 'ota')."""
    info = payload.get("info")
    if not isinstance(info, dict) or info.get("command") != "get_version":
        return None
    for module in info.get("module") or []:
        if isinstance(module, dict) and module.get("name") == "ota":
            return module.get("sw_ver")
    return None

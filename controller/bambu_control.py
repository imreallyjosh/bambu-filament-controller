"""The ONLY module that can build a printer control command.

It builds exactly two things, both sent via the MQTT 'gcode_line' command:
  * a heated-bed target (M140)
  * one fixed "prepare" motion sequence: home, lower the bed, park the toolhead
No nozzle heater, fan, extrusion, free-form motion, or print commands exist.

Hard limits are enforced here, independently of any caller:
  * the target must be an integer in [0, ABSOLUTE_BED_MAX_C]
  * a non-zero target must not exceed the caller-supplied effective maximum
Turning the heater OFF (target 0) is always allowed.
"""
import itertools
import json
from dataclasses import dataclass

ABSOLUTE_BED_MAX_C = 100  # never exceeds the lower of the two models' spec maximum (P1S 100 °C)

_seq = itertools.count(1000)


@dataclass(frozen=True)
class BedCommand:
    target_c: int
    payload: str

    @property
    def is_off(self) -> bool:
        return self.target_c == 0


def bed_off() -> BedCommand:
    return _build(0)


def bed_target(target_c: int, effective_max_c: float) -> BedCommand:
    if not isinstance(target_c, int) or isinstance(target_c, bool):
        raise ValueError("Bed target must be an integer °C")
    if target_c <= 0:
        raise ValueError("Use bed_off() to switch the heater off")
    if target_c > ABSOLUTE_BED_MAX_C or target_c > effective_max_c:
        raise ValueError(f"Bed target {target_c} °C exceeds the limit ({min(ABSOLUTE_BED_MAX_C, effective_max_c)} °C)")
    return _build(target_c)


def _build(target_c: int) -> BedCommand:
    payload = {"print": {"sequence_id": str(next(_seq)), "command": "gcode_line", "param": f"M140 S{target_c}\n"}}
    return BedCommand(target_c, json.dumps(payload))


def command_result(payload: dict) -> dict | None:
    """Extract the printer's response to a gcode_line command, if this report is one."""
    p = payload.get("print")
    if not isinstance(p, dict) or p.get("command") != "gcode_line":
        return None
    return {"result": p.get("result"), "reason": p.get("reason"), "sequence_id": p.get("sequence_id")}


ABSOLUTE_Z_MAX = 250
_XY_RANGE = range(0, 257)


@dataclass(frozen=True)
class MotionCommand:
    gcode: str
    payload: str


def prepare_sequence(park_x: int, park_y: int, park_z: int) -> MotionCommand:
    """Home, then lower the bed BEFORE moving the toolhead, so a raised bed can't be swept by the toolhead.

    Only safe with an empty bed: homing Z raises the bed until it meets the nozzle.
    """
    for v in (park_x, park_y, park_z):
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("Park coordinates must be integers")
    if park_x not in _XY_RANGE or park_y not in _XY_RANGE or not (10 <= park_z <= ABSOLUTE_Z_MAX):
        raise ValueError("Park position outside the build volume")
    gcode = f"G28\nG90\nG1 Z{park_z} F600\nG1 X{park_x} Y{park_y} F3000\nM400\n"
    payload = {"print": {"sequence_id": str(next(_seq)), "command": "gcode_line", "param": gcode}}
    return MotionCommand(gcode, json.dumps(payload))

"""Per-model capability profiles.

Each capability has a status:
  "spec"        - taken from the manufacturer's published spec sheet, not yet
                  confirmed on your unit
  "documented"  - documented by community protocol references (OpenBambuAPI),
                  not yet confirmed on your unit/firmware
  "verified"    - confirmed on your printer (set from the printer record once tested)
  "unsupported" - known not to exist on this model
  "unverified"  - plausible but untested; the feature stays disabled

Heating and fan *control* start as "unverified" and stay disabled until the
Phase 4 test passes on that specific printer and firmware.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    status: str
    detail: str = ""
    value: float | int | str | None = None


@dataclass(frozen=True)
class ModelProfile:
    key: str
    display_name: str
    mqtt_port: int
    full_status_push: bool
    pushall_min_interval_s: int
    capabilities: dict[str, Capability] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    # "Prepare printer" park position: bed lowered to Z, toolhead to X/Y. Unverified until seen on the printer.
    park_xyz: tuple[int, int, int] = (128, 250, 245)
    # Camera protocol: "rtsps" (X1: H.264 on port 322) or "jpeg_tcp" (P1: JPEG frames on port 6000).
    camera_kind: str = "jpeg_tcp"

    def cap(self, name: str) -> Capability:
        return self.capabilities.get(name, Capability("unsupported", "Not defined for this model"))


_COMMON_MQTT = Capability(
    "documented",
    "Local MQTT over TLS on port 8883, user 'bblp', password = LAN access code. "
    "Topics device/<serial>/report (subscribe) and device/<serial>/request (publish).",
)

_AUTH_NOTE = (
    "Bambu 'Authorization Control' firmware rejects third-party control commands "
    "(including temperature changes) unless the printer is in LAN Only mode with "
    "Developer Mode enabled. Read-only status monitoring is reported to remain available. "
    "Check your firmware version and network mode before any control test."
)

PROFILES: dict[str, ModelProfile] = {
    "X1": ModelProfile(
        key="X1",
        display_name="Bambu Lab X1 (original, non-Carbon)",
        mqtt_port=8883,
        full_status_push=True,
        pushall_min_interval_s=60,
        camera_kind="rtsps",
        capabilities={
            "mqtt_status": _COMMON_MQTT,
            "bed_max_temp_c": Capability("spec", "Manufacturer heatbed maximum", 110),
            "nozzle_max_temp_c": Capability("spec", "Manufacturer hotend maximum", 300),
            "chamber_temp_sensor": Capability(
                "documented",
                "X1-series reports chamber_temper from a chamber sensor. Accuracy vs. the "
                "air around a spool is unknown; treat it as a printer reading, not filament temperature.",
            ),
            "active_chamber_heater": Capability("unsupported", "No chamber heater on X1 / X1C (only X1E)."),
            "bed_heater_control_idle": Capability(
                "unverified",
                "Community tools send M140 via the 'gcode_line' MQTT command while idle. "
                "Blocked on Authorization-Control firmware unless Developer Mode is on. Must be tested.",
            ),
            "fan_control_idle": Capability("unverified", "M106 P2/P3 via gcode_line; untested."),
            "prepare_motion": Capability(
                "unverified",
                "Home (G28), lower the bed to Z245, park the toolhead at X128 Y250, sent via gcode_line while idle. "
                "Only with an EMPTY bed: homing raises the bed to the nozzle.",
            ),
            "pause": Capability("unsupported", "Idle bed heating is not a print job; 'pause' means heater off."),
            "annealing": Capability(
                "unverified",
                "Best-effort bed annealing (owner opted in): ramp up, hold and controlled ramp down by stepping the "
                "bed target. Heat comes from below only. Needs verified heater control like drying.",
            ),
            "firmware_heater_idle_timeout": Capability(
                "unverified",
                "Unknown whether firmware turns the bed off by itself when idle. Must not be assumed.",
            ),
        },
        notes=(_AUTH_NOTE,),
    ),
    "P1S": ModelProfile(
        key="P1S",
        display_name="Bambu Lab P1S",
        mqtt_port=8883,
        full_status_push=False,
        # OpenBambuAPI warns pushall on P1-series can cause lag more often than every 5 min.
        pushall_min_interval_s=300,
        capabilities={
            "mqtt_status": _COMMON_MQTT,
            "bed_max_temp_c": Capability("spec", "Manufacturer heatbed maximum", 100),
            "nozzle_max_temp_c": Capability("spec", "Manufacturer hotend maximum", 300),
            "chamber_temp_sensor": Capability(
                "unsupported",
                "P1S has no chamber temperature sensor. Any chamber_temper value it reports must not "
                "be displayed as a measurement. Use an independent sensor.",
            ),
            "active_chamber_heater": Capability("unsupported", "No chamber heater."),
            "bed_heater_control_idle": Capability(
                "unverified",
                "Same gcode_line/M140 mechanism as X1 per community tools. Authorization firmware "
                "reached P-series later than X-series; must be tested on your firmware.",
            ),
            "fan_control_idle": Capability("unverified", "M106 P2/P3 via gcode_line; untested."),
            "prepare_motion": Capability(
                "unverified",
                "Home (G28), lower the bed to Z245, park the toolhead at X128 Y250, sent via gcode_line while idle. "
                "Only with an EMPTY bed: homing raises the bed to the nozzle.",
            ),
            "pause": Capability("unsupported", "Idle bed heating is not a print job; 'pause' means heater off."),
            "annealing": Capability(
                "unverified",
                "Best-effort bed annealing (owner opted in): ramp up, hold and controlled ramp down by stepping the "
                "bed target. Heat comes from below only. Needs verified heater control like drying.",
            ),
            "firmware_heater_idle_timeout": Capability(
                "unverified",
                "Unknown whether firmware turns the bed off by itself when idle. Must not be assumed.",
            ),
            "status_reports_are_deltas": Capability(
                "documented",
                "P1-series push_status sends only changed fields; full state requires 'pushall'.",
            ),
        },
        notes=(_AUTH_NOTE, "AMS (gen 1) has no heater; AMS humidity/temperature are informational only."),
    ),
}

MODEL_CHOICES = [(k, p.display_name) for k, p in PROFILES.items()]


def profile_for(model: str) -> ModelProfile:
    try:
        return PROFILES[model]
    except KeyError as exc:
        raise ValueError(f"Unknown printer model {model!r}") from exc


def has_chamber_sensor(model: str) -> bool:
    return profile_for(model).cap("chamber_temp_sensor").status in {"documented", "verified"}


def bed_max_c(model: str) -> int:
    return int(profile_for(model).cap("bed_max_temp_c").value)

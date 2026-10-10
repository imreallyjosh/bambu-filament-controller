"""Temperature limits shared by the web app and the controller daemon (the daemon re-checks)."""
from .capabilities import bed_max_c

# Over-temperature trip point above the target (never above the model's spec maximum).
OVERSHOOT_C = 8.0
MIN_BED_TARGET_C = 30


def bed_limits(printer, preset=None, setting=None) -> dict[str, float]:
    """Every applicable maximum for a bed target. The lowest one wins."""
    limits = {"Model spec heatbed max": float(bed_max_c(printer.model))}
    if printer.max_bed_temp_c:
        limits["Your printer cap"] = float(printer.max_bed_temp_c)
    if preset is not None and preset.max_temp_c:
        limits["Material/spool max"] = float(preset.max_temp_c)
    if setting is not None and setting.max_bed_c:
        limits["Preset max for this model"] = float(setting.max_bed_c)
    return limits


def effective_bed_max(printer, preset=None, setting=None) -> float:
    return min(bed_limits(printer, preset, setting).values())


def overtemp_trip_c(printer, target_c: float) -> float:
    return min(target_c + OVERSHOOT_C, float(bed_max_c(printer.model)))

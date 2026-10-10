"""Policy for unattended (scheduled) heating."""
from .models import Setting


def unattended_heating_allowed() -> tuple[bool, str]:
    """Unattended heating needs either a confirmed independent hardware cutoff, or the owner's
    explicit, logged acceptance of running without one."""
    if Setting.get_bool("safety.hardware_cutoff_confirmed"):
        return True, "Independent hardware cutoff confirmed."
    if Setting.get_bool("safety.no_cutoff_risk_accepted"):
        return True, "Owner accepted running without a hardware cutoff."
    return False, "No hardware cutoff confirmed and the no-cutoff risk has not been accepted (Settings → Safety)."

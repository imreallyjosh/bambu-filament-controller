import ipaddress

from flask_wtf import FlaskForm
from wtforms import (BooleanField, DateTimeLocalField, FloatField, HiddenField, IntegerField, PasswordField,
                     RadioField, SelectField, StringField,
                     TextAreaField)
from wtforms.validators import URL, DataRequired, EqualTo, Length, NumberRange, Optional, Regexp, ValidationError

from .capabilities import MODEL_CHOICES
from .models import VerificationStatus

_VS_CHOICES = [(v, v.title()) for v in VerificationStatus.ALL]
_EMAIL = Regexp(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", message="Enter an email address")


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("Password", validators=[DataRequired()])


class PasswordChangeForm(FlaskForm):
    current = PasswordField("Current password", validators=[DataRequired()])
    new = PasswordField("New password", validators=[DataRequired(), Length(min=10)])
    confirm = PasswordField("Repeat new password", validators=[EqualTo("new", "Passwords must match")])


def _ip(form, field):
    try:
        addr = ipaddress.ip_address(field.data.strip())
    except ValueError as exc:
        raise ValidationError("Enter an IPv4/IPv6 address (reserve it in DHCP).") from exc
    if not addr.is_private:
        raise ValidationError("Printer address must be on your local network.")


class PrinterForm(FlaskForm):
    name = StringField("Friendly name", validators=[DataRequired(), Length(max=80)])
    model = SelectField("Model", choices=MODEL_CHOICES, validators=[DataRequired()])
    ip_address = StringField("IP address", validators=[DataRequired(), _ip])
    serial = StringField(
        "Serial number",
        validators=[DataRequired(), Regexp(r"^[A-Z0-9]{8,32}$", message="Uppercase letters/digits as shown on the printer")],
        filters=[lambda s: s.strip().upper() if s else s],
    )
    access_code = PasswordField(
        "LAN access code",
        description="Leave blank when editing to keep the stored code.",
        validators=[Optional(), Regexp(r"^[A-Za-z0-9]{8}$", message="8-character code from the printer screen")],
    )
    firmware_version = StringField("Firmware version (as shown on printer)", validators=[Optional(), Length(max=32)])
    network_mode = SelectField(
        "Network mode",
        choices=[("", "Unknown"), ("cloud", "Cloud (normal)"), ("lan_only", "LAN Only"),
                 ("lan_only_developer", "LAN Only + Developer Mode")],
        validators=[Optional()],
    )
    max_bed_temp_c = IntegerField(
        "Your bed temperature cap (°C)", validators=[Optional(), NumberRange(min=30, max=120)],
        description="Never higher than the model's spec maximum; the lower value always wins.",
    )
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=2000)])


class DryingPresetForm(FlaskForm):
    manufacturer = StringField("Manufacturer", validators=[DataRequired(), Length(max=80)])
    material = StringField("Material", validators=[DataRequired(), Length(max=40)])
    product_name = StringField("Product name", validators=[DataRequired(), Length(max=120)])
    ref_air_temp_c = FloatField("Recommended drying-air temp (°C)", validators=[Optional(), NumberRange(20, 200)])
    ref_air_temp_max_c = FloatField("…upper end of range (°C)", validators=[Optional(), NumberRange(20, 200)])
    ref_duration_min_h = FloatField("Recommended duration, min (h)", validators=[Optional(), NumberRange(0, 96)])
    ref_duration_max_h = FloatField("Recommended duration, max (h)", validators=[Optional(), NumberRange(0, 96)])
    max_temp_c = FloatField("Maximum permitted temperature for material/spool (°C)", validators=[Optional(), NumberRange(20, 200)])
    spool_requirements = TextAreaField("Spool material / heat resistance", validators=[Optional()])
    rotation_required = BooleanField("Spool rotation required")
    cover_requirements = TextAreaField("Drying-cover requirements", validators=[Optional()])
    source_reference = TextAreaField("Source / reference (URL, TDS revision)", validators=[Optional()])
    verification_status = SelectField("Verification status", choices=_VS_CHOICES)
    notes = TextAreaField("Notes", validators=[Optional()])

    def validate_verification_status(self, field):
        if field.data in (VerificationStatus.REFERENCE, VerificationStatus.VERIFIED) and not (self.source_reference.data or "").strip():
            raise ValidationError("A source reference is required for this status.")


class PresetPrinterSettingForm(FlaskForm):
    bed_target_c = FloatField("Heated-bed target (°C)", validators=[Optional(), NumberRange(20, 120)])
    drying_time_h = FloatField("Drying time on this printer (h)", validators=[Optional(), NumberRange(0.25, 48)],
                               description="Pre-fills the Drying form. Counted from reaching the bed target.")
    expected_enclosure_c = FloatField("Expected enclosure temp (°C) — an estimate", validators=[Optional(), NumberRange(10, 120)])
    max_bed_c = FloatField("Max bed temp for this preset (°C)", validators=[Optional(), NumberRange(20, 120)])
    fan_setting = StringField("Fan setting", validators=[Optional(), Length(max=80)])
    verification_status = SelectField("Verification status", choices=_VS_CHOICES)
    verification_notes = TextAreaField("Test notes (thermometer, placement, results)", validators=[Optional()])

    def validate_verification_status(self, field):
        if field.data == VerificationStatus.VERIFIED and not (self.verification_notes.data or "").strip():
            raise ValidationError("Describe the test that verified this setting.")


class AnnealingPresetForm(FlaskForm):
    manufacturer = StringField("Manufacturer", validators=[DataRequired(), Length(max=80)])
    material = StringField("Material", validators=[DataRequired(), Length(max=40)])
    anneal_temp_c = FloatField("Annealing temperature (°C)", validators=[Optional(), NumberRange(20, 300)])
    hold_minutes = FloatField("Holding duration (min)", validators=[Optional(), NumberRange(0, 10000)])
    heat_ramp_c_per_h = FloatField("Heating ramp (°C/h)", validators=[Optional(), NumberRange(0, 1000)])
    cool_ramp_c_per_h = FloatField("Cooling ramp (°C/h)", validators=[Optional(), NumberRange(0, 1000)])
    max_temp_c = FloatField("Maximum permitted temperature (°C)", validators=[Optional(), NumberRange(20, 300)])
    distortion_warning = TextAreaField("Shrinkage / distortion warnings", validators=[Optional()])
    required_equipment = TextAreaField("Required equipment", validators=[Optional()])
    source_reference = TextAreaField("Source documentation", validators=[Optional()])
    verification_status = SelectField("Verification status", choices=_VS_CHOICES)
    notes = TextAreaField("Notes", validators=[Optional()])


class NtfyForm(FlaskForm):
    enabled = BooleanField("Enable ntfy")
    url = StringField("Server URL", validators=[Optional(), URL(require_tld=False)])
    topic = StringField("Topic", validators=[Optional(), Regexp(r"^[A-Za-z0-9_-]{1,64}$")])
    token = PasswordField("Access token (leave blank to keep)", validators=[Optional()])
    username = StringField("Username (if not using a token)", validators=[Optional()])
    password = PasswordField("Password (leave blank to keep)", validators=[Optional()])
    clear_credentials = BooleanField("Clear stored credentials")
    priority = SelectField("Priority for info events", choices=[("1", "1 min"), ("2", "2 low"), ("3", "3 default"),
                                                                ("4", "4 high"), ("5", "5 urgent")])


class EmailForm(FlaskForm):
    enabled = BooleanField("Enable email")
    host = StringField("SMTP host", validators=[Optional()])
    port = IntegerField("SMTP port", validators=[Optional(), NumberRange(1, 65535)], default=587)
    starttls = BooleanField("Use STARTTLS", default=True)
    username = StringField("SMTP username", validators=[Optional()])
    password = PasswordField("SMTP password (leave blank to keep)", validators=[Optional()])
    sender = StringField("From address", validators=[Optional(), _EMAIL])
    to = StringField("To address", validators=[Optional(), _EMAIL])


class SafetySettingsForm(FlaskForm):
    max_heating_minutes = IntegerField(
        "Absolute maximum heating duration (minutes)", validators=[DataRequired(), NumberRange(10, 48 * 60)]
    )
    disconnect_timeout_s = IntegerField(
        "Telemetry loss before declaring disconnect (s)", validators=[DataRequired(), NumberRange(10, 600)]
    )
    hardware_cutoff_description = TextAreaField(
        "Independent hardware cutoff installed (describe device, rating, trip temperature)", validators=[Optional()]
    )
    hardware_cutoff_confirmed = BooleanField(
        "I confirm an independent, appropriately rated thermal cutoff is installed and tested"
    )
    no_cutoff_risk_accepted = BooleanField(
        "Allow scheduled/unattended heating WITHOUT an independent cutoff. I understand that if this server, the "
        "network or the printer firmware stops responding, the bed may stay at its target until someone switches it off."
    )


class HeaterTestForm(FlaskForm):
    target_c = IntegerField("Bed target (°C)", default=40, validators=[DataRequired(), NumberRange(30, 50)],
                            description="Tests are limited to 30–50 °C.")
    minutes = IntegerField("Duration (minutes)", default=5, validators=[DataRequired(), NumberRange(1, 90)],
                           description="5 min checks the command works; 70 min checks the print-hour counter.")
    print_hours_before = FloatField("Printer's total print hours right now", validators=[DataRequired(), NumberRange(0)],
                                    description="Read it from the printer screen before starting.")
    present = BooleanField("I am at the printer and will stay nearby for the whole test",
                           validators=[DataRequired("Required")])
    bed_clear = BooleanField("The bed is empty and nothing heat-sensitive is on or near it",
                             validators=[DataRequired("Required")])
    stop_known = BooleanField("I know how to stop it: the Stop button here, or set the bed to 0 on the touchscreen",
                              validators=[DataRequired("Required")])


class PrintHoursAfterForm(FlaskForm):
    print_hours_after = FloatField("Print hours after the test", validators=[DataRequired(), NumberRange(0)])


class DryingStartForm(FlaskForm):
    printer_id = HiddenField(validators=[DataRequired()])
    preset_id = HiddenField(validators=[DataRequired()])
    spool_size_g = IntegerField("Spool size (g)", validators=[Optional(), NumberRange(1, 10000)],
                                description="Recorded only; it does not change the drying time.")
    target_bed_c = IntegerField("Bed target (°C)", validators=[DataRequired(), NumberRange(30, 120)])
    stop_mode = RadioField("Stop", choices=[("duration", "After a drying time (counted from reaching the target)"),
                                            ("end_time", "At a specific date and time")], default="duration")
    duration_h = FloatField("Drying time (hours)", validators=[Optional(), NumberRange(0.25, 48)])
    end_at = DateTimeLocalField("Stop at", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    start_mode = RadioField("Start", choices=[("now", "Now"), ("scheduled", "At a scheduled date and time")],
                            default="now")
    start_at = DateTimeLocalField("Start at", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    repeat_rule = SelectField("Repeat", choices=[("", "Don't repeat"), ("daily", "Daily"), ("weekly", "Weekly")],
                              validators=[Optional()])
    flip_reminder = BooleanField("Remind me (ntfy) to flip the spool halfway", default=True)
    prep_spool = BooleanField("Spool is on the bed and the bed is otherwise clear", validators=[DataRequired("Required")])
    prep_cover = BooleanField("Drying cover is in place; door and lid are closed", validators=[DataRequired("Required")])
    prep_spool_heat = BooleanField("The spool and its hub can withstand this bed temperature",
                                   validators=[DataRequired("Required")])
    prep_probe = BooleanField("I understand the displayed temperature is the bed heater, not the filament "
                              "(no independent probe is connected)", validators=[DataRequired("Required")])
    accept_unverified = BooleanField("I accept that this printer setting hasn't been verified by a test")


class SchedulePrepForm(FlaskForm):
    prep_spool = BooleanField("Spool is on the bed and the bed is otherwise clear", validators=[DataRequired("Required")])
    prep_cover = BooleanField("Drying cover is in place; door and lid are closed", validators=[DataRequired("Required")])
    prep_spool_heat = BooleanField("The spool and its hub can withstand this bed temperature",
                                   validators=[DataRequired("Required")])


class AnnealingStartForm(FlaskForm):
    printer_id = HiddenField(validators=[DataRequired()])
    preset_id = HiddenField(validators=[DataRequired()])
    anneal_temp_c = IntegerField("Annealing (hold) temperature (°C)", validators=[DataRequired(), NumberRange(30, 120)])
    hold_h = FloatField("Hold time (hours)", validators=[DataRequired(), NumberRange(0.25, 48)],
                        description="Counted from reaching the annealing temperature.")
    heat_ramp_c_per_h = FloatField("Heating ramp (°C per hour)", validators=[Optional(), NumberRange(1, 200)],
                                   description="Blank = heat at the bed's full rate.")
    cool_ramp_c_per_h = FloatField("Cooling ramp (°C per hour)", validators=[Optional(), NumberRange(1, 200)],
                                   description="Blank = switch the heater off after the hold and let it cool naturally.")
    cool_end_c = IntegerField("End controlled cool-down at (°C)", default=40, validators=[Optional(), NumberRange(25, 100)],
                              description="The heater switches off here; the rest is natural cooling.")
    prep_part = BooleanField("The part is on the bed and nothing else is on or near the bed",
                             validators=[DataRequired("Required")])
    prep_closed = BooleanField("Door and lid are closed (any cover over the part is in place)",
                               validators=[DataRequired("Required")])
    prep_warp = BooleanField("I understand heat comes from the bed side only and the part may shrink or warp",
                             validators=[DataRequired("Required")])

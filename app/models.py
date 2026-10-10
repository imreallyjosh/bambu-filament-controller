"""Database schema.

All timestamps are stored as naive UTC and converted to the configured local
timezone for display (see app.timeutil).

Tables used by later phases (process, schedule, telemetry, command_request)
are defined now so the schema is complete and later migrations are additive.
"""
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import crypto
from .extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class VerificationStatus:
    UNSOURCED = "unsourced"        # no source yet; values must be filled in
    REFERENCE = "reference"        # copied from a cited manufacturer source, not tested here
    UNVERIFIED = "unverified"      # printer-specific value entered but not tested
    VERIFIED = "verified"          # tested on this hardware with an independent thermometer
    ALL = [UNSOURCED, REFERENCE, UNVERIFIED, VERIFIED]


class ProcessState:
    PENDING = "pending"
    AWAITING_PREPARATION = "awaiting_preparation"
    HEATING = "heating"
    DRYING = "drying"
    COOLING = "cooling"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    SAFETY_SHUTDOWN = "safety_shutdown"
    ACTIVE = {HEATING, DRYING, COOLING}
    TERMINAL = {COMPLETED, STOPPED, FAILED, SAFETY_SHUTDOWN}
    ALL = [PENDING, AWAITING_PREPARATION, HEATING, DRYING, COOLING, COMPLETED, STOPPED, FAILED, SAFETY_SHUTDOWN]


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Printer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    model = db.Column(db.String(16), nullable=False)  # key into capabilities.PROFILES
    ip_address = db.Column(db.String(64), nullable=False)
    serial = db.Column(db.String(32), unique=True, nullable=False)
    access_code_enc = db.Column(db.Text, nullable=False)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    # Reported by the printer (get_version); user-entered value kept separately.
    firmware_version = db.Column(db.String(32))
    firmware_version_reported = db.Column(db.String(32))
    network_mode = db.Column(db.String(32))  # e.g. "cloud", "lan_only", "lan_only_developer" (user-declared)
    # SHA-256 of the printer's TLS certificate, pinned on first successful connect.
    tls_fingerprint = db.Column(db.String(95))
    # User cap; the effective limit is min(this, model spec maximum).
    max_bed_temp_c = db.Column(db.Integer)
    # Set only after the Phase 4 supervised test passes on this printer + firmware.
    heater_control_verified = db.Column(db.Boolean, default=False, nullable=False)
    heater_control_verified_firmware = db.Column(db.String(32))
    heater_control_verified_at = db.Column(db.DateTime)
    print_hours_test_notes = db.Column(db.Text)
    # Set when the owner confirms the printer was homed, bed lowered and toolhead parked (before loading the bed).
    # Cleared when a print starts, because the bed will have moved.
    motion_prepared_at = db.Column(db.DateTime)
    motion_prepared_by = db.Column(db.String(64))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    status = db.relationship("PrinterStatus", uselist=False, back_populates="printer", cascade="all, delete-orphan")

    @property
    def access_code(self) -> str | None:
        return crypto.decrypt(self.access_code_enc)

    @access_code.setter
    def access_code(self, value: str) -> None:
        self.access_code_enc = crypto.encrypt(value)

    def heater_control_allowed(self, global_switch: bool) -> tuple[bool, str]:
        """Interlock check. Every condition must pass; firmware changes revoke verification."""
        if not global_switch:
            return False, "Heater control is disabled globally (BFC_ALLOW_HEATER_CONTROL)."
        if not self.enabled:
            return False, "Printer is disabled."
        if not self.heater_control_verified:
            return False, "Heater control has not been verified on this printer."
        if self.firmware_version_reported != self.heater_control_verified_firmware:
            return False, "Firmware changed since heater control was verified; re-test required."
        return True, "OK"


class PrinterStatus(db.Model):
    """Latest telemetry snapshot, written only by the controller daemon."""

    printer_id = db.Column(db.Integer, db.ForeignKey("printer.id", ondelete="CASCADE"), primary_key=True)
    online = db.Column(db.Boolean, default=False, nullable=False)
    last_seen_at = db.Column(db.DateTime)
    last_error = db.Column(db.Text)
    gcode_state = db.Column(db.String(32))
    stage = db.Column(db.String(64))
    bed_temp_c = db.Column(db.Float)
    bed_target_c = db.Column(db.Float)
    nozzle_temp_c = db.Column(db.Float)
    nozzle_target_c = db.Column(db.Float)
    # Only populated when the model has a chamber sensor.
    chamber_temp_c = db.Column(db.Float)
    hms_json = db.Column(db.Text)
    raw_json = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    printer = db.relationship("Printer", back_populates="status")


class EnclosureSensor(db.Model):
    """Independent temperature sensor (e.g. MQTT probe on your mosquitto broker)."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    printer_id = db.Column(db.Integer, db.ForeignKey("printer.id", ondelete="SET NULL"))
    kind = db.Column(db.String(32), nullable=False, default="mqtt")  # mqtt | http
    source = db.Column(db.String(255), nullable=False)  # topic or URL
    json_path = db.Column(db.String(128))  # dotted path into JSON payload, blank = raw number
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    last_value_c = db.Column(db.Float)
    last_seen_at = db.Column(db.DateTime)


class DryingPreset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    manufacturer = db.Column(db.String(80), nullable=False)
    material = db.Column(db.String(40), nullable=False)
    product_name = db.Column(db.String(120), nullable=False)
    is_template = db.Column(db.Boolean, default=False, nullable=False)
    # Manufacturer reference values for a filament dryer/oven. NOT bed targets.
    ref_air_temp_c = db.Column(db.Float)
    ref_air_temp_max_c = db.Column(db.Float)
    ref_duration_min_h = db.Column(db.Float)
    ref_duration_max_h = db.Column(db.Float)
    # Hard limit for the material (e.g. below Tg / spool limit); printer limits applied separately.
    max_temp_c = db.Column(db.Float)
    spool_requirements = db.Column(db.Text)
    rotation_required = db.Column(db.Boolean, default=False, nullable=False)
    cover_requirements = db.Column(db.Text)
    source_reference = db.Column(db.Text)
    verification_status = db.Column(db.String(16), default=VerificationStatus.UNSOURCED, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    printer_settings = db.relationship(
        "DryingPresetPrinterSetting", back_populates="preset", cascade="all, delete-orphan"
    )

    @property
    def label(self) -> str:
        return f"{self.manufacturer} {self.product_name}"

    def setting_for(self, model: str):
        return next((s for s in self.printer_settings if s.printer_model == model), None)


class DryingPresetPrinterSetting(db.Model):
    """Printer-model-specific operating values. Entered and verified separately."""

    __table_args__ = (db.UniqueConstraint("preset_id", "printer_model"),)
    id = db.Column(db.Integer, primary_key=True)
    preset_id = db.Column(db.Integer, db.ForeignKey("drying_preset.id", ondelete="CASCADE"), nullable=False)
    printer_model = db.Column(db.String(16), nullable=False)
    bed_target_c = db.Column(db.Float)
    drying_time_h = db.Column(db.Float)  # bed-drying time for this printer (not the oven time)
    expected_enclosure_c = db.Column(db.Float)  # an expectation, never displayed as a measurement
    max_bed_c = db.Column(db.Float)
    fan_setting = db.Column(db.String(80))
    verification_status = db.Column(db.String(16), default=VerificationStatus.UNVERIFIED, nullable=False)
    verification_notes = db.Column(db.Text)

    preset = db.relationship("DryingPreset", back_populates="printer_settings")


class AnnealingPreset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    manufacturer = db.Column(db.String(80), nullable=False)
    material = db.Column(db.String(40), nullable=False)
    is_template = db.Column(db.Boolean, default=False, nullable=False)
    anneal_temp_c = db.Column(db.Float)
    hold_minutes = db.Column(db.Float)
    heat_ramp_c_per_h = db.Column(db.Float)
    cool_ramp_c_per_h = db.Column(db.Float)
    max_temp_c = db.Column(db.Float)
    distortion_warning = db.Column(db.Text)
    required_equipment = db.Column(db.Text)
    source_reference = db.Column(db.Text)
    verification_status = db.Column(db.String(16), default=VerificationStatus.UNSOURCED, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Process(db.Model):
    """A drying, annealing, or heater-test run. Heating runs are supervised by the controller daemon."""

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(16), nullable=False)  # drying | annealing | heater_test
    printer_id = db.Column(db.Integer, db.ForeignKey("printer.id", ondelete="SET NULL"))
    drying_preset_id = db.Column(db.Integer, db.ForeignKey("drying_preset.id", ondelete="SET NULL"))
    annealing_preset_id = db.Column(db.Integer, db.ForeignKey("annealing_preset.id", ondelete="SET NULL"))
    schedule_id = db.Column(db.Integer, db.ForeignKey("schedule.id", ondelete="SET NULL"))
    # Snapshots so history survives preset/printer edits.
    printer_name = db.Column(db.String(80))
    material_label = db.Column(db.String(160))
    spool_size_g = db.Column(db.Integer)
    state = db.Column(db.String(24), default=ProcessState.PENDING, nullable=False)
    target_bed_c = db.Column(db.Float)
    max_temp_c = db.Column(db.Float)
    planned_duration_min = db.Column(db.Integer)
    scheduled_start_at = db.Column(db.DateTime)
    scheduled_end_at = db.Column(db.DateTime)
    started_at = db.Column(db.DateTime)
    target_reached_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)
    prep_confirmed_at = db.Column(db.DateTime)
    prep_confirmed_by = db.Column(db.String(64))
    heater_off_verified = db.Column(db.Boolean, default=False, nullable=False)
    max_bed_observed_c = db.Column(db.Float)
    max_enclosure_observed_c = db.Column(db.Float)
    error = db.Column(db.Text)
    notes = db.Column(db.Text)
    # Heater-test bookkeeping: print-hour counter before/after, firmware at test time.
    print_hours_before = db.Column(db.Float)
    print_hours_after = db.Column(db.Float)
    firmware_at_start = db.Column(db.String(32))
    flip_reminder = db.Column(db.Boolean, default=False, nullable=False)
    flip_notified_at = db.Column(db.DateTime)
    # Temperature program (annealing): ramp up, hold, controlled ramp down. Blank ramp = no ramp.
    heat_ramp_c_per_h = db.Column(db.Float)
    cool_ramp_c_per_h = db.Column(db.Float)
    cool_end_c = db.Column(db.Float)  # controlled cool-down ends here, then heater off
    ramp_start_c = db.Column(db.Float)
    current_setpoint_c = db.Column(db.Float)  # last bed target commanded
    cooldown_started_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    @property
    def is_active(self) -> bool:
        return self.state in ProcessState.ACTIVE or self.state == ProcessState.PENDING


class Schedule(db.Model):
    """A future drying session. start_at is always the NEXT occurrence (advanced after each run).

    Each occurrence needs its own physical-preparation confirmation (prep_confirmed_for == start_at).
    """

    REPEAT_RULES = (None, "daily", "weekly")

    id = db.Column(db.Integer, primary_key=True)
    printer_id = db.Column(db.Integer, db.ForeignKey("printer.id", ondelete="CASCADE"), nullable=False)
    drying_preset_id = db.Column(db.Integer, db.ForeignKey("drying_preset.id", ondelete="CASCADE"), nullable=False)
    start_at = db.Column(db.DateTime, nullable=False)
    duration_min = db.Column(db.Integer)  # drying time counted from reaching the target
    end_at = db.Column(db.DateTime)  # one-off only: stop at this clock time instead
    repeat_rule = db.Column(db.String(64))  # None = one-off | "daily" | "weekly"
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    target_bed_c = db.Column(db.Float)
    spool_size_g = db.Column(db.Integer)
    flip_reminder = db.Column(db.Boolean, default=True, nullable=False)
    prep_confirmed_for = db.Column(db.DateTime)  # equals start_at when this occurrence is confirmed
    prep_confirmed_by = db.Column(db.String(64))
    approach_notified_for = db.Column(db.DateTime)
    last_result = db.Column(db.Text)
    created_by = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    printer = db.relationship("Printer")
    preset = db.relationship("DryingPreset")

    @property
    def prepared(self) -> bool:
        return self.prep_confirmed_for is not None and self.prep_confirmed_for == self.start_at


class TelemetrySample(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    printer_id = db.Column(db.Integer, db.ForeignKey("printer.id", ondelete="CASCADE"), nullable=False, index=True)
    process_id = db.Column(db.Integer, db.ForeignKey("process.id", ondelete="SET NULL"), index=True)
    ts = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    bed_temp_c = db.Column(db.Float)
    bed_target_c = db.Column(db.Float)
    nozzle_temp_c = db.Column(db.Float)
    chamber_temp_c = db.Column(db.Float)  # printer's own chamber sensor (X1 only)
    enclosure_temp_c = db.Column(db.Float)  # independent sensor, measured


class CommandRequest(db.Model):
    """Web -> controller daemon command queue. The daemon is the only MQTT publisher."""

    id = db.Column(db.Integer, primary_key=True)
    printer_id = db.Column(db.Integer, db.ForeignKey("printer.id", ondelete="CASCADE"), nullable=False)
    process_id = db.Column(db.Integer, db.ForeignKey("process.id", ondelete="SET NULL"))
    command = db.Column(db.String(32), nullable=False)
    payload_json = db.Column(db.Text)
    status = db.Column(db.String(16), default="queued", nullable=False)  # queued|sent|confirmed|failed
    requested_by = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    processed_at = db.Column(db.DateTime)
    result = db.Column(db.Text)


class EventLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    level = db.Column(db.String(10), default="info", nullable=False)  # info|warning|error|critical
    category = db.Column(db.String(32), nullable=False)  # auth|printer|process|safety|notify|system
    printer_id = db.Column(db.Integer, db.ForeignKey("printer.id", ondelete="SET NULL"))
    process_id = db.Column(db.Integer, db.ForeignKey("process.id", ondelete="SET NULL"))
    actor = db.Column(db.String(64))
    message = db.Column(db.Text, nullable=False)


class Alert(db.Model):
    """Web dashboard alert; one per notification event."""

    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    event_type = db.Column(db.String(40), nullable=False)
    severity = db.Column(db.String(10), default="info", nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text)
    acknowledged = db.Column(db.Boolean, default=False, nullable=False)
    delivery_json = db.Column(db.Text)  # per-channel delivery results


class Setting(db.Model):
    """Key/value settings. Secret values are stored encrypted (secret=True)."""

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)
    secret = db.Column(db.Boolean, default=False, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @staticmethod
    def get(key: str, default=None):
        row = db.session.get(Setting, key)
        if row is None or row.value is None:
            return default
        return crypto.decrypt(row.value) if row.secret else row.value

    @staticmethod
    def get_bool(key: str, default: bool = False) -> bool:
        val = Setting.get(key)
        return default if val is None else val == "1"

    @staticmethod
    def set(key: str, value, secret: bool = False) -> None:
        row = db.session.get(Setting, key) or Setting(key=key)
        if isinstance(value, bool):
            value = "1" if value else "0"
        if value is not None and not isinstance(value, str):
            value = str(value)
        row.secret = secret
        row.value = crypto.encrypt(value) if (secret and value) else value
        db.session.add(row)


def log_event(category: str, message: str, level: str = "info", printer_id=None, process_id=None, actor=None):
    db.session.add(
        EventLog(category=category, message=message, level=level, printer_id=printer_id,
                 process_id=process_id, actor=actor)
    )

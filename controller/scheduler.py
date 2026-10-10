"""Persistent drying scheduler.

Schedules live in the SQLite `schedule` table and are evaluated by the controller daemon's
main loop, so they survive restarts by construction (no separate job store to get out of sync).

Rules:
  * A session starts only within START_GRACE_S of its start time. If the controller was down
    or busy past that window, the occurrence is MISSED and reported, never started late.
  * Every occurrence needs its own physical-preparation confirmation (Schedule.prepared).
  * Unattended heating must be permitted (hardware cutoff or accepted risk) at start time.
  * Repeating schedules advance on the local wall clock, so DST changes keep the same local time.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models import CommandRequest, Printer, Process, ProcessState, Schedule, Setting, log_event, utcnow
from app.notifications import notify
from app.safety import unattended_heating_allowed
from app.timeutil import fmt, local_tz

log = logging.getLogger("bambu-fc-controller.scheduler")

START_GRACE_S = 5 * 60
DEFAULT_APPROACH_MIN = 30


def next_occurrence(start_utc: datetime, rule: str | None, now_utc: datetime) -> datetime | None:
    """Next start after now for a repeating rule, stepping in local wall-clock time."""
    if rule not in ("daily", "weekly"):
        return None
    step = timedelta(days=1 if rule == "daily" else 7)
    tz = local_tz()
    local = start_utc.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)  # naive wall clock
    while True:
        local += step
        candidate = local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
        if candidate > now_utc:
            return candidate


def material_label(schedule: Schedule) -> str:
    return schedule.preset.label if schedule.preset else "?"


class Scheduler:
    def tick(self) -> None:
        now = utcnow()
        approach_min = int(Setting.get("notify.approach_minutes", DEFAULT_APPROACH_MIN))
        for sch in Schedule.query.filter_by(enabled=True).order_by(Schedule.start_at).all():
            if sch.start_at - timedelta(minutes=approach_min) <= now < sch.start_at \
                    and sch.approach_notified_for != sch.start_at:
                self._approaching(sch)
            if now >= sch.start_at:
                if (now - sch.start_at).total_seconds() > START_GRACE_S:
                    self._missed(sch, now)
                else:
                    self._start(sch)
        db.session.commit()

    def _approaching(self, sch: Schedule) -> None:
        sch.approach_notified_for = sch.start_at
        db.session.commit()
        prep = ("Preparation confirmed." if sch.prepared else
                "Preparation NOT confirmed yet: confirm it on the Schedules page or this session will not start.")
        notify("schedule_approaching", printer=sch.printer.name, material=material_label(sch),
               process=f"schedule #{sch.id}", details=f"Starts at {fmt(sch.start_at)}. {prep}")

    def _missed(self, sch: Schedule, now: datetime) -> None:
        reason = (f"Start time {fmt(sch.start_at)} passed while the controller could not start it "
                  "(server or controller down?). Sessions are never started late.")
        self._fail(sch, reason)

    def _fail(self, sch: Schedule, reason: str) -> None:
        sch.last_result = reason
        log_event("process", f"Schedule #{sch.id} did not start: {reason}", level="warning",
                  printer_id=sch.printer_id, actor="scheduler")
        self._advance(sch)
        db.session.commit()
        notify("schedule_failed", printer=sch.printer.name, material=material_label(sch),
               process=f"schedule #{sch.id}", details=reason)

    def _start(self, sch: Schedule) -> None:
        printer: Printer = sch.printer
        if not sch.prepared:
            return self._fail(sch, "Physical preparation was not confirmed for this session.")
        ok, why = unattended_heating_allowed()
        if not ok:
            return self._fail(sch, why)
        if Process.query.filter(Process.printer_id == printer.id,
                                Process.state.in_(ProcessState.ACTIVE | {ProcessState.PENDING})).count():
            return self._fail(sch, "Another operation is active on this printer.")
        setting = sch.preset.setting_for(printer.model) if sch.preset else None
        target = sch.target_bed_c or (setting.bed_target_c if setting else None)
        if not target:
            return self._fail(sch, f"No {printer.model} bed target for this preset.")
        proc = Process(
            kind="drying", printer_id=printer.id, drying_preset_id=sch.drying_preset_id, schedule_id=sch.id,
            printer_name=printer.name, material_label=material_label(sch), spool_size_g=sch.spool_size_g,
            state=ProcessState.PENDING, target_bed_c=target, planned_duration_min=sch.duration_min,
            scheduled_start_at=sch.start_at, scheduled_end_at=sch.end_at if not sch.repeat_rule else None,
            prep_confirmed_at=sch.prep_confirmed_for, prep_confirmed_by=sch.prep_confirmed_by,
            flip_reminder=sch.flip_reminder,
        )
        db.session.add(proc)
        db.session.flush()
        db.session.add(CommandRequest(printer_id=printer.id, process_id=proc.id, command="start_drying",
                                      requested_by=f"schedule #{sch.id}"))
        sch.last_result = f"Started as drying #{proc.id} at {fmt(utcnow())}."
        log_event("process", f"Schedule #{sch.id} started drying #{proc.id}.", printer_id=printer.id,
                  process_id=proc.id, actor="scheduler")
        self._advance(sch)
        return None

    def _advance(self, sch: Schedule) -> None:
        nxt = next_occurrence(sch.start_at, sch.repeat_rule, utcnow())
        if nxt is None:
            sch.enabled = False
        else:
            sch.start_at = nxt  # the new occurrence needs a fresh preparation confirmation

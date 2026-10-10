from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import Alert, CommandRequest, DryingPreset, Printer, Process, Schedule, Setting, utcnow
from controller.scheduler import Scheduler, next_occurrence


@pytest.fixture()
def setup(app):
    app.config["TIMEZONE"] = "America/Chicago"
    printer = Printer(name="P1S", model="P1S", ip_address="192.168.1.80", serial="01P00A000000001", enabled=True)
    printer.access_code = "abcd1234"
    preset = DryingPreset.query.filter_by(product_name="PETG").one()
    preset.setting_for("P1S").bed_target_c = 70
    db.session.add(printer)
    Setting.set("safety.no_cutoff_risk_accepted", True)
    db.session.commit()
    return printer, preset


def make(printer, preset, start, prepared=True, repeat=None, minutes=120):
    sch = Schedule(printer_id=printer.id, drying_preset_id=preset.id, start_at=start, duration_min=minutes,
                   repeat_rule=repeat, target_bed_c=70, flip_reminder=True)
    if prepared:
        sch.prep_confirmed_for = start
    db.session.add(sch)
    db.session.commit()
    return sch


def events():
    return [a.event_type for a in Alert.query.order_by(Alert.id)]


def test_due_prepared_schedule_starts_once(setup):
    printer, preset = setup
    sch = make(printer, preset, utcnow() - timedelta(seconds=30))
    Scheduler().tick()
    Scheduler().tick()
    proc = Process.query.one()
    assert proc.kind == "drying" and proc.schedule_id == sch.id and proc.target_bed_c == 70 and proc.flip_reminder
    assert proc.prep_confirmed_at is not None
    assert CommandRequest.query.one().command == "start_drying"
    assert not sch.enabled  # one-off


def test_unprepared_session_is_skipped(setup):
    printer, preset = setup
    sch = make(printer, preset, utcnow() - timedelta(seconds=30), prepared=False)
    Scheduler().tick()
    assert Process.query.count() == 0 and "schedule_failed" in events() and not sch.enabled
    assert "not confirmed" in sch.last_result


def test_missed_session_never_starts_late(setup):
    printer, preset = setup
    sch = make(printer, preset, utcnow() - timedelta(minutes=20))
    Scheduler().tick()
    assert Process.query.count() == 0 and "never started late" in sch.last_result and "schedule_failed" in events()


def test_unattended_not_permitted(setup):
    printer, preset = setup
    Setting.set("safety.no_cutoff_risk_accepted", False)
    db.session.commit()
    make(printer, preset, utcnow() - timedelta(seconds=30))
    Scheduler().tick()
    assert Process.query.count() == 0 and "schedule_failed" in events()


def test_repeat_advances_and_needs_new_confirmation(setup):
    printer, preset = setup
    start = utcnow() - timedelta(seconds=30)
    sch = make(printer, preset, start, repeat="daily")
    Scheduler().tick()
    assert Process.query.count() == 1 and sch.enabled
    assert timedelta(hours=23) <= sch.start_at - start <= timedelta(hours=25)
    assert not sch.prepared  # each occurrence needs its own confirmation


def test_approaching_notification_once(setup):
    printer, preset = setup
    make(printer, preset, utcnow() + timedelta(minutes=10), prepared=False)
    Scheduler().tick()
    Scheduler().tick()
    assert events() == ["schedule_approaching"]
    assert "NOT confirmed" in Alert.query.one().body


def test_next_occurrence_keeps_local_time_across_dst(app):
    app.config["TIMEZONE"] = "America/Chicago"
    # 2026-10-31 08:00 CDT = 13:00 UTC. DST ends 2026-11-01, so 08:00 CST = 14:00 UTC.
    nxt = next_occurrence(datetime(2026, 10, 31, 13, 0), "daily", datetime(2026, 10, 31, 14, 0))
    assert nxt == datetime(2026, 11, 1, 14, 0)
    assert next_occurrence(datetime(2026, 10, 31, 13, 0), None, datetime(2026, 10, 31, 14, 0)) is None

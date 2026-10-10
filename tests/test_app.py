from unittest import mock

import pytest
import requests

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import (Alert, DryingPreset, EventLog, Printer, Process, ProcessState, Setting, VerificationStatus, utcnow)
from app.notifications import notify
from controller.daemon import recover_interrupted_processes

from .conftest import PRINTER

PAGES = ["/", "/printers/", "/drying/", "/annealing/", "/presets/", "/schedules/", "/history/",
         "/notifications/", "/settings/", "/history/export.csv"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_require_login(client, path):
    resp = client.get(path)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(auth_client, path):
    assert auth_client.get(path).status_code == 200


def test_bad_login_rejected(client):
    resp = client.post("/login", data={"username": "admin", "password": "nope"})
    assert b"Invalid username or password" in resp.data


def test_open_redirect_blocked(client):
    resp = client.post("/login?next=//evil.example", data={"username": "admin", "password": "correct-horse-battery"})
    assert resp.headers["Location"] == "/"


def test_csrf_enforced_when_enabled():
    class CsrfConfig(TestConfig):
        WTF_CSRF_ENABLED = True

    app = create_app(CsrfConfig)
    with app.app_context():
        db.create_all()
        resp = app.test_client().post("/login", data={"username": "x", "password": "y"})
        assert resp.status_code == 400  # missing CSRF token
        db.drop_all()


def test_add_printer_encrypts_access_code(auth_client, app):
    resp = auth_client.post("/printers/new", data=PRINTER)
    assert resp.status_code == 302
    p = Printer.query.one()
    assert p.enabled is False  # new printers start disabled
    assert p.access_code_enc != "abcd1234" and "abcd1234" not in p.access_code_enc
    assert p.access_code == "abcd1234"


def test_edit_keeps_access_code_when_blank(auth_client):
    auth_client.post("/printers/new", data=PRINTER)
    p = Printer.query.one()
    auth_client.post(f"/printers/{p.id}/edit", data={**PRINTER, "access_code": "", "name": "Renamed"})
    db.session.refresh(p)
    assert p.name == "Renamed" and p.access_code == "abcd1234"


def test_bed_cap_cannot_exceed_model_spec(auth_client):
    resp = auth_client.post("/printers/new", data={**PRINTER, "max_bed_temp_c": "110"})
    assert resp.status_code == 200 and b"spec maximum of 100" in resp.data
    assert Printer.query.count() == 0


def test_public_ip_rejected(auth_client):
    resp = auth_client.post("/printers/new", data={**PRINTER, "ip_address": "8.8.8.8"})
    assert b"local network" in resp.data


def test_delete_requires_disabled(auth_client):
    auth_client.post("/printers/new", data=PRINTER)
    p = Printer.query.one()
    auth_client.post(f"/printers/{p.id}/toggle")
    auth_client.post(f"/printers/{p.id}/delete")
    assert Printer.query.count() == 1


def test_heater_interlock(app):
    p = Printer(name="x", model="X1", ip_address="192.168.1.2", serial="00M00A000000001", enabled=True)
    p.access_code = "abcd1234"
    assert p.heater_control_allowed(False)[0] is False
    assert p.heater_control_allowed(True)[0] is False  # not verified
    p.heater_control_verified = True
    p.heater_control_verified_firmware = "01.08.05.00"
    p.firmware_version_reported = "01.08.05.00"
    assert p.heater_control_allowed(True) == (True, "OK")
    p.firmware_version_reported = "01.09.00.00"  # firmware updated -> must re-test
    assert p.heater_control_allowed(True)[0] is False


def test_template_values_are_sourced_and_safe(app):
    from app.capabilities import bed_max_c

    templates = DryingPreset.query.filter_by(is_template=True).all()
    assert {(p.manufacturer, p.product_name) for p in templates} >= {("Siraya Tech", "Fibreheart PPA-CF"),
                                                                      ("Generic", "PETG"), ("Fiberon", "PA12-CF10")}
    assert not any(p.manufacturer == "Fiberon" and p.product_name == "PPA-CF" for p in templates)
    for preset in templates:
        # Every template now has a cited reference; the oven figure is never copied into a bed target.
        assert preset.verification_status == VerificationStatus.REFERENCE and preset.source_reference
        assert preset.ref_air_temp_c is not None
        assert {s.printer_model for s in preset.printer_settings} == {"P1S", "X1"}
        for st in preset.printer_settings:
            assert st.verification_notes  # every printer value explains where it came from
            assert st.verification_status != VerificationStatus.VERIFIED  # never pre-verified
            if st.bed_target_c is not None:
                # Room for the 8 °C over-temperature trip below the model's bed maximum.
                assert st.bed_target_c + 8 <= bed_max_c(st.printer_model) + 0.01 or st.bed_target_c <= 92
                assert st.bed_target_c <= bed_max_c(st.printer_model) and st.drying_time_h
    ppa = DryingPreset.query.filter_by(product_name="Generic PPA-CF").one()
    assert ppa.setting_for("P1S").bed_target_c is None and "Not possible" in ppa.setting_for("P1S").verification_notes
    assert (ppa.setting_for("X1").bed_target_c, ppa.setting_for("X1").drying_time_h) == (100, 18)
    assert DryingPreset.query.filter_by(product_name="Generic PA6-CF").one().setting_for("P1S").drying_time_h == 18


def test_printer_settings_not_overwritten(app):
    from app.seed import seed_presets

    preset = DryingPreset.query.filter_by(product_name="PETG").one()
    st = preset.setting_for("P1S")
    st.bed_target_c, st.drying_time_h, st.verification_notes = 70, 6, "my test"
    db.session.commit()
    seed_presets()
    assert (st.bed_target_c, st.drying_time_h, st.verification_notes) == (70, 6, "my test")


def test_old_fiberon_ppa_template_is_renamed(app):
    from app.seed import seed_presets

    siraya = DryingPreset.query.filter_by(manufacturer="Siraya Tech").one()
    siraya.manufacturer, siraya.product_name = "Fiberon", "PPA-CF"  # simulate an older install
    siraya.notes = "No Polymaker Fiberon product named PPA-CF could be found"
    db.session.commit()
    seed_presets()
    assert DryingPreset.query.filter_by(manufacturer="Fiberon", product_name="PPA-CF").count() == 0
    assert DryingPreset.query.filter_by(manufacturer="Siraya Tech", product_name="Fibreheart PPA-CF").count() == 1


def test_sourced_values_do_not_overwrite_user_edits(app):
    from app.seed import seed_presets

    preset = DryingPreset.query.filter_by(manufacturer="Fiberon", product_name="PA12-CF10").one()
    preset.ref_air_temp_c = 90
    preset.source_reference = "my own test"
    db.session.commit()
    seed_presets()
    assert preset.ref_air_temp_c == 90 and preset.source_reference == "my own test"


def test_bed_target_limited_by_material_max(auth_client):
    preset = DryingPreset.query.filter_by(product_name="PLA").one()
    preset.max_temp_c = 55
    db.session.commit()
    resp = auth_client.post(f"/presets/{preset.id}/printer/P1S",
                            data={"P1S-bed_target_c": "70", "P1S-verification_status": "unverified"},
                            follow_redirects=True)
    assert b"exceeds the lowest applicable limit (55" in resp.data
    assert preset.setting_for("P1S").bed_target_c == 60  # unchanged (template value)


def test_verified_requires_notes_and_temp_change_resets(auth_client):
    preset = DryingPreset.query.filter_by(product_name="PETG").one()
    url = f"/presets/{preset.id}/printer/X1"
    auth_client.post(url, data={"X1-bed_target_c": "60", "X1-verification_status": "unverified"})
    auth_client.post(url, data={"X1-bed_target_c": "60", "X1-verification_status": "verified"})
    assert preset.setting_for("X1").verification_status == "unverified"  # rejected: no notes
    # New temperature + "verified" in one save is not allowed; values must already be the tested ones.
    auth_client.post(url, data={"X1-bed_target_c": "60", "X1-verification_status": "verified",
                                "X1-verification_notes": "probe at spool: ok"})
    db.session.refresh(preset.setting_for("X1"))
    assert preset.setting_for("X1").verification_status == "verified"
    auth_client.post(url, data={"X1-bed_target_c": "65", "X1-verification_status": "verified",
                                "X1-verification_notes": "probe at spool: ok"})
    db.session.refresh(preset.setting_for("X1"))
    assert preset.setting_for("X1").verification_status == "unverified"


def test_reference_status_requires_source(auth_client):
    preset = DryingPreset.query.filter_by(product_name="ABS").one()
    resp = auth_client.post(f"/presets/{preset.id}/edit", data={
        "manufacturer": "Generic", "material": "ABS", "product_name": "ABS", "verification_status": "reference",
        "source_reference": ""})
    assert b"source reference is required" in resp.data


def test_notify_records_alert_when_ntfy_fails(app):
    Setting.set("notify.ntfy.enabled", True)
    Setting.set("notify.ntfy.url", "http://127.0.0.1:9")
    Setting.set("notify.ntfy.topic", "t")
    db.session.commit()
    with mock.patch("app.notifications.requests.post", side_effect=requests.ConnectionError("refused")):
        alert = notify("temp_limit_exceeded", printer="P1S", material="PLA", process="drying #1", details="bed 120")
    assert Alert.query.count() == 1
    assert "FAILED" in alert.delivery_json


def test_critical_events_cannot_be_muted(app):
    Setting.set("notify.event.heater_off_unconfirmed", False)
    Setting.set("notify.event.process_started", False)
    Setting.set("notify.ntfy.enabled", True)
    Setting.set("notify.ntfy.url", "http://ntfy.local")
    Setting.set("notify.ntfy.topic", "t")
    db.session.commit()
    with mock.patch("app.notifications.requests.post") as post:
        post.return_value.status_code = 200
        notify("heater_off_unconfirmed", printer="X1")
        notify("process_started", printer="X1")
    assert post.call_count == 1
    assert post.call_args.kwargs["headers"]["Priority"] == "5"


def test_controller_recovery_never_resumes(app):
    from app.models import CommandRequest

    printer = Printer(name="X1", model="X1", ip_address="192.168.1.9", serial="00M00A000000009", enabled=True)
    printer.access_code = "abcd1234"
    db.session.add(printer)
    db.session.flush()
    proc = Process(kind="drying", printer_id=printer.id, printer_name="X1", material_label="PETG",
                   state=ProcessState.DRYING, started_at=utcnow())
    pending = Process(kind="heater_test", printer_id=printer.id, state=ProcessState.PENDING)
    db.session.add_all([proc, pending])
    db.session.flush()
    db.session.add(CommandRequest(printer_id=printer.id, process_id=pending.id, command="start_heater_test"))
    db.session.commit()
    assert recover_interrupted_processes() == [printer.id]  # must be switched off when reachable
    assert pending.state == ProcessState.STOPPED  # queued start is never executed after a restart
    assert CommandRequest.query.one().status == "failed"
    db.session.refresh(proc)
    assert proc.state == ProcessState.FAILED and "Not resumed" in proc.error
    assert {a.event_type for a in Alert.query} == {"process_ended_unexpectedly", "heater_off_unconfirmed"}


def test_csv_export_escapes_formulas(auth_client):
    db.session.add(Process(kind="drying", printer_name="=HYPERLINK(1)", state=ProcessState.STOPPED))
    db.session.commit()
    body = auth_client.get("/history/export.csv").data.decode()
    assert "'=HYPERLINK(1)" in body


def test_hardware_cutoff_requires_description(auth_client):
    auth_client.post("/settings/safety", data={"safety-max_heating_minutes": "600", "safety-disconnect_timeout_s": "60",
                                                "safety-hardware_cutoff_confirmed": "y"})
    assert Setting.get_bool("safety.hardware_cutoff_confirmed") is False


def test_security_headers(auth_client):
    resp = auth_client.get("/")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]


def test_live_pages_with_telemetry(auth_client):
    from app.models import PrinterStatus, TelemetrySample

    auth_client.post("/printers/new", data=PRINTER)
    p = Printer.query.one()
    p.enabled = True
    db.session.add(PrinterStatus(printer_id=p.id, online=True, gcode_state="IDLE", bed_temp_c=24.5,
                                 chamber_temp_c=None, last_seen_at=utcnow(), raw_json='{"bed_temper": 24.5}'))
    db.session.add(TelemetrySample(printer_id=p.id, bed_temp_c=24.5, bed_target_c=0, nozzle_temp_c=26))
    db.session.commit()
    page = auth_client.get(f"/printers/{p.id}").data.decode()
    assert "Last raw status report" in page and "chart.umd.min.js" in page
    assert page.count("js/app.js") == 1
    status = auth_client.get("/api/status").get_json()["printers"][0]
    assert status["online"] and status["bed_temp_c"] == 24.5 and status["chamber_temp_c"] is None
    series = auth_client.get(f"/api/printers/{p.id}/telemetry").get_json()["series"]
    assert "Chamber (printer sensor)" not in series and series["Bed (heater reading)"] == [24.5]
    assert auth_client.get("/").status_code == 200 and auth_client.get("/printers/").status_code == 200


def _online_printer(auth_client, **status):
    from app.models import PrinterStatus

    auth_client.post("/printers/new", data=PRINTER)
    p = Printer.query.one()
    p.enabled = True
    p.firmware_version_reported = "01.08.00.00"
    db.session.add(PrinterStatus(printer_id=p.id, online=True, gcode_state="IDLE", bed_temp_c=24, bed_target_c=0,
                                 last_seen_at=utcnow(), **status))
    Setting.set("controller.heartbeat_at", utcnow().isoformat())
    db.session.commit()
    return p


HT_FORM = {"target_c": "40", "minutes": "5", "print_hours_before": "1000", "present": "y", "bed_clear": "y",
           "stop_known": "y"}


def test_heater_test_blocked_without_global_switch(auth_client):
    from app.models import CommandRequest

    p = _online_printer(auth_client)
    page = auth_client.get(f"/printers/{p.id}/heater-test/").data.decode()
    assert "⛔ Heater control switched on for the server" in page
    auth_client.post(f"/printers/{p.id}/heater-test/", data=HT_FORM, follow_redirects=True)
    assert CommandRequest.query.count() == 0 and Process.query.count() == 0


def test_heater_test_requires_confirmations_and_limits(auth_client, app):
    from app.models import CommandRequest

    app.config["ALLOW_HEATER_CONTROL"] = True
    p = _online_printer(auth_client)
    auth_client.post(f"/printers/{p.id}/heater-test/", data={**HT_FORM, "present": ""})
    auth_client.post(f"/printers/{p.id}/heater-test/", data={**HT_FORM, "target_c": "55"})
    assert CommandRequest.query.count() == 0
    auth_client.post(f"/printers/{p.id}/heater-test/", data=HT_FORM)
    proc = Process.query.one()
    assert proc.state == ProcessState.PENDING and proc.print_hours_before == 1000
    assert CommandRequest.query.one().command == "start_heater_test"
    # A second request while one is open is refused.
    auth_client.post(f"/printers/{p.id}/heater-test/", data=HT_FORM)
    assert CommandRequest.query.count() == 1
    page = auth_client.get(f"/printers/{p.id}/heater-test/").data.decode()
    assert "STOP: switch heater off now" in page
    assert auth_client.get(f"/api/processes/{proc.id}").get_json()["state"] == "pending"


def test_print_hours_and_verification(auth_client, app):
    p = _online_printer(auth_client)
    from datetime import timedelta
    t0 = utcnow() - timedelta(minutes=75)
    proc = Process(kind="heater_test", printer_id=p.id, state=ProcessState.COMPLETED, target_bed_c=40,
                   planned_duration_min=70, started_at=t0, ended_at=t0 + timedelta(minutes=70),
                   heater_off_verified=True, firmware_at_start="01.08.00.00", print_hours_before=1000)
    db.session.add(proc)
    db.session.commit()
    auth_client.post(f"/printers/{p.id}/heater-test/{proc.id}/hours", data={"print_hours_after": "1000"})
    assert "UNCHANGED" in Printer.query.one().print_hours_test_notes
    auth_client.post(f"/printers/{p.id}/heater-test/verify")
    p = Printer.query.one()
    assert p.heater_control_verified and p.heater_control_verified_firmware == "01.08.00.00"
    assert p.heater_control_allowed(True) == (True, "OK")


def test_cannot_verify_without_confirmed_shutdown(auth_client):
    p = _online_printer(auth_client)
    db.session.add(Process(kind="heater_test", printer_id=p.id, state=ProcessState.FAILED, target_bed_c=40,
                           heater_off_verified=False, firmware_at_start="01.08.00.00"))
    db.session.commit()
    auth_client.post(f"/printers/{p.id}/heater-test/verify")
    assert not Printer.query.one().heater_control_verified


def test_heater_test_blocked_when_bed_warm(auth_client, app):
    from app.models import CommandRequest, PrinterStatus

    app.config["ALLOW_HEATER_CONTROL"] = True
    p = _online_printer(auth_client)
    db.session.get(PrinterStatus, p.id).bed_temp_c = 48.0  # above the 45 °C start limit
    db.session.commit()
    page = auth_client.get(f"/printers/{p.id}/heater-test/").data.decode()
    assert "⛔ Bed cool enough to observe heating" in page and "bed now: 48.0 °C" in page
    auth_client.post(f"/printers/{p.id}/heater-test/", data=HT_FORM)
    assert CommandRequest.query.count() == 0


def test_unattended_policy(auth_client):
    from app.safety import unattended_heating_allowed

    assert unattended_heating_allowed()[0] is False
    auth_client.post("/settings/safety", data={"safety-max_heating_minutes": "720", "safety-disconnect_timeout_s": "60",
                                                "safety-no_cutoff_risk_accepted": "y"})
    ok, reason = unattended_heating_allowed()
    assert ok and "accepted" in reason
    assert EventLog.query.filter(EventLog.message.contains("without cutoff accepted: True")).count() == 1


# ---------------------------------------------------------------- Phase 5 web
def _ready_p1s(auth_client, app, bed_target=70):
    app.config["ALLOW_HEATER_CONTROL"] = True
    app.config["TIMEZONE"] = "America/Chicago"
    p = _online_printer(auth_client)
    p.heater_control_verified = True
    p.heater_control_verified_firmware = "01.08.00.00"
    p.motion_prepared_at = utcnow()
    preset = DryingPreset.query.filter_by(product_name="PETG").one()
    preset.setting_for("P1S").bed_target_c = bed_target
    db.session.commit()
    return p, preset


def _drying_form(p, preset, **kw):
    data = {"printer_id": p.id, "preset_id": preset.id, "target_bed_c": "70", "stop_mode": "duration",
            "duration_h": "4", "start_mode": "now", "flip_reminder": "y", "prep_spool": "y", "prep_cover": "y",
            "prep_spool_heat": "y", "prep_probe": "y", "accept_unverified": "y", "spool_size_g": "1000"}
    data.update(kw)
    return {k: v for k, v in data.items() if v is not None}


def test_drying_page_review_and_start(auth_client, app):
    from app.models import CommandRequest

    p, preset = _ready_p1s(auth_client, app)
    page = auth_client.get(f"/drying/?printer_id={p.id}&preset_id={preset.id}&spool_g=1000").data.decode()
    assert "Start / schedule drying" in page and 'value="70"' in page
    assert 'name="duration_h" type="text" value="12.0"' in page  # P1S drying time, not the 8 h oven time
    auth_client.post("/drying/start", data=_drying_form(p, preset))
    proc = Process.query.one()
    assert (proc.kind, proc.state, proc.target_bed_c, proc.planned_duration_min) == ("drying", "pending", 70, 240)
    assert proc.flip_reminder and proc.spool_size_g == 1000 and proc.prep_confirmed_at is not None
    assert CommandRequest.query.one().command == "start_drying"
    assert "Stop: switch heater off" in auth_client.get("/drying/").data.decode()
    assert "Stop: switch heater off" in auth_client.get("/").data.decode()
    auth_client.post(f"/drying/{proc.id}/stop")
    assert CommandRequest.query.filter_by(command="stop").count() == 1


@pytest.mark.parametrize("override, message", [
    ({"accept_unverified": None}, "unverified printer setting"),
    ({"target_bed_c": "105"}, "Bed target must be"),
    ({"prep_cover": None}, "Required"),
    ({"stop_mode": "end_time", "duration_h": None}, "stop time must be"),
])
def test_drying_start_rejections(auth_client, app, override, message):
    p, preset = _ready_p1s(auth_client, app)
    resp = auth_client.post("/drying/start", data=_drying_form(p, preset, **override), follow_redirects=True)
    assert message in resp.data.decode() and Process.query.count() == 0


def test_drying_blocked_when_not_verified(auth_client, app):
    p, preset = _ready_p1s(auth_client, app)
    p.heater_control_verified = False
    db.session.commit()
    resp = auth_client.post("/drying/start", data=_drying_form(p, preset), follow_redirects=True)
    assert "required checks" in resp.data.decode() and Process.query.count() == 0


def test_schedule_create_overlap_and_prepare(auth_client, app):
    from datetime import datetime, timedelta

    from app.models import Schedule

    p, preset = _ready_p1s(auth_client, app)
    Setting.set("safety.no_cutoff_risk_accepted", True)
    db.session.commit()
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0)
    form = _drying_form(p, preset, start_mode="scheduled", start_at=tomorrow.strftime("%Y-%m-%dT%H:%M"),
                        repeat_rule="daily")
    auth_client.post("/drying/start", data=form)
    sch = Schedule.query.one()
    assert sch.prepared and sch.repeat_rule == "daily" and sch.duration_min == 240 and sch.target_bed_c == 70
    # Overlapping schedule on the same printer is refused.
    later = tomorrow + timedelta(hours=1)
    resp = auth_client.post("/drying/start", data={**form, "start_at": later.strftime("%Y-%m-%dT%H:%M"),
                                                   "repeat_rule": ""}, follow_redirects=True)
    assert "Overlaps schedule" in resp.data.decode() and Schedule.query.count() == 1
    page = auth_client.get("/schedules/").data.decode()
    assert "confirmed" in page and "daily" in page
    # Re-confirming preparation for a future occurrence.
    sch.prep_confirmed_for = None
    db.session.commit()
    auth_client.post(f"/schedules/{sch.id}/prepare", data={"prep_spool": "y", "prep_cover": "y", "prep_spool_heat": "y"})
    assert Schedule.query.one().prepared


def test_scheduled_start_requires_unattended_permission(auth_client, app):
    from datetime import datetime, timedelta

    from app.models import Schedule

    p, preset = _ready_p1s(auth_client, app)
    start = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    resp = auth_client.post("/drying/start", data=_drying_form(p, preset, start_mode="scheduled", start_at=start),
                            follow_redirects=True)
    assert "required checks" in resp.data.decode() and Schedule.query.count() == 0


def test_process_api_drying_timing(auth_client, app):
    from datetime import timedelta

    p, preset = _ready_p1s(auth_client, app)
    now = utcnow()
    proc = Process(kind="drying", printer_id=p.id, state=ProcessState.DRYING, target_bed_c=70, planned_duration_min=60,
                   started_at=now - timedelta(minutes=40), target_reached_at=now - timedelta(minutes=30))
    db.session.add(proc)
    db.session.commit()
    data = auth_client.get(f"/api/processes/{proc.id}").get_json()
    assert 1700 <= data["remaining_s"] <= 1800 and 49 <= data["progress"] <= 51
    assert auth_client.get(f"/history/{proc.id}").status_code == 200


def test_acknowledge_single_alert(auth_client):
    a1 = notify("process_started", printer="P1S")
    a2 = notify("target_reached", printer="P1S")
    assert f"/notifications/alerts/{a1.id}/ack" in auth_client.get("/").data.decode()
    assert f"/notifications/alerts/{a2.id}/ack" in auth_client.get("/notifications/").data.decode()
    resp = auth_client.post(f"/notifications/alerts/{a1.id}/ack", headers={"Referer": "http://localhost/"})
    assert resp.headers["Location"] in ("/", "http://localhost/")
    assert db.session.get(Alert, a1.id).acknowledged and not db.session.get(Alert, a2.id).acknowledged
    # Off-site referrer is not followed.
    resp = auth_client.post(f"/notifications/alerts/{a2.id}/ack", headers={"Referer": "http://evil.example/"})
    assert resp.headers["Location"].endswith("/notifications/")


def test_obsolete_seed_note_is_replaced(app):
    from app.seed import OBSOLETE_NOTES, seed_presets

    st = DryingPreset.query.filter_by(product_name="Generic PPA-CF").one().setting_for("X1")
    st.bed_target_c, st.drying_time_h, st.verification_notes = None, None, next(iter(OBSOLETE_NOTES))  # older install
    db.session.commit()
    seed_presets()
    assert (st.bed_target_c, st.drying_time_h) == (100, 18) and "100 °C for 18 h" in st.verification_notes


# ---------------------------------------------------------------- Phase 7 annealing web
def test_annealing_review_and_start(auth_client, app):
    from app.models import AnnealingPreset, CommandRequest

    p, _ = _ready_p1s(auth_client, app)
    pc = AnnealingPreset.query.filter_by(manufacturer="Generic", material="PC").one()
    assert (pc.anneal_temp_c, pc.max_temp_c, pc.hold_minutes) == (85, 100, 720) and "85 to 100" in pc.source_reference
    page = auth_client.get(f"/annealing/?printer_id={p.id}&preset_id={pc.id}").data.decode()
    assert "Start annealing" in page and "Estimated program" in page and "planning and logging" not in page
    form = {"printer_id": p.id, "preset_id": pc.id, "anneal_temp_c": "85", "hold_h": "4", "heat_ramp_c_per_h": "20",
            "cool_ramp_c_per_h": "10", "cool_end_c": "40", "prep_part": "y", "prep_closed": "y", "prep_warp": "y"}
    auth_client.post("/annealing/start", data=form)
    proc = Process.query.one()
    assert (proc.kind, proc.target_bed_c, proc.planned_duration_min, proc.heat_ramp_c_per_h, proc.cool_ramp_c_per_h,
            proc.cool_end_c) == ("annealing", 85, 240, 20, 10, 40)
    assert CommandRequest.query.one().command == "start_annealing"
    assert "Stop: switch heater off" in auth_client.get("/annealing/").data.decode()


@pytest.mark.parametrize("override, message", [
    ({"anneal_temp_c": "105"}, "Annealing temperature must be"),  # above the P1S bed maximum
    ({"cool_end_c": "90"}, "cool-down end temperature must be below"),
    ({"hold_h": "6"}, "safety limit"),  # ~3 h ramp + 6 h hold + 4.5 h cool-down > 12 h
    ({"prep_warp": None}, "Required"),
])
def test_annealing_start_rejections(auth_client, app, override, message):
    from app.models import AnnealingPreset

    p, _ = _ready_p1s(auth_client, app)
    pc = AnnealingPreset.query.filter_by(manufacturer="Generic", material="PC").one()
    form = {"printer_id": p.id, "preset_id": pc.id, "anneal_temp_c": "85", "hold_h": "4", "heat_ramp_c_per_h": "20",
            "cool_ramp_c_per_h": "10", "cool_end_c": "40", "prep_part": "y", "prep_closed": "y", "prep_warp": "y"}
    form.update(override)
    resp = auth_client.post("/annealing/start", data={k: v for k, v in form.items() if v is not None},
                            follow_redirects=True)
    assert message in resp.data.decode() and Process.query.count() == 0


def test_annealing_api_phases(auth_client, app):
    from datetime import timedelta

    p, _ = _ready_p1s(auth_client, app)
    now = utcnow()
    proc = Process(kind="annealing", printer_id=p.id, state=ProcessState.DRYING, target_bed_c=85, planned_duration_min=60,
                   cool_ramp_c_per_h=10, cool_end_c=45, current_setpoint_c=85, started_at=now - timedelta(hours=4),
                   target_reached_at=now - timedelta(minutes=30))
    db.session.add(proc)
    db.session.commit()
    data = auth_client.get(f"/api/processes/{proc.id}").get_json()
    assert "Holding 85" in data["note"] and 4.4 * 3600 <= data["remaining_s"] <= 4.6 * 3600  # 30 min hold + 4 h cool



# ---------------------------------------------------------------- prepare printer (home, lower bed, park)
def test_prepare_requires_empty_bed_and_confirmation(auth_client, app):
    from app.models import CommandRequest

    p, preset = _ready_p1s(auth_client, app)
    p.motion_prepared_at = None
    db.session.commit()
    page = auth_client.get(f"/drying/?printer_id={p.id}&preset_id={preset.id}").data.decode()
    assert "⛔ Printer prepared" in page and "Prepare printer" in page
    auth_client.post(f"/printers/{p.id}/prepare", data={})  # bed not confirmed empty
    assert CommandRequest.query.filter_by(command="prepare_motion").count() == 0
    auth_client.post(f"/printers/{p.id}/prepare/confirm", data={"bed_lowered": "y"})  # nothing sent yet
    assert Printer.query.one().motion_prepared_at is None
    auth_client.post(f"/printers/{p.id}/prepare", data={"bed_empty": "y"})
    cmd = CommandRequest.query.filter_by(command="prepare_motion").one()
    cmd.status = "sent"
    db.session.commit()
    assert "I can see the bed at the bottom" in auth_client.get(f"/drying/?printer_id={p.id}").data.decode()
    auth_client.post(f"/printers/{p.id}/prepare/confirm", data={"bed_lowered": "y"})
    assert Printer.query.one().motion_prepared_at is not None
    assert "✅ Printer prepared" in auth_client.get(f"/drying/?printer_id={p.id}&preset_id={preset.id}").data.decode()


def test_drying_blocked_until_prepared(auth_client, app):
    p, preset = _ready_p1s(auth_client, app)
    p.motion_prepared_at = None
    db.session.commit()
    resp = auth_client.post("/drying/start", data=_drying_form(p, preset), follow_redirects=True)
    assert "required checks" in resp.data.decode() and Process.query.count() == 0



def test_polylite_pla_pro_annealing_preset(app):
    from app.models import AnnealingPreset

    pre = AnnealingPreset.query.filter_by(manufacturer="Polymaker", material="PolyLite PLA Pro").one()
    assert (pre.anneal_temp_c, pre.max_temp_c, pre.hold_minutes) == (50, 60, 720)
    assert pre.verification_status == VerificationStatus.UNVERIFIED  # borrowed, not Polymaker's own figure
    assert "NO annealing guidance" in pre.source_reference and "Stress relief only" in pre.notes


def test_loop_watchdog_exits_when_stalled(monkeypatch):
    import time as _time

    from controller import daemon

    exited = []
    monkeypatch.setattr(daemon, "LOOP_STALL_EXIT_S", 0.1)
    monkeypatch.setattr(daemon.os, "_exit", lambda code: exited.append(code))
    monkeypatch.setattr(daemon, "LOOP_WATCH_INTERVAL_S", 0.05)
    last = {"heartbeat": _time.monotonic() - 5}
    daemon.start_loop_watchdog(last)
    _time.sleep(0.3)
    last["heartbeat"] = 0.0  # disarm: the thread outlives the test and would call the real os._exit
    _time.sleep(0.1)
    assert exited and exited[0] == 70

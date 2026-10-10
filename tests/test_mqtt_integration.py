"""End-to-end read-only telemetry against a local TLS broker + fake printer.

Skipped when mosquitto/openssl are not installed. Uses only 127.0.0.1.
"""
import json
import shutil
import socket
import subprocess
import time

import pytest

from app.extensions import db
from app.models import EventLog, Printer, PrinterStatus
from controller import telemetry as telemetry_mod
from controller.bambu_protocol import READONLY_REQUESTS
from controller.telemetry import TelemetryManager

from .fake_printer import FakePrinter, wait_for

SERIAL = "01P00A999999999"
CODE = "abcd1234"

pytestmark = pytest.mark.skipif(
    not (shutil.which("mosquitto") or shutil.which("/usr/sbin/mosquitto")) or not shutil.which("openssl"),
    reason="mosquitto/openssl not installed",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Broker:
    def __init__(self, tmp_path, port):
        self.port = port
        d = tmp_path
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", f"/CN={SERIAL}", "-keyout", d / "key.pem", "-out", d / "cert.pem"],
                       check=True, capture_output=True)
        (d / "passwd").write_text("")
        subprocess.run(["mosquitto_passwd", "-b", d / "passwd", "bblp", CODE], check=True, capture_output=True)
        (d / "mosquitto.conf").write_text(
            f"listener {port} 127.0.0.1\nallow_anonymous false\npassword_file {d / 'passwd'}\n"
            f"certfile {d / 'cert.pem'}\nkeyfile {d / 'key.pem'}\npersistence false\n"
            f"log_dest file {d / 'mosquitto.log'}\n")
        self.conf = d / "mosquitto.conf"
        self.proc = None

    def start(self):
        exe = shutil.which("mosquitto") or "/usr/sbin/mosquitto"
        self.proc = subprocess.Popen([exe, "-c", str(self.conf)])
        assert wait_for(lambda: socket.socket().connect_ex(("127.0.0.1", self.port)) == 0, 5)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)


@pytest.fixture()
def env(app, tmp_path, monkeypatch):
    port = _free_port()
    broker = Broker(tmp_path, port)
    broker.start()
    # Point the P1S profile at the test broker's port.
    real_profile = telemetry_mod.profile_for

    class _P:
        def __init__(self, p):
            self._p = p
            self.mqtt_port = port
            self.pushall_min_interval_s = p.pushall_min_interval_s

    monkeypatch.setattr(telemetry_mod, "profile_for", lambda m: _P(real_profile(m)))
    printer = Printer(name="Fake P1S", model="P1S", ip_address="127.0.0.1", serial=SERIAL, enabled=True)
    printer.access_code = CODE
    db.session.add(printer)
    db.session.commit()
    fake = FakePrinter(port, SERIAL, CODE)
    # The real printer is always connected first; without this the app's first pushall can go unanswered.
    assert fake.connected.wait(10)
    tm = TelemetryManager()
    yield broker, fake, tm, printer
    tm.stop_all()
    fake.stop()
    broker.stop()


def pump(tm, until, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        tm.drain(0.3)
        tm.flush()
        tm.periodic()
        db.session.expire_all()
        if until():
            return True
    return False


def _status():
    return db.session.get(PrinterStatus, Printer.query.one().id)


def test_connect_parse_pin_and_readonly(env):
    _broker, fake, tm, printer = env
    tm.sync_printers()
    assert pump(tm, lambda: _status() is not None and _status().online and _status().nozzle_temp_c is not None
                and Printer.query.one().firmware_version_reported is not None)
    st, p = _status(), Printer.query.one()
    assert p.firmware_version_reported == "01.08.02.00"
    assert p.tls_fingerprint and len(p.tls_fingerprint) == 95  # SHA-256 pinned on first use
    assert st.gcode_state == "IDLE" and st.nozzle_temp_c == 26.0
    assert st.chamber_temp_c is None  # P1S has no chamber sensor; bogus value must not surface
    first_bed = st.bed_temp_c
    assert pump(tm, lambda: _status().bed_temp_c > first_bed)  # delta reports merged
    assert _status().nozzle_temp_c == 26.0  # fields absent from deltas are retained
    assert json.loads(_status().raw_json)["gcode_state"] == "IDLE"
    # Only allow-listed read-only requests ever reached the printer.
    kinds = {next(iter(r.values()))["command"] for r in fake.received}
    assert kinds <= set(READONLY_REQUESTS) and kinds == {"pushall", "get_version"}


def test_reconnect_after_broker_restart(env):
    broker, _fake, tm, _printer = env
    tm.sync_printers()
    assert pump(tm, lambda: _status() is not None and _status().online)
    broker.stop()
    assert pump(tm, lambda: not _status().online, timeout=10)
    assert EventLog.query.filter(EventLog.message.contains("disconnected")).count() >= 1
    broker.start()
    assert pump(tm, lambda: _status().online and _status().last_error is None, timeout=40)


def test_certificate_mismatch_blocks_connection(env):
    _broker, _fake, tm, printer = env
    printer.tls_fingerprint = ":".join(["AA"] * 32)
    db.session.commit()
    tm.sync_printers()
    assert pump(tm, lambda: _status() is not None and _status().last_error
                and "does not match" in _status().last_error, timeout=10)
    assert not _status().online
    assert tm.links[printer.id].blocked


def test_wrong_access_code_reported(env):
    _broker, _fake, tm, printer = env
    printer.access_code = "wrong123"
    db.session.commit()
    tm.sync_printers()
    assert pump(tm, lambda: _status() is not None and _status().last_error
                and "refused" in _status().last_error, timeout=10)


def test_disable_stops_link(env):
    _broker, _fake, tm, printer = env
    tm.sync_printers()
    assert pump(tm, lambda: _status() is not None and _status().online)
    printer.enabled = False
    db.session.commit()
    tm.sync_printers()
    assert printer.id not in tm.links and not _status().online


def test_state_transitions_logged(env):
    _broker, fake, tm, _printer = env
    tm.sync_printers()
    assert pump(tm, lambda: _status() is not None and _status().gcode_state == "IDLE")
    fake.target = 80.0
    fake._report({"print": {"gcode_state": "RUNNING", "bed_target_temper": 80.0, "print_type": "local",
                            "subtask_name": "Filament drying", "mc_remaining_time": 480}})
    assert pump(tm, lambda: EventLog.query.filter(EventLog.message.contains("IDLE → RUNNING")).count() == 1)
    msg = EventLog.query.filter(EventLog.message.contains("IDLE → RUNNING")).one().message
    assert "bed target 0.0 → 80.0" in msg and "subtask_name=Filament drying" in msg and "print_type=local" in msg


# ---------------------------------------------------------------- Phase 4 runner
from app.models import Alert, CommandRequest, Process, ProcessState  # noqa: E402
from controller.process_runner import ProcessRunner  # noqa: E402


@pytest.fixture()
def runner(env, app):
    _broker, _fake, tm, _printer = env
    app.config["ALLOW_HEATER_CONTROL"] = True
    r = ProcessRunner(tm)
    r.MINUTE_S, r.ACCEPT_TIMEOUT_S, r.OFF_RETRY_S, r.OFF_MAX_ATTEMPTS = 1, 4, 1, 3
    tm.sync_printers()
    assert pump(tm, lambda: _status() is not None and _status().online and _status().bed_target_c is not None)
    return r


def run(tm, r, until, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        tm.drain(0.3)
        tm.flush()
        r.tick()
        db.session.expire_all()
        if until():
            return True
    return False


def start(printer, target=40, minutes=8):
    proc = Process(kind="heater_test", printer_id=printer.id, printer_name=printer.name, material_label="(test)",
                   state=ProcessState.PENDING, target_bed_c=target, planned_duration_min=minutes)
    db.session.add(proc)
    db.session.flush()
    db.session.add(CommandRequest(printer_id=printer.id, process_id=proc.id, command="start_heater_test",
                                  requested_by="test"))
    db.session.commit()
    return proc.id


def proc_(pid):
    return db.session.get(Process, pid)


def alerts():
    return [a.event_type for a in Alert.query.order_by(Alert.id)]


def gcode_sent(fake):
    return [r["print"]["param"].strip() for r in fake.received if r.get("print", {}).get("command") == "gcode_line"]


def test_heater_test_happy_path(env, runner):
    _b, fake, tm, printer = env
    pid = start(printer, 40, 8)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.DRYING)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.COMPLETED)
    p = proc_(pid)
    assert p.heater_off_verified and fake.target == 0 and p.max_bed_observed_c >= 38
    assert gcode_sent(fake)[0] == "M140 S40" and gcode_sent(fake)[-1] == "M140 S0"
    assert alerts() == ["process_started", "target_reached", "process_completed"]
    assert p.firmware_at_start == "01.08.02.00"


def test_firmware_rejects_command(env, runner):
    _b, fake, tm, printer = env
    fake.mode = "reject"
    pid = start(printer)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "did not accept" in proc_(pid).error and "Developer Mode" in proc_(pid).error
    assert proc_(pid).heater_off_verified  # target never left 0
    assert "process_ended_unexpectedly" in alerts() and "process_completed" not in alerts()


def test_overtemperature_safety_shutdown(env, runner):
    _b, fake, tm, printer = env
    fake.mode = "runaway"
    pid = start(printer, 40, 30)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.SAFETY_SHUTDOWN)
    assert proc_(pid).heater_off_verified and fake.target == 0
    assert "temp_limit_exceeded" in alerts()


def test_heater_off_not_confirmed(env, runner):
    _b, fake, tm, printer = env
    pid = start(printer, 40, 3)
    assert run(tm, runner, lambda: proc_(pid).state in ProcessState.ACTIVE and fake.target == 40)
    fake.mode = "ignore_off"
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert not proc_(pid).heater_off_verified
    assert "heater_off_unconfirmed" in alerts() and "process_completed" not in alerts()
    assert printer.id in runner.pending_off
    fake.mode = "accept"  # printer starts obeying: pending_off clears it
    assert run(tm, runner, lambda: fake.target == 0 and printer.id not in runner.pending_off)


def test_manual_stop(env, runner):
    _b, fake, tm, printer = env
    pid = start(printer, 40, 30)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.DRYING)
    db.session.add(CommandRequest(printer_id=printer.id, process_id=pid, command="stop", requested_by="test"))
    db.session.commit()
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.STOPPED)
    assert proc_(pid).heater_off_verified and fake.target == 0 and alerts()[-1] == "process_stopped"


def test_refuses_when_printer_busy(env, runner):
    _b, fake, tm, printer = env
    fake._report({"print": {"gcode_state": "RUNNING"}})
    assert pump(tm, lambda: _status().gcode_state == "RUNNING")
    pid = start(printer)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "busy" in proc_(pid).error and gcode_sent(fake) == []


def test_refuses_when_global_switch_off(env, runner, app):
    _b, fake, tm, printer = env
    app.config["ALLOW_HEATER_CONTROL"] = False
    pid = start(printer)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "disabled on the server" in proc_(pid).error and gcode_sent(fake) == []


def test_refuses_target_above_test_limit(env, runner):
    _b, fake, tm, printer = env
    pid = start(printer, target=55)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "exceeds the limit" in proc_(pid).error and gcode_sent(fake) == []


def test_expired_start_request_never_runs(env, runner):
    from datetime import timedelta

    from app.models import utcnow
    _b, fake, tm, printer = env
    pid = start(printer)
    CommandRequest.query.one().created_at = utcnow() - timedelta(minutes=5)
    db.session.commit()
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "expired" in proc_(pid).error and gcode_sent(fake) == []


def test_print_started_during_test_is_left_alone(env, runner):
    _b, fake, tm, printer = env
    pid = start(printer, 40, 30)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.DRYING)
    sent_before = len(gcode_sent(fake))
    fake._report({"print": {"gcode_state": "PREPARE"}})
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert len(gcode_sent(fake)) == sent_before  # never sends heater-off into a print job


def test_connection_loss_during_heating(env, runner):
    broker, fake, tm, printer = env
    pid = start(printer, 40, 60)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.DRYING)
    broker.stop()
    assert run(tm, runner, lambda: "heater_off_unconfirmed" in alerts(), timeout=90)
    assert printer.id in runner.pending_off
    broker.start()
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED and proc_(pid).heater_off_verified,
               timeout=90)
    assert fake.target == 0


def test_refuses_when_bed_already_warm(env, runner):
    _b, fake, tm, printer = env
    fake.bed = 48.0
    assert pump(tm, lambda: _status().bed_temp_c is not None and _status().bed_temp_c >= 40)
    pid = start(printer, 40)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "too close" in proc_(pid).error and gcode_sent(fake) == []


# ---------------------------------------------------------------- Phase 5 drying runs
from app.models import DryingPreset  # noqa: E402


def _verify(printer, preset_name="PETG", bed=40):
    from app.models import utcnow
    printer.motion_prepared_at = utcnow()
    printer.heater_control_verified = True
    printer.heater_control_verified_firmware = "01.08.02.00"
    preset = DryingPreset.query.filter_by(product_name=preset_name).one()
    preset.setting_for("P1S").bed_target_c = bed
    db.session.commit()
    return preset


def start_drying(printer, preset, target=40, minutes=6, prep=True, end_at=None, flip=True):
    from app.models import utcnow
    proc = Process(kind="drying", printer_id=printer.id, drying_preset_id=preset.id, printer_name=printer.name,
                   material_label=preset.label, state=ProcessState.PENDING, target_bed_c=target,
                   planned_duration_min=minutes, scheduled_end_at=end_at, flip_reminder=flip,
                   prep_confirmed_at=utcnow() if prep else None)
    db.session.add(proc)
    db.session.flush()
    db.session.add(CommandRequest(printer_id=printer.id, process_id=proc.id, command="start_drying"))
    db.session.commit()
    return proc.id


def test_drying_run_with_flip_reminder(env, runner):
    _b, fake, tm, printer = env
    fake.firmware = "01.08.02.00"
    preset = _verify(printer)
    assert pump(tm, lambda: Printer.query.one().firmware_version_reported == "01.08.02.00")
    pid = start_drying(printer, preset, 40, 6)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.COMPLETED, timeout=40)
    p = proc_(pid)
    assert p.heater_off_verified and fake.target == 0 and p.flip_notified_at is not None
    # Timer runs from reaching the target; flip reminder lands between target and completion.
    assert (p.ended_at - p.target_reached_at).total_seconds() >= 6
    assert p.target_reached_at < p.flip_notified_at < p.ended_at
    assert alerts() == ["process_started", "target_reached", "flip_spool", "process_completed"]


def test_drying_until_stop_time(env, runner):
    from datetime import timedelta

    from app.models import utcnow
    _b, fake, tm, printer = env
    preset = _verify(printer)
    pid = start_drying(printer, preset, 40, None, end_at=utcnow() + timedelta(seconds=12), flip=False)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.COMPLETED, timeout=40)
    assert "Scheduled stop time" in EventLog.query.filter(EventLog.process_id == pid,
                                                          EventLog.message.contains("switching heater off")).one().message
    assert "flip_spool" not in alerts()


def test_drying_refused_unverified_printer(env, runner):
    _b, fake, tm, printer = env
    preset = _verify(printer)
    printer.heater_control_verified = False
    db.session.commit()
    pid = start_drying(printer, preset)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "not been verified" in proc_(pid).error and gcode_sent(fake) == []


def test_drying_refused_without_preparation(env, runner):
    _b, fake, tm, printer = env
    preset = _verify(printer)
    pid = start_drying(printer, preset, prep=False)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "preparation" in proc_(pid).error and gcode_sent(fake) == []


def test_drying_refused_above_material_max(env, runner):
    _b, fake, tm, printer = env
    preset = _verify(printer)
    preset.max_temp_c = 45
    db.session.commit()
    pid = start_drying(printer, preset, target=60)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "exceeds the limit" in proc_(pid).error and gcode_sent(fake) == []


# ---------------------------------------------------------------- Phase 7 annealing programs
from app.models import AnnealingPreset  # noqa: E402


def start_annealing(printer, temp=40, hold=3, up=None, down=None, cool_end=None, prep=True):
    from app.models import utcnow
    printer.heater_control_verified = True
    printer.heater_control_verified_firmware = "01.08.02.00"
    printer.motion_prepared_at = utcnow()
    preset = AnnealingPreset.query.filter_by(manufacturer="Generic", material="PLA").one()
    proc = Process(kind="annealing", printer_id=printer.id, annealing_preset_id=preset.id, printer_name=printer.name,
                   material_label="PLA (annealing)", state=ProcessState.PENDING, target_bed_c=temp,
                   planned_duration_min=hold, heat_ramp_c_per_h=up, cool_ramp_c_per_h=down, cool_end_c=cool_end,
                   prep_confirmed_at=utcnow() if prep else None)
    db.session.add(proc)
    db.session.flush()
    db.session.add(CommandRequest(printer_id=printer.id, process_id=proc.id, command="start_annealing"))
    db.session.commit()
    return proc.id


def _targets(fake):
    return [int(t.split("S")[1]) for t in gcode_sent(fake)]


def test_annealing_program_ramps_holds_and_cools(env, runner):
    _b, fake, tm, printer = env
    assert pump(tm, lambda: Printer.query.one().firmware_version_reported is not None)
    # MINUTE_S=1 in tests: 600 °C/h = 10 °C per simulated minute (1 s).
    pid = start_annealing(printer, temp=40, hold=3, up=600, down=600, cool_end=30)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.COMPLETED, timeout=60)
    p, sent = proc_(pid), _targets(fake)
    peak = sent.index(40)
    assert sent[0] < 40 and sent[:peak + 1] == sorted(sent[:peak + 1])  # stepped up
    assert sent[-1] == 0 and sent[peak:-1] == sorted(sent[peak:-1], reverse=True)  # stepped down, then off
    assert min(sent[peak:-1]) >= 30 and len(sent[peak:-1]) > 1
    assert p.heater_off_verified and p.cooldown_started_at is not None and fake.target == 0
    assert "temp_limit_exceeded" not in alerts()  # bed lagging above the falling setpoint is not an over-temp
    assert alerts() == ["process_started", "target_reached", "process_completed"]


def test_annealing_without_ramps_cools_naturally(env, runner):
    _b, fake, tm, printer = env
    assert pump(tm, lambda: Printer.query.one().firmware_version_reported is not None)
    pid = start_annealing(printer, temp=40, hold=3)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.COMPLETED, timeout=40)
    assert _targets(fake) == [40, 0] and proc_(pid).cooldown_started_at is None
    assert "naturally" in EventLog.query.filter(EventLog.process_id == pid,
                                                EventLog.message.contains("switching heater off")).one().message


def test_annealing_program_longer_than_safety_limit_refused(env, runner):
    from app.models import Setting
    _b, fake, tm, printer = env
    Setting.set("safety.max_heating_minutes", 60)
    db.session.commit()
    pid = start_annealing(printer, temp=40, hold=50, up=None, down=1, cool_end=30)  # 50 min + 600 min cool-down
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "safety" in proc_(pid).error and gcode_sent(fake) == []


def test_annealing_refused_above_preset_max(env, runner):
    _b, fake, tm, printer = env
    pid = start_annealing(printer, temp=65)  # PLA template max is 60 °C
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "exceeds the limit" in proc_(pid).error and gcode_sent(fake) == []


def test_annealing_refused_without_preparation(env, runner):
    _b, fake, tm, printer = env
    pid = start_annealing(printer, prep=False)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "preparation" in proc_(pid).error and gcode_sent(fake) == []



def test_prepare_motion_sent_and_reset_by_print(env, runner):
    _b, fake, tm, printer = env
    db.session.add(CommandRequest(printer_id=printer.id, command="prepare_motion", requested_by="test"))
    db.session.commit()
    assert run(tm, runner, lambda: CommandRequest.query.one().status == "confirmed")
    assert len(fake.motions) == 1 and fake.motions[0].startswith("G28\nG90\nG1 Z245")
    printer.motion_prepared_at = utcnow_()
    db.session.commit()
    fake._report({"print": {"gcode_state": "PREPARE"}})  # a print starts: bed will move
    assert pump(tm, lambda: Printer.query.one().motion_prepared_at is None)


def test_prepare_motion_refused_while_busy_or_active(env, runner):
    _b, fake, tm, printer = env
    preset = _verify(printer)
    pid = start_drying(printer, preset, 40, 30)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.DRYING)
    db.session.add(CommandRequest(printer_id=printer.id, command="prepare_motion", requested_by="test"))
    db.session.commit()
    assert run(tm, runner, lambda: CommandRequest.query.filter_by(command="prepare_motion").one().status == "failed")
    assert fake.motions == []  # never moves the bed under a running process


def test_drying_refused_when_not_prepared(env, runner):
    _b, fake, tm, printer = env
    preset = _verify(printer)
    printer.motion_prepared_at = None
    db.session.commit()
    pid = start_drying(printer, preset)
    assert run(tm, runner, lambda: proc_(pid).state == ProcessState.FAILED)
    assert "not prepared" in proc_(pid).error and gcode_sent(fake) == []


def utcnow_():
    from app.models import utcnow
    return utcnow()

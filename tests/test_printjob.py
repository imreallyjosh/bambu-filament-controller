import json

from app.extensions import db
from app.models import Printer, PrinterStatus, Process, ProcessState, utcnow
from app.printjob import job_info

RUNNING = {"gcode_state": "RUNNING", "subtask_name": "Benchy", "gcode_file": "/data/Metadata/plate_1.gcode",
           "mc_percent": 42, "mc_remaining_time": 75, "layer_num": 88, "total_layer_num": 210, "stg_cur": 0,
           "spd_lvl": 2, "bed_temper": 55.0}


def test_job_info_running(app):
    job = job_info(json.dumps(RUNNING))
    assert job["active"] and job["name"] == "Benchy" and job["percent"] == 42
    assert (job["layer"], job["total_layers"]) == (88, 210)
    assert job["remaining"] == "1 h 15 min" and job["eta"] and job["stage"] == "Printing" and job["speed"] == "Standard"


def test_job_info_edge_cases(app):
    assert job_info(json.dumps({"gcode_state": "IDLE"})) is None
    assert job_info(None) is None and job_info("not json") is None
    fin = job_info(json.dumps({**RUNNING, "gcode_state": "FINISH", "mc_percent": 100}))
    assert not fin["active"] and fin["state_label"] == "Finished" and fin["remaining"] is None and fin["eta"] is None
    noname = job_info(json.dumps({"gcode_state": "PREPARE", "gcode_file": "/sdcard/cache/part.gcode", "stg_cur": 99}))
    assert noname["name"] == "part.gcode" and noname["stage"] == "Stage 99" and noname["percent"] is None


def _printer(raw):
    p = Printer(name="X1", model="X1", ip_address="192.168.1.81", serial="00M00A000000001", enabled=True)
    p.access_code = "abcd1234"
    db.session.add(p)
    db.session.flush()
    db.session.add(PrinterStatus(printer_id=p.id, online=True, gcode_state=raw.get("gcode_state"),
                                 last_seen_at=utcnow(), raw_json=json.dumps(raw)))
    db.session.commit()
    return p


def test_dashboard_and_api_show_running_job(auth_client):
    p = _printer(RUNNING)
    page = auth_client.get("/").data.decode()
    assert "Benchy" in page and f'data-job="{p.id}"' in page and "88 / 210" in page and "1 h 15 min" in page
    job = auth_client.get("/api/status").get_json()["printers"][0]["job"]
    assert job["name"] == "Benchy" and job["percent"] == 42


def test_finished_job_hidden_during_app_run(auth_client):
    p = _printer({**RUNNING, "gcode_state": "FINISH"})
    assert auth_client.get("/api/status").get_json()["printers"][0]["job"]["state"] == "FINISH"
    db.session.add(Process(kind="drying", printer_id=p.id, state=ProcessState.DRYING, target_bed_c=60,
                           started_at=utcnow()))
    db.session.commit()
    assert auth_client.get("/api/status").get_json()["printers"][0]["job"] is None
    assert 'data-job="%d" hidden' % p.id in auth_client.get("/").data.decode()


def test_expected_completion_and_timing(app):
    import time as _time

    app.config["TIMEZONE"] = "UTC"
    started = int(_time.time()) - 95 * 60
    job = job_info(json.dumps({**RUNNING, "gcode_start_time": str(started), "mc_remaining_time": 30}))
    assert job["eta"].split()[0] in ("Today", "Tomorrow") and ("AM" in job["eta"] or "PM" in job["eta"])
    assert job["elapsed"] == "1 h 35 min" and job["total"] == "2 h 05 min" and job["started"]
    # Implausible or missing start time: those fields are simply absent.
    assert job_info(json.dumps({**RUNNING, "gcode_start_time": "0"}))["elapsed"] is None


def test_filament_from_ams_and_external(app):
    ams = {"tray_now": "5", "ams": [
        {"id": "0", "tray": [{"id": "1", "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 80}]},
        {"id": "1", "tray": [{"id": "1", "tray_type": "PETG", "tray_sub_brands": "PETG HF", "tray_color": "00AE42FF",
                              "remain": 63}]}]}
    fil = job_info(json.dumps({**RUNNING, "ams": ams}))["filament"]
    assert fil == {"type": "PETG HF", "color": "#00AE42", "remain": 63, "source": "AMS 2, slot 2"}
    ext = job_info(json.dumps({**RUNNING, "ams": {"tray_now": "254"},
                               "vt_tray": {"tray_type": "PA6-CF", "tray_color": "000000FF", "remain": -1}}))["filament"]
    assert ext == {"type": "PA6-CF", "color": "#000000", "remain": None, "source": "External spool"}
    assert job_info(json.dumps({**RUNNING, "ams": {"tray_now": "255"}}))["filament"] is None  # nothing loaded


def test_fans_and_problems(app):
    job = job_info(json.dumps({**RUNNING, "cooling_fan_speed": "15", "big_fan1_speed": "0", "big_fan2_speed": "10",
                               "print_error": 50348044, "hms": [{"attr": 1, "code": 2}]}))
    assert job["fans"] == "Part 100% · Aux 0% · Chamber 67%"
    assert job["error"] == "0x0300400C" and job["hms_count"] == 1
    clean = job_info(json.dumps(RUNNING))
    assert clean["error"] is None and clean["hms_count"] == 0


def test_friendly_times(app):
    from datetime import timedelta

    from app.timeutil import friendly

    app.config["TIMEZONE"] = "UTC"
    now = utcnow()
    assert friendly(now).startswith("Today ")
    assert friendly(now + timedelta(days=1)).startswith("Tomorrow ")
    assert friendly(now + timedelta(days=3))[:3] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    assert friendly(None) is None


def test_dashboard_shows_expected_completion(auth_client):
    _printer({**RUNNING, "cooling_fan_speed": "15"})
    page = auth_client.get("/").data.decode()
    assert "Expected completion" in page and "More details" in page and "Part 100%" in page

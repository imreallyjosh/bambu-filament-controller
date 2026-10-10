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

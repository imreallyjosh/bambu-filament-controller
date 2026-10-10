import json

import pytest

from controller import bambu_protocol as proto


@pytest.mark.parametrize("name", ["gcode_line", "print", "M140", "set_bed_temp", "project_file", ""])
def test_only_readonly_requests_can_be_built(name):
    with pytest.raises(PermissionError):
        proto.build_request(name)


def test_readonly_payloads():
    assert json.loads(proto.build_request("pushall"))["pushing"]["command"] == "pushall"
    assert json.loads(proto.build_request("get_version"))["info"]["command"] == "get_version"


def test_no_control_payload_text_in_module():
    src = open(proto.__file__).read()
    assert "gcode_line" not in src and "M140" not in src and "M104" not in src


def test_deep_merge_keeps_unchanged_fields():
    state = {"bed_temper": 20, "nozzle_temper": 25, "ams": {"humidity": "3", "tray": 1}}
    proto.deep_merge(state, {"bed_temper": 21, "ams": {"humidity": "4"}})
    assert state == {"bed_temper": 21, "nozzle_temper": 25, "ams": {"humidity": "4", "tray": 1}}


def test_chamber_hidden_without_sensor():
    st = {"chamber_temper": 5, "bed_temper": "30.5"}
    assert proto.extract_status(st, has_chamber_sensor=False)["chamber_temp_c"] is None
    assert proto.extract_status(st, has_chamber_sensor=True)["chamber_temp_c"] == 5.0
    assert proto.extract_status(st, has_chamber_sensor=False)["bed_temp_c"] == 30.5


def test_firmware_extraction():
    payload = {"info": {"command": "get_version", "module": [{"name": "mc", "sw_ver": "x"},
                                                             {"name": "ota", "sw_ver": "01.08.05.00"}]}}
    assert proto.extract_firmware(payload) == "01.08.05.00"
    assert proto.extract_firmware({"print": {}}) is None


def test_job_context():
    ctx = proto.job_context({"print_type": "local", "gcode_file": "", "subtask_name": "dry", "task_id": "0",
                             "mc_remaining_time": 0, "bed_temper": 50})
    assert ctx == {"print_type": "local", "subtask_name": "dry", "mc_remaining_time": 0}



def test_prepare_sequence_order_and_limits():
    from controller import bambu_control as control

    cmd = control.prepare_sequence(128, 250, 245)
    lines = [ln for ln in cmd.gcode.split("\n") if ln]
    assert lines[0] == "G28" and lines.index("G1 Z245 F600") < lines.index("G1 X128 Y250 F3000")  # bed down first
    assert json.loads(cmd.payload)["print"]["command"] == "gcode_line"
    for bad in [(128, 250, 300), (-5, 10, 200), (128, 300, 200), (128.0, 250, 245)]:
        with pytest.raises(ValueError):
            control.prepare_sequence(*bad)

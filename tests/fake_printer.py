"""A fake Bambu printer for integration tests.

Connects to a test broker as the printer would, answers 'pushall' and
'get_version' like a P1-series printer, emits delta-only status reports, and
records every request it receives so tests can assert nothing else was sent.
"""
import json
import threading
import time

import paho.mqtt.client as mqtt

FULL_STATE = {
    "gcode_state": "IDLE", "stg_cur": 0, "bed_temper": 24.5, "bed_target_temper": 0.0,
    "nozzle_temper": 26.0, "nozzle_target_temper": 0.0, "chamber_temper": 5.0,  # P1S: bogus chamber value
    "cooling_fan_speed": "0", "big_fan1_speed": "0", "big_fan2_speed": "0", "hms": [],
    "wifi_signal": "-50dBm", "mc_percent": 0, "command": "push_status",
}


class FakePrinter:
    def __init__(self, port: int, serial: str, password: str, firmware: str = "01.08.02.00"):
        self.serial, self.firmware = serial, firmware
        self.received: list[dict] = []
        self.motions: list[str] = []
        self.bed = 24.5
        self.target = 0.0
        self.mode = "accept"        # accept | reject | ignore_off | runaway
        self.heat_rate = 2.0        # °C per 0.5 s tick
        self._stop = threading.Event()
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="fake-printer")
        self.client.username_pw_set("bblp", password)
        self.client.tls_set(cert_reqs=__import__("ssl").CERT_NONE)
        self.client.tls_insecure_set(True)
        self.connected = threading.Event()
        self.client.on_subscribe = lambda *_: self.connected.set()
        self.client.on_connect = lambda c, *_: c.subscribe(f"device/{serial}/request")
        self.client.on_message = self._on_request
        self.client.reconnect_delay_set(1, 2)
        self.client.connect_async("127.0.0.1", port)
        self.client.loop_start()
        self._thread = threading.Thread(target=self._deltas, daemon=True)
        self._thread.start()

    def _report(self, body: dict) -> None:
        self.client.publish(f"device/{self.serial}/report", json.dumps(body))

    def _on_request(self, _c, _u, msg):
        req = json.loads(msg.payload)
        self.received.append(req)
        if req.get("pushing", {}).get("command") == "pushall":
            self._report({"print": {**FULL_STATE, "bed_temper": self.bed, "bed_target_temper": self.target}})
        elif req.get("print", {}).get("command") == "gcode_line":
            param = req["print"]["param"]
            seq = req["print"]["sequence_id"]
            if self.mode == "reject":
                self._report({"print": {"command": "gcode_line", "result": "failed", "reason": "unauthorized",
                                        "sequence_id": seq}})
                return
            self._report({"print": {"command": "gcode_line", "result": "success", "sequence_id": seq,
                                    "param": param}})
            if "M140" not in param:
                self.motions.append(param)
                return
            target = float(param.split("S")[1])
            if target == 0 and self.mode == "ignore_off":
                return
            self.target = target
            self._report({"print": {"bed_target_temper": self.target, "command": "push_status"}})
        elif req.get("info", {}).get("command") == "get_version":
            self._report({"info": {"command": "get_version", "sequence_id": req["info"]["sequence_id"],
                                   "module": [{"name": "ota", "sw_ver": self.firmware, "hw_ver": "OTA"},
                                              {"name": "mc", "sw_ver": "00.00.30.00"}]}})

    def _deltas(self):
        # P1-series style: only changed fields.
        while not self._stop.wait(0.5):
            if self.mode == "runaway" and self.target > 0:
                self.bed = round(self.bed + 3, 1)
            elif self.target > self.bed:
                self.bed = round(min(self.target, self.bed + self.heat_rate), 1)
            elif self.target == 0 and self.bed > 24.5:
                self.bed = round(max(24.5, self.bed - 1), 1)
            else:
                self.bed = round(self.bed + 0.1 if self.target == 0 else self.target, 1)
            self._report({"print": {"bed_temper": self.bed, "bed_target_temper": self.target,
                                    "command": "push_status", "msg": 1}})

    def stop(self):
        self._stop.set()
        self.client.disconnect()
        self.client.loop_stop()


def wait_for(predicate, timeout=15.0, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return False

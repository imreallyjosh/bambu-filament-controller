"""One read-only MQTT connection per printer.

Runs paho's network loop in its own thread. Callbacks never touch the
database; they push events onto a queue that the daemon's main thread drains.

TLS: Bambu printers use a certificate signed by Bambu's private CA with the
serial number as CN, so normal CA validation isn't possible without that CA.
Instead the certificate's SHA-256 fingerprint is pinned on first successful
connection (trust on first use) and every later connection must match it.
"""
import hashlib
import json
import logging
import queue
import ssl
import threading
import time
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

from . import bambu_protocol as proto
from .bambu_control import BedCommand, MotionCommand, command_result

log = logging.getLogger("bambu-fc-controller.mqtt")


@dataclass
class LinkConfig:
    printer_id: int
    name: str
    model: str
    host: str
    port: int
    serial: str
    access_code: str
    pinned_fingerprint: str | None

    def identity(self) -> tuple:
        """Any change to these means the connection must be rebuilt. The pinned certificate is
        excluded: the link pins it itself, and it can only be reset while the printer is disabled."""
        return (self.host, self.port, self.serial, self.access_code, self.model)


@dataclass
class LinkEvent:
    printer_id: int
    kind: str  # connected | disconnected | report | firmware | pinned | error
    data: dict = field(default_factory=dict)


def fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


class PrinterLink:
    def __init__(self, cfg: LinkConfig, events: "queue.Queue[LinkEvent]"):
        self.cfg = cfg
        self.events = events
        self.connected = False
        self.blocked = False  # set on certificate mismatch; stays down until config changes
        self.last_message_at: float | None = None
        self.last_pushall_at = 0.0
        self.print_state: dict = {}
        self._lock = threading.Lock()
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-fc-{cfg.printer_id}-{int(time.time())}",
            protocol=mqtt.MQTTv311,
        )
        self._client.username_pw_set("bblp", cfg.access_code)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # authenticity enforced by fingerprint pinning in _on_connect
        self._client.tls_set_context(ctx)
        self._client.reconnect_delay_set(min_delay=2, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        log.info("[%s] connecting to %s:%s", self.cfg.name, self.cfg.host, self.cfg.port)
        self._client.connect_async(self.cfg.host, self.cfg.port, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    # -- requests (allow-listed, read-only) -----------------------------
    def request(self, name: str) -> None:
        payload = proto.build_request(name)  # raises for anything not allow-listed
        if not self.connected:
            return
        self._client.publish(proto.request_topic(self.cfg.serial), payload, qos=0)
        if name == "pushall":
            self.last_pushall_at = time.monotonic()

    def live(self) -> dict:
        """Freshest in-memory view (not the throttled DB copy). Safety decisions use this."""
        with self._lock:
            st = self.print_state
            return {
                "connected": self.connected,
                "age_s": None if self.last_message_at is None else time.monotonic() - self.last_message_at,
                "last_message_at": self.last_message_at,
                "gcode_state": st.get("gcode_state"),
                "bed_c": proto._num(st.get("bed_temper")),
                "bed_target_c": proto._num(st.get("bed_target_temper")),
            }

    def send_motion(self, cmd: MotionCommand) -> bool:
        if not isinstance(cmd, MotionCommand):
            raise TypeError("send_motion only accepts MotionCommand from controller.bambu_control")
        if not self.connected:
            return False
        info = self._client.publish(proto.request_topic(self.cfg.serial), cmd.payload, qos=1)
        log.warning("[%s] sent prepare motion sequence (rc=%s)", self.cfg.name, info.rc)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # -- control (Phase 4): only BedCommand objects from bambu_control ---
    def send_bed(self, cmd: BedCommand) -> bool:
        """Publish a bed-target command. Returns False if not connected (caller must treat as NOT sent).
        A True return only means the message was handed to the broker; success is judged from telemetry."""
        if not isinstance(cmd, BedCommand):
            raise TypeError("send_bed only accepts BedCommand from controller.bambu_control")
        if not self.connected:
            return False
        info = self._client.publish(proto.request_topic(self.cfg.serial), cmd.payload, qos=1)
        log.warning("[%s] sent bed target %s °C (rc=%s)", self.cfg.name, cmd.target_c, info.rc)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # -- callbacks (paho thread) ----------------------------------------
    def _emit(self, kind: str, **data) -> None:
        self.events.put(LinkEvent(self.cfg.printer_id, kind, data))

    def _on_connect(self, client, _userdata, _flags, reason_code, _props):
        if reason_code.is_failure:
            msg = str(reason_code)
            if "authoriz" in msg.lower() or "password" in msg.lower():
                msg = f"{msg} - check the LAN access code (it changes if LAN mode is toggled)"
            self._emit("error", message=f"MQTT connect refused: {msg}")
            return
        try:
            der = client.socket().getpeercert(binary_form=True)
        except Exception as exc:  # pragma: no cover - defensive
            der = None
            log.warning("[%s] could not read peer certificate: %s", self.cfg.name, exc)
        fp = fingerprint(der) if der else None
        if self.cfg.pinned_fingerprint and fp != self.cfg.pinned_fingerprint:
            self._emit("error", message=(
                "TLS certificate does not match the pinned fingerprint. Refusing to use this connection. "
                "If the printer was replaced or reset, use 'Reset pinned certificate' on the printer page."))
            self.blocked = True
            client.disconnect()  # explicit disconnect: paho will not auto-reconnect
            return
        if not self.cfg.pinned_fingerprint and fp:
            self.cfg.pinned_fingerprint = fp
            self._emit("pinned", fingerprint=fp)
        self.connected = True
        client.subscribe(proto.report_topic(self.cfg.serial), qos=0)
        self._emit("connected")
        self.request("get_version")
        self.request("pushall")

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props):
        was = self.connected
        self.connected = False
        if was:
            self._emit("disconnected", reason=str(reason_code))

    def _on_message(self, _client, _userdata, msg):
        self.last_message_at = time.monotonic()
        try:
            payload = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        result = command_result(payload)
        if result is not None:
            self._emit("command_result", **result)
            return
        fw = proto.extract_firmware(payload)
        if fw:
            self._emit("firmware", version=fw, modules=payload["info"].get("module"))
        if isinstance(payload.get("print"), dict):
            with self._lock:
                proto.deep_merge(self.print_state, payload["print"])
                snapshot = json.loads(json.dumps(self.print_state))
            self._emit("report", state=snapshot)

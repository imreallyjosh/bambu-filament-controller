"""Camera service: on-demand snapshots from each printer's camera.

Runs as its own systemd service (bambu-fc-camera) so video decoding can never take down the
safety-critical controller daemon. It connects to a printer's camera only while someone is
watching: the web app touches <camera_dir>/<printer_id>.want on every snapshot request, and the
grabber stops IDLE_STOP_S after the last request.

Output per printer, written atomically:
  <id>.jpg    latest frame
  <id>.json   {"state": "...", "error": "...", "frame_at": epoch}

Protocols (OpenBambuAPI video.md):
  X1  : RTSPS rtsps://bblp:<code>@<ip>:322/streaming/live/1 (H.264), decoded in-process with PyAV
        so the access code never appears on a command line
  P1S : TLS on port 6000; 80-byte auth packet, then [16-byte header + JPEG] frames
"""
import json
import logging
import os
import signal
import socket
import ssl
import struct
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("bambu-fc-camera")

WANT_FRESH_S = 15      # a viewer is "watching" if it requested a frame this recently
IDLE_STOP_S = 30       # stop grabbing this long after the last request
FRAME_INTERVAL_S = 0.5  # write at most 2 frames/s
RECONNECT_S = 5


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class Grabber(threading.Thread):
    def __init__(self, printer_id: int, host: str, access_code: str, out_dir: Path):
        super().__init__(daemon=True, name=f"camera-{printer_id}")
        self.printer_id, self.host, self.access_code, self.out_dir = printer_id, host, access_code, out_dir
        self.stop_event = threading.Event()
        self._last_write = 0.0

    def status(self, state: str, error: str | None = None, frame_at: float | None = None) -> None:
        write_atomic(self.out_dir / f"{self.printer_id}.json",
                     json.dumps({"state": state, "error": error, "frame_at": frame_at, "at": time.time()}).encode())

    def frame(self, jpeg: bytes) -> None:
        now = time.monotonic()
        if now - self._last_write < FRAME_INTERVAL_S:
            return
        self._last_write = now
        write_atomic(self.out_dir / f"{self.printer_id}.jpg", jpeg)
        self.status("streaming", frame_at=time.time())

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.status("connecting")
                self.stream()
                if not self.stop_event.is_set():
                    self.status("reconnecting", "Stream ended")
            except Exception as exc:  # report and retry; never crash the service
                log.warning("[printer %s] camera error: %s", self.printer_id, exc)
                self.status("error", str(exc)[:300])
            self.stop_event.wait(RECONNECT_S)
        self.status("stopped")

    def stream(self) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError


class JpegTcpGrabber(Grabber):
    """P1-series chamber camera: TLS on port 6000 streaming JPEG frames."""

    port = 6000

    def auth_packet(self) -> bytes:
        return (struct.pack("<IIII", 0x40, 0x3000, 0, 0)
                + b"bblp".ljust(32, b"\0") + self.access_code.encode("ascii").ljust(32, b"\0"))

    def stream(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # self-signed printer cert; the access code authenticates us
        with socket.create_connection((self.host, self.port), timeout=10) as raw, \
                ctx.wrap_socket(raw, server_hostname=self.host) as sock:
            sock.settimeout(15)
            sock.sendall(self.auth_packet())
            while not self.stop_event.is_set():
                header = self._recv_exact(sock, 16)
                size = struct.unpack_from("<I", header, 0)[0]
                if not 0 < size < 10_000_000:
                    raise ValueError(f"Unexpected frame size {size}")
                payload = self._recv_exact(sock, size)
                if payload[:2] == b"\xff\xd8" and payload[-2:] == b"\xff\xd9":
                    self.frame(payload)

    @staticmethod
    def _recv_exact(sock, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(min(65536, n - len(buf)))
            if not chunk:
                raise ConnectionError("Camera closed the connection (wrong access code, or camera disabled?)")
            buf += chunk
        return bytes(buf)


LIVEVIEW_OFF = ("The X1's LAN live view is switched off. On the printer's touchscreen, open the network/LAN "
                "settings and turn on \"LAN Only Liveview\" (some firmware calls it \"LAN Mode Liveview\").")


class RtspGrabber(Grabber):
    """X1-series camera: RTSPS H.264, decoded with PyAV and re-encoded to JPEG."""

    port = 322
    width, height = 1280, 720

    def __init__(self, *args, reported_url: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        # From the printer's status report (print.ipcam.rtsp_url): "disable", or the stream URL without credentials.
        self.reported_url = reported_url

    def url(self) -> str:
        return f"rtsps://bblp:{self.access_code}@{self.host}:{self.port}/streaming/live/1"

    def stream(self) -> None:
        if self.reported_url == "disable":
            raise RuntimeError(LIVEVIEW_OFF)
        try:
            self._stream()
        except ConnectionRefusedError as exc:
            raise RuntimeError(LIVEVIEW_OFF) from exc
        except OSError as exc:
            if getattr(exc, "errno", None) == 111 or "Connection refused" in str(exc):
                raise RuntimeError(LIVEVIEW_OFF) from exc
            raise

    def _stream(self) -> None:
        import av

        options = {"rtsp_transport": "tcp", "timeout": "10000000"}  # 10 s socket timeout (µs)
        with av.open(self.url(), options=options, timeout=15) as container:
            video = container.streams.video[0]
            video.thread_type = "AUTO"
            encoder = av.CodecContext.create("mjpeg", "w")
            encoder.width, encoder.height, encoder.pix_fmt = self.width, self.height, "yuvj420p"
            encoder.time_base = Fraction(1, 30)
            encoder.options = {"q:v": "5"}
            seq = 0
            for frame in container.decode(video):
                if self.stop_event.is_set():
                    return
                if time.monotonic() - self._last_write < FRAME_INTERVAL_S:
                    continue
                small = frame.reformat(width=self.width, height=self.height, format="yuvj420p")
                # The encoder rejects (EINVAL) frames without increasing timestamps; source timestamps don't
                # apply to independent JPEGs, so number them ourselves.
                small.pts, small.time_base = seq, encoder.time_base
                seq += 1
                packets = encoder.encode(small)
                if packets:
                    self.frame(bytes(packets[0]))


GRABBERS = {"rtsps": RtspGrabber, "jpeg_tcp": JpegTcpGrabber}


def reported_rtsp_url(printer) -> str | None:
    """The live-view URL the printer advertises in its status report, if telemetry has captured it."""
    try:
        state = json.loads(printer.status.raw_json) if printer.status and printer.status.raw_json else {}
    except ValueError:
        return None
    ipcam = state.get("ipcam") if isinstance(state, dict) else None
    return ipcam.get("rtsp_url") if isinstance(ipcam, dict) else None


class CameraService:
    def __init__(self, app, out_dir: Path):
        self.app, self.out_dir = app, out_dir
        self.grabbers: dict[int, Grabber] = {}
        self.last_wanted: dict[int, float] = {}

    def wanted(self) -> set[int]:
        now = time.time()
        ids = set()
        for f in self.out_dir.glob("*.want"):
            try:
                pid = int(f.stem)
            except ValueError:
                continue
            if now - f.stat().st_mtime < WANT_FRESH_S:
                ids.add(pid)
        return ids

    def tick(self) -> None:
        from app.capabilities import profile_for
        from app.extensions import db
        from app.models import Printer

        now = time.monotonic()
        for pid in self.wanted():
            self.last_wanted[pid] = now
            if pid in self.grabbers and self.grabbers[pid].is_alive():
                continue
            printer = db.session.get(Printer, pid)
            if printer is None or not printer.enabled:
                continue
            kind = profile_for(printer.model).camera_kind
            extra = {"reported_url": reported_rtsp_url(printer)} if kind == "rtsps" else {}
            grabber = GRABBERS[kind](pid, printer.ip_address, printer.access_code, self.out_dir, **extra)
            log.info("[%s] starting %s camera", printer.name, kind)
            grabber.start()
            self.grabbers[pid] = grabber
        for pid, grabber in list(self.grabbers.items()):
            if now - self.last_wanted.get(pid, 0) > IDLE_STOP_S:
                log.info("[printer %s] no viewers; stopping camera", pid)
                grabber.stop_event.set()
                del self.grabbers[pid]
        db.session.remove()

    def stop_all(self) -> None:
        for grabber in self.grabbers.values():
            grabber.stop_event.set()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from app import create_app

    app = create_app()
    out_dir = Path(app.config["CAMERA_DIR"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    service = CameraService(app, out_dir)
    with app.app_context():
        while not stop.is_set():
            try:
                service.tick()
            except Exception:
                log.exception("camera tick failed")
            stop.wait(1.0)
    service.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())

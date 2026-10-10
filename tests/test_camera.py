"""Camera service: P1-series JPEG-over-TLS protocol against a fake camera, X1 decode path against a
generated H.264 file, the snapshot route, and on-demand start/stop."""
import json
import shutil
import socket
import ssl
import struct
import subprocess
import os
import threading
import time
from pathlib import Path

import pytest

from app.extensions import db
from app.models import Printer
from controller import camera as cam

from .fake_printer import wait_for

JPEG = b"\xff\xd8" + b"fakejpegdata" * 50 + b"\xff\xd9"


def _cert(tmp_path):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=cam",
                    "-keyout", tmp_path / "k.pem", "-out", tmp_path / "c.pem"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    return ctx


class FakeP1Camera(threading.Thread):
    """Speaks the P1-series camera protocol: expects the 80-byte auth packet, then sends frames."""

    def __init__(self, tmp_path, code="abcd1234"):
        super().__init__(daemon=True)
        self.ctx, self.code, self.auth = _cert(tmp_path), code, None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    def run(self):
        try:
            self._serve()
        except (ssl.SSLError, OSError):
            pass  # client hung up; expected when the grabber stops

    def _serve(self):
        conn, _ = self.sock.accept()
        with self.ctx.wrap_socket(conn, server_side=True) as tls:
            self.auth = tls.recv(80)
            if self.auth[48:80].rstrip(b"\0").decode() != self.code:
                return  # wrong access code: close, as the printer does
            for _ in range(5):
                tls.sendall(struct.pack("<IIII", len(JPEG), 0, 1, 0) + JPEG[:20])
                time.sleep(0.05)
                tls.sendall(JPEG[20:])  # split across reads, as on the real printer
                time.sleep(0.6)


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_p1_camera_protocol(tmp_path):
    server = FakeP1Camera(tmp_path)
    server.start()
    grabber = cam.JpegTcpGrabber(1, "127.0.0.1", "abcd1234", tmp_path)
    grabber.port = server.port
    grabber.start()
    assert wait_for(lambda: (tmp_path / "1.jpg").exists(), 10)
    grabber.stop_event.set()
    assert (tmp_path / "1.jpg").read_bytes() == JPEG
    assert struct.unpack_from("<IIII", server.auth) == (0x40, 0x3000, 0, 0)
    assert server.auth[16:48].rstrip(b"\0") == b"bblp" and len(server.auth) == 80
    assert json.loads((tmp_path / "1.json").read_text())["state"] in ("streaming", "stopped", "reconnecting")


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_p1_camera_wrong_code_reports_error(tmp_path):
    server = FakeP1Camera(tmp_path, code="otherone")
    server.start()
    grabber = cam.JpegTcpGrabber(2, "127.0.0.1", "abcd1234", tmp_path)
    grabber.port = server.port
    grabber.start()
    assert wait_for(lambda: (tmp_path / "2.json").exists()
                    and "wrong access code" in (json.loads((tmp_path / "2.json").read_text()).get("error") or ""), 10)
    grabber.stop_event.set()
    assert not (tmp_path / "2.jpg").exists()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_x1_decode_path_produces_jpegs(tmp_path, monkeypatch):
    video = tmp_path / "test.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30",
                    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    frames = []

    class FileGrabber(cam.RtspGrabber):
        def url(self):
            return str(video)

        def frame(self, jpeg):
            frames.append(jpeg)
            super().frame(jpeg)

    monkeypatch.setattr(cam, "FRAME_INTERVAL_S", 0)  # encode every decoded frame, not one per 0.5 s
    grabber = FileGrabber(3, "unused", "unused", tmp_path)
    grabber.start()
    # Many consecutive frames must encode: the first one succeeding is not enough (regression: EINVAL on frame 2).
    assert wait_for(lambda: len(frames) >= 30, 30)
    grabber.stop_event.set()
    assert all(f[:2] == b"\xff\xd8" and f[-2:] == b"\xff\xd9" and len(f) > 1000 for f in frames)
    assert json.loads((tmp_path / "3.json").read_text()).get("error") is None


def test_rtsp_url_format():
    g = cam.RtspGrabber(1, "192.168.1.81", "abcd1234", Path("/tmp"))
    assert g.url() == "rtsps://bblp:abcd1234@192.168.1.81:322/streaming/live/1"


def _printer():
    p = Printer(name="P1S", model="P1S", ip_address="192.168.1.80", serial="01P00A000000001", enabled=True)
    p.access_code = "abcd1234"
    db.session.add(p)
    db.session.commit()
    return p


def test_snapshot_route(auth_client, app, tmp_path):
    app.config["CAMERA_DIR"] = str(tmp_path)
    p = _printer()
    resp = auth_client.get(f"/printers/{p.id}/camera.jpg")
    assert resp.status_code == 503 and (tmp_path / f"{p.id}.want").exists()
    (tmp_path / f"{p.id}.jpg").write_bytes(JPEG)
    (tmp_path / f"{p.id}.json").write_text(json.dumps({"state": "streaming"}))
    resp = auth_client.get(f"/printers/{p.id}/camera.jpg")
    assert resp.status_code == 200 and resp.data == JPEG and resp.headers["Cache-Control"] == "no-store"
    (tmp_path / f"{p.id}.json").write_text(json.dumps({"state": "error", "error": "Camera closed the connection"}))
    resp = auth_client.get(f"/printers/{p.id}/camera.jpg")
    assert resp.status_code == 503 and b"Camera closed" in resp.data
    page = auth_client.get("/").data.decode()
    assert "Show camera" in page and f"/printers/{p.id}/camera.jpg" in page


def test_camera_requires_login(client):
    assert client.get("/printers/1/camera.jpg").status_code == 302


def test_service_starts_on_demand_and_stops_when_idle(app, tmp_path, monkeypatch):
    pid = _printer().id  # the service resets its DB session every tick
    started = []

    class Dummy(cam.Grabber):
        def stream(self):
            started.append(self.access_code)
            self.stop_event.wait()

    monkeypatch.setitem(cam.GRABBERS, "jpeg_tcp", Dummy)
    service = cam.CameraService(app, tmp_path)
    service.tick()
    assert service.grabbers == {}  # nobody watching: no connection
    (tmp_path / f"{pid}.want").touch()
    service.tick()
    assert wait_for(lambda: started == ["abcd1234"], 5)
    monkeypatch.setattr(cam, "IDLE_STOP_S", 0)
    os.utime(tmp_path / f"{pid}.want", (time.time() - 60, time.time() - 60))  # viewer went away
    service.tick()
    assert service.grabbers == {}


def test_dashboard_has_fullscreen_controls(auth_client):
    p = _printer()
    page = auth_client.get("/").data.decode()
    assert f'data-camera-fullscreen="camera-{p.id}"' in page and "data-camera-exit" in page



def test_x1_liveview_off_reported_clearly(tmp_path):
    g = cam.RtspGrabber(4, "127.0.0.1", "abcd1234", tmp_path, reported_url="disable")
    g.start()
    assert wait_for(lambda: (tmp_path / "4.json").exists()
                    and "LAN Only Liveview" in (json.loads((tmp_path / "4.json").read_text()).get("error") or ""), 10)
    g.stop_event.set()


def test_x1_connection_refused_explained(tmp_path):
    with socket.socket() as s:  # find a closed port
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    g = cam.RtspGrabber(5, "127.0.0.1", "abcd1234", tmp_path)
    g.port = closed
    g.start()
    assert wait_for(lambda: (tmp_path / "5.json").exists()
                    and "LAN Only Liveview" in (json.loads((tmp_path / "5.json").read_text()).get("error") or ""), 20)
    g.stop_event.set()


def test_reported_rtsp_url_from_status(app):
    from app.models import PrinterStatus

    p = _printer()
    db.session.add(PrinterStatus(printer_id=p.id, online=True, raw_json=json.dumps({"ipcam": {"rtsp_url": "disable"}})))
    db.session.commit()
    assert cam.reported_rtsp_url(p) == "disable"

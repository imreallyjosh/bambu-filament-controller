"""Configuration loading.

All secrets come from environment variables, which systemd loads from
/etc/bambu-filament-controller/controller.env (mode 0640, root:bambufc).
Nothing secret lives in the source tree.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.environ.get("BFC_SECRET_KEY")
    # Fernet key used to encrypt printer access codes and notification credentials at rest.
    FERNET_KEY = os.environ.get("BFC_FERNET_KEY")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "BFC_DATABASE_URI", "sqlite:////var/lib/bambu-filament-controller/controller.db"
    )
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 15}}

    # Display timezone; defaults to the server's configured local timezone.
    TIMEZONE = os.environ.get("BFC_TIMEZONE") or None

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Set true if you put the app behind HTTPS (e.g. your nginx-proxy-manager).
    SESSION_COOKIE_SECURE = _bool("BFC_COOKIE_SECURE", False)
    REMEMBER_COOKIE_HTTPONLY = True
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12
    WTF_CSRF_TIME_LIMIT = None  # token valid for the session lifetime

    # Master interlock for all printer control (bed heater, prepare motion). Heating additionally
    # requires per-printer verification by a supervised heater test.
    ALLOW_HEATER_CONTROL = _bool("BFC_ALLOW_HEATER_CONTROL", False)
    # MQTT telemetry and control channel to the printers.
    ENABLE_MQTT = _bool("BFC_ENABLE_MQTT", False)

    # Camera service writes the latest snapshot per printer here; the web app serves it.
    CAMERA_DIR = os.environ.get("BFC_CAMERA_DIR", "/var/lib/bambu-filament-controller/camera")

    # Controller daemon is considered dead if its heartbeat is older than this.
    CONTROLLER_HEARTBEAT_STALE_S = 30


class DevConfig(Config):
    """Local development: everything inside ./instance."""

    _inst = Path(__file__).resolve().parent.parent / "instance"
    SQLALCHEMY_DATABASE_URI = os.environ.get("BFC_DATABASE_URI", f"sqlite:///{_inst / 'dev.db'}")
    CAMERA_DIR = os.environ.get("BFC_CAMERA_DIR", str(_inst / "camera"))


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret"
    FERNET_KEY = Fernet.generate_key().decode()
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False

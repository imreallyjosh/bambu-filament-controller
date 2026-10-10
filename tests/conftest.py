import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import User
from app.seed import seed_presets


@pytest.fixture()
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        user = User(username="admin")
        user.set_password("correct-horse-battery")
        db.session.add(user)
        db.session.commit()
        seed_presets()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def auth_client(client):
    client.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
    return client


PRINTER = {
    "name": "Garage P1S", "model": "P1S", "ip_address": "192.168.1.80", "serial": "01P00A000000001",
    "access_code": "abcd1234", "firmware_version": "01.08.00.00", "network_mode": "lan_only_developer",
    "max_bed_temp_c": "", "notes": "",
}

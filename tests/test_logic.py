"""Unit tests: no database needed. Run:  pytest -q"""
import base64
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402  (importing does not touch the database)


def settings(monkeypatch, **cfg):
    monkeypatch.setattr(main, "setting", lambda k, d=None: cfg.get(k, d))


# ---------------------------------------------------------------- lateness
@pytest.mark.parametrize("shift,now,expected", [
    ("09:00", (8, 30), ("early", -30)),
    ("09:00", (9, 0), ("on_time", 0)),
    ("09:00", (9, 10), ("on_time", 10)),   # exactly at the grace limit
    ("09:00", (9, 11), ("late", 11)),
    ("09:30", (12, 30), ("late", 180)),
])
def test_classify(shift, now, expected):
    assert main.classify(shift, datetime(2026, 10, 7, *now), 10) == expected


def test_classify_respects_grace():
    assert main.classify("09:00", datetime(2026, 10, 7, 9, 20), 30)[0] == "on_time"
    assert main.classify("09:00", datetime(2026, 10, 7, 9, 20), 0)[0] == "late"


# ---------------------------------------------------------------- distance and the geofence
def test_distance_known_values():
    assert main.dist(22.5726, 88.3639, 22.5726, 88.3639) == 0
    assert 110 < main.dist(22.0, 88.0, 22.001, 88.0) < 112          # 0.001 degree of latitude is about 111 m
    assert 20000 < main.dist(22.57, 88.36, 22.57, 88.36 + 0.2) < 21000


OFFICE = dict(office_lat="22.5726", office_lng="88.3639", radius="150", office_name="Head Office")


def test_geofence_fails_closed_without_an_office(monkeypatch):
    settings(monkeypatch)
    with pytest.raises(HTTPException) as e:
        main.check_place({"lat": 22.5726, "lng": 88.3639, "acc": 10})
    assert e.value.status_code == 403 and "office" in e.value.detail


def test_geofence_inside_and_outside(monkeypatch):
    settings(monkeypatch, **OFFICE)
    assert main.check_place({"lat": 22.5727, "lng": 88.3640, "acc": 10}) == (22.5727, 88.3640, 10.0)
    with pytest.raises(HTTPException) as e:
        main.check_place({"lat": 22.60, "lng": 88.40, "acc": 10})
    assert e.value.status_code == 403 and "Head Office" in e.value.detail


@pytest.mark.parametrize("acc", [None, "abc", -5, float("nan")])
def test_geofence_needs_a_real_accuracy(monkeypatch, acc):
    settings(monkeypatch, **OFFICE)
    with pytest.raises(HTTPException) as e:
        main.check_place({"lat": 22.5727, "lng": 88.3640, "acc": acc})
    assert e.value.status_code == 400


def test_geofence_rejects_a_poor_fix(monkeypatch):
    settings(monkeypatch, **OFFICE)
    with pytest.raises(HTTPException) as e:
        main.check_place({"lat": 22.5727, "lng": 88.3640, "acc": main.MAX_ACCURACY + 1})
    assert e.value.status_code == 400 and "weak" in e.value.detail


@pytest.mark.parametrize("body", [{}, {"lat": "x", "lng": 1, "acc": 5}, {"lat": 95, "lng": 0, "acc": 5}, {"lat": 0, "lng": 200, "acc": 5}])
def test_bad_coordinates(body):
    with pytest.raises(HTTPException) as e:
        main.pos(body)
    assert e.value.status_code == 400


# ---------------------------------------------------------------- working days and holidays
def test_day_off(monkeypatch):
    settings(monkeypatch, holidays="2026-10-20, 2026-12-25")
    assert main.is_day_off("2026-10-10") is True      # a Saturday
    assert main.is_day_off("2026-10-12") is False     # a Monday
    assert main.is_day_off("2026-10-20") is True      # listed holiday
    settings(monkeypatch, workdays="0,1,2,3,4,5,6")
    assert main.is_day_off("2026-10-10") is False     # Saturday is now a working day


# ---------------------------------------------------------------- passwords
def test_password_roundtrip_and_salting():
    h1, h2 = main.hpw("correct horse"), main.hpw("correct horse")
    assert h1 != h2 and h1.startswith("s15$")
    assert main.verify("correct horse", h1) and not main.verify("wrong", h1)


def test_legacy_hashes_still_verify():
    legacy = "ab12cd:" + main._scrypt("old-password", "ab12cd", 2**14)
    assert main.verify("old-password", legacy) and not main.verify("nope", legacy)


# ---------------------------------------------------------------- photos and faces
def test_photo_bytes_validation():
    ok = base64.b64encode(b"x" * 100).decode()
    assert main.photo_bytes({"photo": ok}) == b"x" * 100
    assert main.photo_bytes({"photo": "data:image/jpeg;base64," + ok}) == b"x" * 100
    for bad in ({}, {"photo": ""}, {"photo": "not base64!!"}, {"photo": base64.b64encode(b"x" * 700_000).decode()}):
        with pytest.raises(HTTPException) as e:
            main.photo_bytes(bad)
        assert e.value.status_code == 400


def test_cosine():
    assert main.cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert main.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert main.cosine([1, 2, 3], [-1, -2, -3]) == pytest.approx(-1.0)


def test_face_is_refused_unless_approved():
    for status, text in (("none", "Register"), ("pending", "waiting")):
        with pytest.raises(HTTPException) as e:
            main.verify_face({"face_status": status}, {})
        assert e.value.status_code == 403 and text in e.value.detail


# ---------------------------------------------------------------- network helpers
def fake_request(host="9.9.9.9", scheme="http", **headers):
    return SimpleNamespace(client=SimpleNamespace(host=host), url=SimpleNamespace(scheme=scheme),
                           headers={k.replace("_", "-"): v for k, v in headers.items()})


def test_client_ip_trusts_forwarded_headers_only_from_a_local_proxy():
    assert main.client_ip(fake_request("203.0.113.5", x_forwarded_for="1.2.3.4")) == "203.0.113.5"   # spoof attempt ignored
    assert main.client_ip(fake_request("127.0.0.1", cf_connecting_ip="1.2.3.4")) == "1.2.3.4"
    assert main.client_ip(fake_request("127.0.0.1", x_forwarded_for="5.6.7.8, 10.0.0.1")) == "5.6.7.8"
    assert main.client_ip(fake_request("127.0.0.1")) == "127.0.0.1"


def test_is_https():
    assert main.is_https(fake_request(scheme="https"))
    assert main.is_https(fake_request(x_forwarded_proto="https"))
    assert not main.is_https(fake_request())


# ---------------------------------------------------------------- HTTP layer (database calls are stubbed)
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "SECRET", "unit-test-secret-at-least-32-characters-long")
    return TestClient(main.app)


def test_pages_and_static(client):
    for path in ("/", "/admin", "/sw.js", "/static/manifest-employee.json"):
        assert client.get(path).status_code == 200
    r = client.get("/")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_protected_routes_need_a_login(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/admin/data").status_code == 401
    assert client.post("/api/checkin", json={}).status_code == 401


def test_cross_site_writes_are_blocked(client):
    assert client.post("/api/login", json={}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_login_sets_a_secure_cookie_over_https_and_a_correct_expiry(client, monkeypatch):
    user = {"id": 7, "pw": main.hpw("hunter2hunter2"), "status": "active", "role": "employee"}
    monkeypatch.setattr(main, "one", lambda *a: user)
    monkeypatch.setattr(main, "attempts", lambda *a, **k: 0)
    monkeypatch.setattr(main, "note", lambda *a, **k: None)
    body = {"username": "x@example.com", "password": "hunter2hunter2"}
    plain = client.post("/api/login", json=body)
    assert plain.status_code == 200 and "secure" not in plain.headers["set-cookie"].lower()
    tunnel = client.post("/api/login", json=body, headers={"X-Forwarded-Proto": "https"})
    cookie = tunnel.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    token = cookie.split("token=")[1].split(";")[0]
    exp = jwt.decode(token, "unit-test-secret-at-least-32-characters-long", algorithms=["HS256"])["exp"]
    hours = (exp - datetime.now().timestamp()) / 3600
    assert 7 * 24 - 0.1 < hours < 7 * 24 + 0.1          # seven days from now, whatever the server's UTC offset


def test_login_is_throttled(client, monkeypatch):
    monkeypatch.setattr(main, "attempts", lambda kind, ip, user=None, secs=900: 10 if user else 0)
    assert client.post("/api/login", json={"username": "a@b.co", "password": "x"}).status_code == 429


def test_register_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(main, "attempts", lambda *a, **k: 20)
    assert client.post("/api/register", json={}).status_code == 429


def test_register_validation(client, monkeypatch):
    monkeypatch.setattr(main, "attempts", lambda *a, **k: 0)
    monkeypatch.setattr(main, "note", lambda *a, **k: None)
    monkeypatch.setattr(main, "one", lambda sql, *a: {"c": 0} if "count(*)" in sql else None)
    ok = {"name": "A", "username": "a@example.com", "password": "longenough1"}
    assert client.post("/api/register", json={**ok, "password": "short7!"}).status_code == 400   # 7 characters
    assert client.post("/api/register", json={**ok, "username": "bad name"}).status_code == 400
    assert client.post("/api/register", json=ok).status_code == 400                                # no selfie


def test_first_start_requires_an_admin_password(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "q", lambda *a: [])
    monkeypatch.setattr(main, "run", lambda *a: calls.append(a) or 0)
    monkeypatch.setattr(main, "one", lambda sql, *a: None)    # no admin, no secret yet
    monkeypatch.setattr(main, "setting", lambda k, d=None: None)
    monkeypatch.setattr(main, "PHOTOS", Path(__file__).parent / "_tmp_photos")
    monkeypatch.setattr(main, "MODELS", Path(__file__).parent / "_tmp_models")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(main, "env", lambda k, d=None: {}.get(k, d))
    with pytest.raises(SystemExit) as e:
        main.init()
    assert "ADMIN_PASSWORD" in str(e.value)
    for d in (main.PHOTOS, main.MODELS):
        if d.exists():
            d.rmdir()

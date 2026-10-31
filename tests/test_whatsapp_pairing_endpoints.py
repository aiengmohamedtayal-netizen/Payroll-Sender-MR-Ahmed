"""
اختبارات تكاملية لنقاط نهاية الربط برقم الهاتف لواتساب (Phone Number Pairing Endpoints).
"""

from unittest.mock import patch
from fastapi.testclient import TestClient
import pytest

from main import app


@pytest.fixture
def auth_client(monkeypatch):
    import auth
    monkeypatch.setattr(auth, "get_user_by_token", lambda tok: {"id": 1, "name": "Test User", "email": "test@example.com", "verified": True})
    with TestClient(app) as client:
        client.cookies.set("session_token", "dummy_token_123")
        yield client


def test_pair_phone_invalid_number(auth_client):
    # رقم غير صالح
    res = auth_client.post("/api/whatsapp/pair-phone", json={"phone": "123", "country_code": "20"})
    assert res.status_code == 422 or res.status_code == 400


def test_pair_phone_mocked_success(auth_client):
    with patch("sender.request_phone_pairing") as mock_pair:
        mock_pair.return_value = (True, "تم إنشاء كود الربط بنجاح.", "ABCD-1234")
        res = auth_client.post("/api/whatsapp/pair-phone", json={"phone": "01012345678", "country_code": "20"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["pairing_code"] == "ABCD-1234"


def test_cancel_pairing_success(auth_client):
    with patch("sender.cancel_phone_pairing") as mock_cancel:
        mock_cancel.return_value = True
        res = auth_client.post("/api/whatsapp/cancel-pairing")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

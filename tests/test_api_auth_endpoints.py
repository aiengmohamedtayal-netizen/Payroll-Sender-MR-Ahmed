"""
اختبارات تكاملية لنقاط نهاية المصادقة في FastAPI (API Integration Tests).
"""

import json
import os
import shutil
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

import auth
import email_service
from main import app


@pytest.fixture
def client_isolated(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, "api_test_users.db")
    temp_config = os.path.join(temp_dir, "email_config.json")

    monkeypatch.setattr(auth, "DB_PATH", temp_db)
    monkeypatch.setattr(email_service, "DATA_DIR", temp_dir)
    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "development")

    auth.init_db()
    try:
        import models
        with auth.get_db() as db:
            db.query(models.User).filter(models.User.email.in_([
                "sara@company.com", "ahmed@company.com", "mahmoud@company.com"
            ])).delete(synchronize_session=False)
            db.commit()
    except Exception:
        pass

    # تجاوز التحقق الخارجي من MX records في بيئة الاختبار
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)

    with TestClient(app) as test_client:
        yield {
            "client": test_client,
            "dir": temp_dir,
            "config": temp_config,
        }

    try:
        import models
        with auth.get_db() as db:
            db.query(models.User).filter(models.User.email.in_([
                "sara@company.com", "ahmed@company.com", "mahmoud@company.com"
            ])).delete(synchronize_session=False)
            db.commit()
    except Exception:
        pass

    shutil.rmtree(temp_dir, ignore_errors=True)


def test_api_email_status_endpoint(client_isolated):
    """التحقق من نقطة نهاية حالة إعدادات البريد."""
    c = client_isolated["client"]
    resp = c.get("/api/auth/email-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "ready" in data
    assert "status" in data
    assert "message" in data


def test_api_register_with_missing_config_fails_in_prod(client_isolated, monkeypatch):
    """التحقق من رفض التسجيل بـ 503 عند غياب إعدادات البريد في الإنتاج."""
    monkeypatch.setenv("APP_ENV", "production")
    c = client_isolated["client"]

    resp = c.post("/api/auth/register", json={
        "name": "أحمد محمود",
        "email": "ahmed@company.com",
        "password": "Password123",
    })

    assert resp.status_code == 503
    detail = resp.json().get("detail", {})
    assert "إعدادات البريد الإلكتروني غير مكتملة" in detail.get("message", "")


def test_api_register_and_verify_flow_dev_mode(client_isolated, monkeypatch):
    """التحقق من مسار التسجيل والتفعيل كاملاً في وضع التطوير مع استخراج كود التفعيل ومطابقته."""
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    c = client_isolated["client"]

    reg_resp = c.post("/api/auth/register", json={
        "name": "سارة علي",
        "email": "sara@company.com",
        "password": "Password123",
    })

    assert reg_resp.status_code == 200
    reg_data = reg_resp.json()
    assert reg_data["simulated"] is True
    otp_code = reg_data["dev_code"]
    assert otp_code is not None
    assert len(otp_code) == 6

    # تفعيل الحساب
    ver_resp = c.post("/api/auth/verify", json={
        "email": "sara@company.com",
        "code": otp_code,
    })

    assert ver_resp.status_code == 200
    assert ver_resp.json()["status"] == "ok"
    assert auth.SESSION_COOKIE in ver_resp.cookies


def test_api_register_with_smtp_mock(client_isolated, monkeypatch):
    """التحقق من تدفق التسجيل عند توفر إعدادات SMTP موك."""
    cfg_data = {
        "host": "smtp.gmail.com",
        "port": 587,
        "username": "admin@company.com",
        "password": "validmockpassword16",
    }
    with open(client_isolated["config"], "w", encoding="utf-8") as f:
        json.dump(cfg_data, f)

    with patch("email_service._send_smtp") as mock_send:
        c = client_isolated["client"]
        resp = c.post("/api/auth/register", json={
            "name": "محمود عادل",
            "email": "mahmoud@company.com",
            "password": "Password123",
        })

        assert resp.status_code == 200
        assert mock_send.called
        data = resp.json()
        assert data["simulated"] is False
        assert data["dev_code"] is None

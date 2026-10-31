"""
مجموعة اختبارات شاملة لخدمة البريد والمصادقة (Email & Auth Test Suite).
تغطي الاختبارات المطلوبة:
1. Valid SMTP configuration.
2. Missing email_config.json.
3. Invalid SMTP credentials.
4. SMTP connection failure.
5. Invalid recipient address.
6. Successful email submission using a mocked SMTP server.
7. OTP expiration.
8. OTP incorrect attempts.
9. OTP successful verification.
10. OTP reuse prevention.
11. Real Gmail SMTP delivery test (conditioned on real credentials existence).
"""

import json
import os
import shutil
import smtplib
import socket
import ssl
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

# تأكيد مسار المشروع
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import email_service


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """عزل بيئة الاختبار في مجلد مؤقت وقاعدة بيانات مؤقتة."""
    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, "test_users.db")
    temp_config = os.path.join(temp_dir, "email_config.json")

    monkeypatch.setattr(auth, "DB_PATH", temp_db)
    monkeypatch.setattr(email_service, "DATA_DIR", temp_dir)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    # تهيئة جدول المستخدمين وتنظيف إيميلات الاختبار السابقة
    auth.init_db()
    try:
        import models
        with auth.get_db() as db:
            db.query(models.User).filter(models.User.email.in_([
                "omar@example.com", "khaled@example.com", "mona@example.com", "yasser@example.com"
            ])).delete(synchronize_session=False)
            db.commit()
    except Exception:
        pass

    yield {
        "dir": temp_dir,
        "db": temp_db,
        "config": temp_config,
    }

    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 1: Valid SMTP configuration
# ---------------------------------------------------------------------------
def test_valid_smtp_configuration(isolated_environment):
    """1. التحقق من قراءة ومعالجة وتدقيق إعدادات SMTP الصالحة وإزالة المسافات من App Password."""
    config_data = {
        "host": "smtp.gmail.com",
        "port": 587,
        "username": "testuser@gmail.com",
        "password": "abcd efgh ijkl mnop",  # Google App Password with spaces
        "from_name": "نظام الرواتب",
        "use_tls": True,
        "timeout": 25,
    }
    with open(isolated_environment["config"], "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    cfg, status, msg = email_service.load_email_config()
    assert status == email_service.ConfigStatus.VALID
    assert cfg is not None
    assert cfg.host == "smtp.gmail.com"
    assert cfg.port == 587
    assert cfg.username == "testuser@gmail.com"
    # تحقق من إزالة المسافات تلقائياً لكلمات مرور Google App Passwords
    assert cfg.password == "abcdefghijklmnop"
    assert cfg.use_tls is True
    assert cfg.timeout == 25


# ---------------------------------------------------------------------------
# Test 2: Missing email_config.json
# ---------------------------------------------------------------------------
def test_missing_email_config(isolated_environment, monkeypatch):
    """2. التحقق من التعامل الصحيح عند غياب ملف email_config.json."""
    if os.path.exists(isolated_environment["config"]):
        os.remove(isolated_environment["config"])

    # في الإنتاج، يجب أن يفشل بخطأ إعدادات صريح
    monkeypatch.setenv("APP_ENV", "production")
    cfg, status, msg = email_service.load_email_config()
    assert cfg is None
    assert status == email_service.ConfigStatus.MISSING_FILE

    with pytest.raises(email_service.EmailConfigError) as exc_info:
        email_service.send_otp_email("recipient@domain.com", "مستخدم", "123456")
    assert "غير موجود" in str(exc_info.value.message)


# ---------------------------------------------------------------------------
# Test 3: Invalid SMTP credentials
# ---------------------------------------------------------------------------
def test_invalid_smtp_credentials(isolated_environment):
    """3. التحقق من رصد ومعالجة خطأ بيانات الدخول (Authentication Error)."""
    cfg = email_service.EmailConfig(
        host="smtp.gmail.com",
        port=587,
        username="wrong@gmail.com",
        password="wrongpassword1234",
    )
    msg = email_service.build_verification_email(cfg, "target@example.com", "أحمد", "654321")

    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_instance = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_instance
        # محاكاة خطأ كلمة مرور خاطئة من جوجل
        mock_instance.login.side_effect = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

        with pytest.raises(email_service.EmailAuthError) as exc_info:
            email_service._send_smtp(cfg, msg)

        assert "App Password" in exc_info.value.message
        assert exc_info.value.status_code == 502


# ---------------------------------------------------------------------------
# Test 4: SMTP connection failure
# ---------------------------------------------------------------------------
def test_smtp_connection_failure(isolated_environment):
    """4. التحقق من معالجة أخطاء الاتصال، DNS، وانتهاء المهلة الزمنية."""
    cfg = email_service.EmailConfig(
        host="nonexistent.mail.server.local",
        port=587,
        username="user@example.com",
        password="secretpassword",
    )
    msg = email_service.build_verification_email(cfg, "target@example.com", "محمود", "112233")

    with patch("smtplib.SMTP") as mock_smtp_cls:
        # محاكاة تعذر الوصول إلى الخادم أو انقطاع الشبكة
        mock_smtp_cls.side_effect = socket.gaierror(11001, "getaddrinfo failed")

        with pytest.raises(email_service.EmailConnectionError) as exc_info:
            email_service._send_smtp(cfg, msg)

        assert "فشل الاتصال بخادم البريد" in exc_info.value.message
        assert exc_info.value.status_code == 502


# ---------------------------------------------------------------------------
# Test 5: Invalid recipient address
# ---------------------------------------------------------------------------
def test_invalid_recipient_address(isolated_environment):
    """5. التحقق من معالجة رفض الخادم لعنوان البريد الإلكتروني للمستلم."""
    cfg = email_service.EmailConfig(
        host="smtp.gmail.com",
        port=587,
        username="valid@gmail.com",
        password="validpassword16ch",
    )
    msg = email_service.build_verification_email(cfg, "invalid-user@domain.com", "سارة", "445566")

    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_instance = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_instance
        mock_instance.send_message.side_effect = smtplib.SMTPRecipientsRefused({"invalid-user@domain.com": (550, b"User not found")})

        with pytest.raises(email_service.EmailRecipientError) as exc_info:
            email_service._send_smtp(cfg, msg)

        assert "رفض إرسال الرسالة إلى هذا البريد" in exc_info.value.message
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Test 6: Successful email submission using a mocked SMTP server
# ---------------------------------------------------------------------------
def test_successful_email_submission_mocked_smtp(isolated_environment):
    """6. التحقق من نجاح بناء وإرسال رسالة البريد بتنسيقاتها ورؤوسها عبر SMTP موك."""
    cfg_data = {
        "host": "smtp.gmail.com",
        "port": 587,
        "username": "sender@gmail.com",
        "password": "validapppassword",
        "from_name": "Payroll System",
        "use_tls": True,
    }
    with open(isolated_environment["config"], "w", encoding="utf-8") as f:
        json.dump(cfg_data, f)

    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_instance = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_instance

        res = email_service.send_otp_email(
            to_email="employee@company.com",
            recipient_name="أحمد حسن",
            code="789012",
        )

        assert res["delivered"] is True
        assert res["simulated"] is False
        assert mock_instance.starttls.called
        assert mock_instance.login.called
        assert mock_instance.send_message.called

        # فحص رؤوس الرسالة
        sent_msg = mock_instance.send_message.call_args[0][0]
        assert "789012" in sent_msg["Subject"]
        assert sent_msg["To"] == "employee@company.com"
        assert sent_msg["From"] == "Payroll System <sender@gmail.com>"
        assert sent_msg["Date"] is not None
        assert sent_msg["Message-ID"] is not None


# ---------------------------------------------------------------------------
# Test 7: OTP expiration
# ---------------------------------------------------------------------------
def test_otp_expiration(isolated_environment, monkeypatch):
    """7. التحقق من رفض كود التفعيل عند انتهاء مهلة الـ 10 دقائق."""
    # محاكاة إرسال بريد ناجح
    monkeypatch.setattr(email_service, "send_otp_email", lambda *args, **kwargs: {"delivered": True, "simulated": False})
    # تجاوز فحص الـ DNS في الاختبار
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)

    auth.register("عمر سامي", "omar@example.com", "Password123")

    with auth._db() as c:
        row = c.execute("SELECT * FROM users WHERE email='omar@example.com'").fetchone()
        user_id = row["id"]

    # توليد كود وتعيين صلاحيته في الماضي (11 دقيقة مضت)
    code = "123456"
    past_time = time.time() - 11 * 60
    with auth._db() as c:
        c.execute("UPDATE users SET code_hash=?, code_expires=? WHERE id=?",
                  (auth._sha(f"omar@example.com:{code}"), past_time, user_id))
        c.commit()

    with pytest.raises(auth.AuthError) as exc_info:
        auth.verify_code("omar@example.com", code)

    assert "الكود انتهت صلاحيته" in exc_info.value.message
    assert exc_info.value.status == 400


# ---------------------------------------------------------------------------
# Test 8: OTP incorrect attempts limit (5 attempts)
# ---------------------------------------------------------------------------
def test_otp_incorrect_attempts(isolated_environment, monkeypatch):
    """8. التحقق من عد المحاولات الخاطئة وقفل التفعيل بعد 5 محاولات."""
    monkeypatch.setattr(email_service, "send_otp_email", lambda *args, **kwargs: {"delivered": True, "simulated": False})
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)

    auth.register("خالد علي", "khaled@example.com", "Password123")

    with auth._db() as c:
        row = c.execute("SELECT * FROM users WHERE email='khaled@example.com'").fetchone()
        user_id = row["id"]

    valid_code = "998877"
    with auth._db() as c:
        c.execute("UPDATE users SET code_hash=?, code_expires=?, code_attempts=0 WHERE id=?",
                  (auth._sha(f"khaled@example.com:{valid_code}"), time.time() + 600, user_id))
        c.commit()

    # محاولة إدخال كود خاطئ 5 مرات
    for attempt in range(1, 6):
        with pytest.raises(auth.AuthError) as exc_info:
            auth.verify_code("khaled@example.com", "000000")
        assert "الكود غلط" in exc_info.value.message

    # المحاولة بعد استنفاد الـ 5 محاولات تؤدي إلى رفض الطلب بـ 429
    with pytest.raises(auth.AuthError) as exc_info:
        auth.verify_code("khaled@example.com", "000000")
    assert "محاولات كتير غلط" in exc_info.value.message
    assert exc_info.value.status == 429


# ---------------------------------------------------------------------------
# Test 9: OTP successful verification
# ---------------------------------------------------------------------------
def test_otp_successful_verification(isolated_environment, monkeypatch):
    """9. التحقق من نجاح تفعيل الحساب عند إدخال الكود الصحيح وتحديث verified=1."""
    monkeypatch.setattr(email_service, "send_otp_email", lambda *args, **kwargs: {"delivered": True, "simulated": False})
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)

    auth.register("منى إبراهيم", "mona@example.com", "Password123")

    with auth._db() as c:
        row = c.execute("SELECT * FROM users WHERE email='mona@example.com'").fetchone()
        user_id = row["id"]

    correct_code = "554433"
    with auth._db() as c:
        c.execute("UPDATE users SET code_hash=?, code_expires=?, code_attempts=0 WHERE id=?",
                  (auth._sha(f"mona@example.com:{correct_code}"), time.time() + 600, user_id))
        c.commit()

    verified_user = auth.verify_code("mona@example.com", correct_code)
    assert verified_user["email"] == "mona@example.com"

    # التحقق من قاعدة البيانات
    with auth._db() as c:
        updated_row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        assert updated_row["verified"] == 1
        assert updated_row["code_hash"] is None
        assert updated_row["code_expires"] is None


# ---------------------------------------------------------------------------
# Test 10: OTP reuse prevention
# ---------------------------------------------------------------------------
def test_otp_reuse_prevention(isolated_environment, monkeypatch):
    """10. التحقق من منع إعادة استخدام كود التفعيل بعد تفعيل الحساب."""
    monkeypatch.setattr(email_service, "send_otp_email", lambda *args, **kwargs: {"delivered": True, "simulated": False})
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)

    auth.register("ياسر كمال", "yasser@example.com", "Password123")

    with auth._db() as c:
        row = c.execute("SELECT * FROM users WHERE email='yasser@example.com'").fetchone()
        user_id = row["id"]

    code = "332211"
    with auth._db() as c:
        c.execute("UPDATE users SET code_hash=?, code_expires=? WHERE id=?",
                  (auth._sha(f"yasser@example.com:{code}"), time.time() + 600, user_id))
        c.commit()

    # التفعيل للمرة الأولى بنجاح
    auth.verify_code("yasser@example.com", code)

    # محاولة إعادة التفعيل بنفس الكود
    with pytest.raises(auth.AuthError) as exc_info:
        auth.verify_code("yasser@example.com", code)

    # يجب رفض التكرار إما لأن الكود تم إتلافه أو لأن الحساب مفعل بالفعل
    assert exc_info.value.status in (400, 409)


# ---------------------------------------------------------------------------
# Test 11: Real Gmail SMTP integration (distinguished from mocked tests)
# ---------------------------------------------------------------------------
def test_real_gmail_smtp_if_configured():
    """
    11. فحص الاتصال الحقيقي بخادم Gmail في حال توفر بيانات حقيقية في email_config.json.
    إذا لم تتوفر بيانات حقيقية، يتم تخطي الاختبار مع تسجيل السبب بوضوح دون ادعاء وهمي.
    """
    real_config_path = os.path.join(auth.DATA_DIR, "email_config.json")
    if not os.path.exists(real_config_path):
        pytest.skip("[REAL_SMTP_TEST_SKIPPED] ملف email_config.json الحقيقي غير موجود لاختبار SMTP الفعلي.")

    try:
        with open(real_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        pytest.skip("[REAL_SMTP_TEST_SKIPPED] ملف email_config.json غير صالح لاختبار SMTP الفعلي.")

    if not data.get("username") or not data.get("password") or "YOUR" in str(data.get("password")).upper():
        pytest.skip("[REAL_SMTP_TEST_SKIPPED] بيانات SMTP الحقيقية لم تضبط بعد (بيانات افتراضية/فارغة).")

    # في حال وجود بيانات حقيقية، يتم اختبار الاتصال الفعلي وإرسال بريد حقيقي للمرسل نفسه
    cfg = email_service.EmailConfig(
        host=data.get("host", "smtp.gmail.com"),
        port=int(data.get("port", 587)),
        username=data["username"],
        password=data["password"],
        from_name="اختبار نظام الرواتب",
    )
    test_msg = email_service.build_verification_email(
        config=cfg,
        to_email=data["username"],
        recipient_name="مختبر النظام",
        code="999999",
    )
    email_service._send_smtp(cfg, test_msg)

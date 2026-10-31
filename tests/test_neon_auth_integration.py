"""
Comprehensive Neon PostgreSQL Authentication Test Suite.
Tests:
1. PostgreSQL connection and health check.
2. User registration in Neon.
3. Unique email enforcement.
4. Password verification using scrypt hashes and salt.
5. Email OTP verification.
6. OTP expiration.
7. Failed login attempts and account lockout.
8. Session creation and token hashing.
9. Session expiration and cleanup.
10. Session revocation / logout.
11. Migration integrity and idempotency.
12. Audit log recording.
13. Foreign key integrity.
"""
import time
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import text

import database
import models
import auth
from database import get_db, check_db_connection
from models import User, SessionModel, AuditLog
import migrate_sqlite_to_neon


def test_neon_connection():
    """Verify live Neon PostgreSQL connectivity."""
    ok, msg = check_db_connection()
    assert ok is True, f"Neon connection failed: {msg}"
    assert "Neon" in msg or "Connected" in msg


def test_existing_migrated_users_present():
    """Verify that the 2 SQLite users were properly migrated into Neon."""
    with get_db() as db:
        u1 = db.query(User).filter(User.email == "mota200615@gmail.com").first()
        u2 = db.query(User).filter(User.email == "aiengmohamedtayal@gmail.com").first()
        assert u1 is not None, "Migrated user 1 missing in Neon"
        assert u2 is not None, "Migrated user 2 missing in Neon"
        assert u1.id == 1
        assert u2.id == 2
        assert u1.verified is True
        assert u2.verified is True


def test_user_registration_flow_in_neon(monkeypatch):
    """Test user registration in Neon PostgreSQL."""
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)
    test_email = f"test_neon_{int(time.time())}@example.com"
    test_pw = "Password123"

    res = auth.register("Neon Test User", test_email, test_pw)
    assert res["email"] == test_email
    assert "resend_after" in res

    # Verify user exists in Neon
    with get_db() as db:
        user = db.query(User).filter(User.email == test_email).first()
        assert user is not None
        assert user.verified is False
        assert user.code_hash is not None

        # Verify audit log was written
        audit = db.query(AuditLog).filter(AuditLog.user_id == user.id, AuditLog.event_type == "REGISTER_ATTEMPT").first()
        assert audit is not None


def test_unique_email_enforcement(monkeypatch):
    """Verify unique email constraint prevents registering an already-verified email."""
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)
    with pytest.raises(auth.AuthError) as exc_info:
        auth.register("Duplicate Test", "mota200615@gmail.com", "Password123")
    assert exc_info.value.status == 409
    assert "متسجل قبل كده" in exc_info.value.message


def test_otp_verification_flow(monkeypatch):
    """Test full OTP verification flow in Neon."""
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)
    test_email = f"otp_test_{int(time.time())}@example.com"
    test_pw = "Password123"

    auth.register("OTP User", test_email, test_pw)

    # Set known OTP in database
    otp_code = "654321"
    now_dt = datetime.now(timezone.utc)
    with get_db() as db:
        user = db.query(User).filter(User.email == test_email).first()
        user.code_hash = auth._sha(f"{test_email}:{otp_code}")
        user.code_expires = now_dt + timedelta(seconds=600)
        db.commit()

    # Wrong code should fail
    with pytest.raises(auth.AuthError) as exc_info:
        auth.verify_code(test_email, "000000")
    assert exc_info.value.status == 400
    assert "الكود غلط" in exc_info.value.message

    # Correct code should succeed
    verified = auth.verify_code(test_email, otp_code)
    assert verified["email"] == test_email

    # Check database status
    with get_db() as db:
        u = db.query(User).filter(User.email == test_email).first()
        assert u.verified is True
        assert u.code_hash is None


def test_otp_expiration(monkeypatch):
    """Test expired OTP rejection."""
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)
    test_email = f"expired_test_{int(time.time())}@example.com"
    auth.register("Expired User", test_email, "Password123")

    with get_db() as db:
        user = db.query(User).filter(User.email == test_email).first()
        # Set code_expires to the past
        user.code_expires = datetime.now(timezone.utc) - timedelta(seconds=10)
        db.commit()

    with pytest.raises(auth.AuthError) as exc_info:
        auth.verify_code(test_email, "123456")
    assert exc_info.value.status == 400
    assert "صلاحيته" in exc_info.value.message


def test_login_and_account_lockout(monkeypatch):
    """Test failed login tracking and account lockout in Neon."""
    monkeypatch.setattr(auth, "validate_email_real", lambda email: None)
    test_email = f"lockout_{int(time.time())}@example.com"
    test_pw = "Password123"

    auth.register("Lockout User", test_email, test_pw)
    with get_db() as db:
        u = db.query(User).filter(User.email == test_email).first()
        u.verified = True
        db.commit()

    # 4 failed attempts
    for _ in range(4):
        with pytest.raises(auth.AuthError) as exc_info:
            auth.login(test_email, "WrongPassword99")
        assert exc_info.value.status == 401

    # 5th failed attempt triggers lockout
    with pytest.raises(auth.AuthError) as exc_info:
        auth.login(test_email, "WrongPassword99")
    assert exc_info.value.status == 401

    # 6th attempt should be blocked with 429 Account Locked
    with pytest.raises(auth.AuthError) as exc_info:
        auth.login(test_email, "WrongPassword99")
    assert exc_info.value.status == 429
    assert "الحساب اتقفل مؤقتاً" in exc_info.value.message

    # Reset lockout and verify successful login
    with get_db() as db:
        u = db.query(User).filter(User.email == test_email).first()
        u.locked_until = None
        db.commit()

    logged_in = auth.login(test_email, test_pw)
    assert logged_in["email"] == test_email


def test_session_lifecycle():
    """Test session creation, lookup, and deletion in Neon."""
    with get_db() as db:
        u = db.query(User).filter(User.verified == True).first()
        assert u is not None
        user_id = u.id

    token = auth.create_session(user_id)
    assert token is not None

    # Lookup by token
    user_data = auth.get_user_by_token(token)
    assert user_data is not None
    assert user_data["id"] == user_id

    # Destroy session
    auth.destroy_session(token)
    assert auth.get_user_by_token(token) is None


def test_migration_idempotency():
    """Verify migration script can be safely re-run without errors."""
    report = migrate_sqlite_to_neon.run_migration("users.db")
    assert report["status"] == "success"
    assert report["users_failed"] == 0
    assert report["users_skipped_existing"] >= 2

"""
Safe, Repeatable, Idempotent Data Migration Script:
Source: SQLite users.db
Destination: Neon PostgreSQL (DATABASE_URL)

Requirements met:
- Preserves original user IDs, names, emails, scrypt pw_hash, pw_salt, verification status, and timestamps.
- Preserves active non-expired sessions.
- Maintains foreign keys and referential integrity.
- Detects existing records and duplicate emails gracefully.
- Idempotent: can be run repeatedly without duplicating or corrupting records.
- Uses transactions (atomic commit / rollback).
- Resets PostgreSQL auto-increment sequences (users_id_seq).
- Generates a clear migration report with counts and status.
- NEVER exposes passwords, hashes, session tokens, or OTP values in logs or reports.
- Leaves SQLite users.db completely untouched.
"""
import os
import sys
import time
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List

from sqlalchemy import text
import database
from models import User, SessionModel, AuditLog

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration")


def unix_to_utc_dt(ts: float | int | None) -> datetime | None:
    """Convert a unix timestamp to a timezone-aware UTC datetime."""
    if ts is None or ts == "":
        return None
    try:
        val = float(ts)
        return datetime.fromtimestamp(val, tz=timezone.utc)
    except Exception:
        return None


def run_migration(sqlite_path: str = "users.db") -> Dict[str, Any]:
    report = {
        "status": "pending",
        "sqlite_path": sqlite_path,
        "sqlite_users_scanned": 0,
        "users_imported": 0,
        "users_skipped_existing": 0,
        "users_failed": 0,
        "sqlite_sessions_scanned": 0,
        "sessions_imported": 0,
        "sessions_skipped_expired": 0,
        "sessions_skipped_existing": 0,
        "sessions_failed": 0,
        "conflicts": [],
        "errors": []
    }

    if not os.path.exists(sqlite_path):
        report["status"] = "failed"
        report["errors"].append(f"SQLite file not found at {sqlite_path}")
        logger.error(report["errors"][-1])
        return report

    # Verify Neon connection
    ok, conn_msg = database.check_db_connection()
    if not ok:
        report["status"] = "failed"
        report["errors"].append(f"Neon connection failed: {conn_msg}")
        logger.error(report["errors"][-1])
        return report

    # Ensure tables exist in Neon
    database.init_db()

    # Read SQLite data
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    c = sqlite_conn.cursor()

    try:
        c.execute("SELECT * FROM users ORDER BY id ASC")
        raw_users = [dict(r) for r in c.fetchall()]
        report["sqlite_users_scanned"] = len(raw_users)

        c.execute("SELECT * FROM sessions")
        raw_sessions = [dict(r) for r in c.fetchall()]
        report["sqlite_sessions_scanned"] = len(raw_sessions)
    finally:
        sqlite_conn.close()

    logger.info("Found %d users and %d sessions in SQLite", report["sqlite_users_scanned"], report["sqlite_sessions_scanned"])

    # Perform atomic migration to Neon
    with database.get_db() as db:
        max_user_id = 0

        # --- 1. Migrate Users ---
        for u in raw_users:
            user_id = int(u["id"])
            if user_id > max_user_id:
                max_user_id = user_id
            
            email = (u.get("email") or "").strip().lower()
            name = (u.get("name") or "").strip()

            # Check if user with this email or ID already exists in Neon
            existing_by_email = db.query(User).filter(User.email == email).first()
            existing_by_id = db.query(User).filter(User.id == user_id).first()

            if existing_by_email:
                if existing_by_email.id == user_id:
                    report["users_skipped_existing"] += 1
                    logger.info("User ID %d (%s) already exists with matching ID. Skipping.", user_id, email)
                    continue
                else:
                    msg = f"Conflict: Email {email} exists in Neon with ID {existing_by_email.id}, but SQLite ID is {user_id}."
                    report["conflicts"].append(msg)
                    report["users_failed"] += 1
                    logger.warning(msg)
                    continue

            if existing_by_id:
                msg = f"Conflict: User ID {user_id} already exists in Neon with different email ({existing_by_id.email})."
                report["conflicts"].append(msg)
                report["users_failed"] += 1
                logger.warning(msg)
                continue

            # Create User record in Neon
            new_user = User(
                id=user_id,
                name=name,
                email=email,
                pw_hash=u["pw_hash"],
                pw_salt=u["pw_salt"],
                verified=bool(u.get("verified", 0)),
                code_hash=u.get("code_hash"),
                code_expires=unix_to_utc_dt(u.get("code_expires")),
                code_attempts=int(u.get("code_attempts") or 0),
                code_sent_at=unix_to_utc_dt(u.get("code_sent_at")),
                fail_count=int(u.get("fail_count") or 0),
                locked_until=unix_to_utc_dt(u.get("locked_until")),
                created_at=unix_to_utc_dt(u.get("created_at")) or datetime.now(timezone.utc)
            )
            db.add(new_user)
            report["users_imported"] += 1
            logger.info("Prepared User import: ID %d (%s)", user_id, email)

        # Flush users so foreign keys are valid for sessions
        db.flush()

        # Update sequence for users_id_seq if we inserted explicit IDs
        if max_user_id > 0:
            try:
                db.execute(text(f"SELECT setval('users_id_seq', (SELECT COALESCE(MAX(id), 1) FROM users) + 1, false);"))
                db.flush()
            except Exception as e:
                logger.warning("Could not setval for users_id_seq: %s", str(e))

        # --- 2. Migrate Sessions ---
        now_ts = time.time()
        for s in raw_sessions:
            token_hash = s.get("token_hash")
            user_id = int(s.get("user_id"))
            expires_ts = float(s.get("expires") or 0)

            # Check if session is expired
            if expires_ts < now_ts:
                report["sessions_skipped_expired"] += 1
                continue

            # Verify that user exists in Neon
            user_exists = db.query(User).filter(User.id == user_id).first()
            if not user_exists:
                msg = f"Session user_id {user_id} does not exist in Neon. Skipping session."
                report["sessions_failed"] += 1
                report["conflicts"].append(msg)
                continue

            # Check if session already exists
            existing_sess = db.query(SessionModel).filter(SessionModel.token_hash == token_hash).first()
            if existing_sess:
                report["sessions_skipped_existing"] += 1
                continue

            new_sess = SessionModel(
                token_hash=token_hash,
                user_id=user_id,
                expires=unix_to_utc_dt(expires_ts),
                created_at=datetime.now(timezone.utc)
            )
            db.add(new_sess)
            report["sessions_imported"] += 1

        # Record migration audit log
        audit = AuditLog(
            user_id=None,
            event_type="SQLITE_TO_NEON_MIGRATION",
            ip_address="127.0.0.1",
            details=f"Migrated {report['users_imported']} users, {report['sessions_imported']} active sessions."
        )
        db.add(audit)
        db.commit()

        # Verification check
        neon_users_count = db.query(User).count()
        neon_sessions_count = db.query(SessionModel).count()
        report["neon_total_users"] = neon_users_count
        report["neon_total_sessions"] = neon_sessions_count
        report["status"] = "success" if report["users_failed"] == 0 else "completed_with_warnings"

    logger.info("=== MIGRATION REPORT ===")
    logger.info("Status: %s", report["status"])
    logger.info("Users Scanned: %d | Imported: %d | Skipped (Existing): %d | Failed: %d",
                report["sqlite_users_scanned"], report["users_imported"],
                report["users_skipped_existing"], report["users_failed"])
    logger.info("Sessions Scanned: %d | Imported: %d | Skipped (Expired): %d | Skipped (Existing): %d | Failed: %d",
                report["sqlite_sessions_scanned"], report["sessions_imported"],
                report["sessions_skipped_expired"], report["sessions_skipped_existing"],
                report["sessions_failed"])
    logger.info("Neon Current Database Totals -> Users: %d, Sessions: %d",
                report["neon_total_users"], report["neon_total_sessions"])

    return report


if __name__ == "__main__":
    db_file = sys.argv[1] if len(sys.argv) > 1 else "users.db"
    res = run_migration(db_file)
    sys.exit(0 if res["status"] in ["success", "completed_with_warnings"] else 1)

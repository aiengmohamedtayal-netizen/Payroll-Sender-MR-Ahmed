"""
نظام الحسابات والمصادقة (Authentication & Accounts) - مدعوم بقاعدة بيانات Neon PostgreSQL السحابية:
- الحسابات والبيانات الأمنية مخزنة بشكل دائم ومركزي في Neon PostgreSQL عبر SQLAlchemy و psycopg.
- كلمات السر مشفرة بتقنية scrypt + salt الفريدة لكل حساب.
- أكواد التحقق (OTP) مشفرة بـ SHA-256 مع انتهاء صلاحية خلال 10 دقائق وحد أقصى 5 محاولات.
- حماية ضد هجمات التخمين (Brute-force) بقفل الحساب تلقائياً لمدة 10 دقائق بعد 5 محاولات خاطئة.
- جلسات تسجيل الدخول تدوم 7 أيام وتخزن في قاعدة البيانات مشفرة بـ SHA-256.
- تسجيل الأحداث الأمنية في جدول audit_logs دون تخزين أي كلمات سر أو أكواد.
"""
import hashlib
import hmac
import os
import re
import secrets
import socket
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Dict, Any
from contextlib import contextmanager

from sqlalchemy import or_, and_, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

import email_service
import database
from database import get_db, check_db_connection, DatabaseConnectionError
from models import User, SessionModel, AuditLog, UserSettings

try:
    import dns.resolver  # dnspython (اختياري - لفحص إن الدومين بيستقبل إيميلات)
except Exception:  # pragma: no cover
    dns = None

FROZEN = getattr(sys, "frozen", False)
DATA_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
# Legacy compatibility for tests
DB_PATH = os.path.join(DATA_DIR, "users.db")

SESSION_COOKIE = "ps_session"
SESSION_DAYS = 7
CODE_TTL_SEC = 10 * 60
RESEND_COOLDOWN_SEC = 60
MAX_CODE_ATTEMPTS = 5
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_SEC = 10 * 60

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+\.)+[A-Za-z]{2,}$")
DISPOSABLE = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com",
    "temp-mail.org", "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com",
}

_lock = threading.Lock()


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


# ---------------------------------------------------------------------------
# قاعدة البيانات
# ---------------------------------------------------------------------------
class _ResultAdapter:
    def __init__(self, result):
        self._result = result

    def fetchone(self):
        row = self._result.mappings().fetchone()
        return dict(row) if row else None

    def fetchall(self):
        rows = self._result.mappings().fetchall()
        return [dict(r) for r in rows]


class _DbAdapter:
    def __init__(self, session):
        self._session = session

    def execute(self, sql_str: str, params: tuple = ()):
        if "?" in sql_str and params:
            converted_params = []
            for p in params:
                if isinstance(p, (int, float)) and p > 1000000000:
                    converted_params.append(datetime.fromtimestamp(p, tz=timezone.utc))
                else:
                    converted_params.append(p)
            named_params = {}
            parts = sql_str.split("?")
            new_sql = ""
            for i, part in enumerate(parts[:-1]):
                param_name = f"p_{i}"
                named_params[param_name] = converted_params[i]
                new_sql += part + f":{param_name}"
            new_sql += parts[-1]
            res = self._session.execute(text(new_sql), named_params)
        else:
            res = self._session.execute(text(sql_str))
        return _ResultAdapter(res)

    def commit(self):
        self._session.commit()


@contextmanager
def _db():
    """Adapter for legacy tests inspecting database directly."""
    with get_db() as session:
        yield _DbAdapter(session)


def init_db():
    """تهيئة جداول قاعدة البيانات في Neon PostgreSQL."""
    try:
        database.init_db()
    except Exception as e:
        raise AuthError(f"فشل الاتصال بقاعدة البيانات: {str(e)}", status=503)


# ---------------------------------------------------------------------------
# أدوات التشفير والفحص
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    if len(name) < 2 or len(name) > 60:
        raise AuthError("اكتب اسمك (من حرفين لـ 60 حرف)")
    return name


def validate_password(password: str):
    if len(password or "") < 8:
        raise AuthError("كلمة السر لازم تكون 8 حروف أو أرقام على الأقل")
    if len(password) > 128:
        raise AuthError("كلمة السر طويلة قوي")
    if not re.search(r"[A-Za-z\u0621-\u064A]", password) or not re.search(r"\d", password):
        raise AuthError("كلمة السر لازم فيها حروف وأرقام مع بعض")


def validate_email_real(email: str):
    """التحقق من صحة البريد الإلكتروني ووجود الدومين وسجلاته."""
    if not EMAIL_RE.match(email) or len(email) > 120:
        raise AuthError("شكل الإيميل مش صحيح")
    domain = email.rsplit("@", 1)[1]
    if domain in DISPOSABLE:
        raise AuthError("الإيميلات المؤقتة مش مسموحة، استخدم إيميلك الشخصي أو بتاع الشغل")

    if dns is not None:
        try:
            dns.resolver.resolve(domain, "MX", lifetime=5)
            return
        except dns.resolver.NXDOMAIN:
            raise AuthError("الدومين بتاع الإيميل ده مش موجود")
        except dns.resolver.NoAnswer:
            try:
                dns.resolver.resolve(domain, "A", lifetime=5)
                return
            except Exception:
                raise AuthError("الدومين ده مبيستقبلش إيميلات")
        except Exception:
            return  # مشكلة شبكة مؤقتة: كود التفعيل هو الفيصل
    else:
        try:
            socket.getaddrinfo(domain, None)
        except socket.gaierror:
            raise AuthError("الدومين بتاع الإيميل ده مش موجود")


# ---------------------------------------------------------------------------
# تفويض إرسال كود التفعيل لخدمة البريد المستقلة (email_service)
# ---------------------------------------------------------------------------
def _dispatch_code_email(to_email: str, name: str, code: str) -> dict:
    try:
        return email_service.send_otp_email(to_email=to_email, recipient_name=name, code=code)
    except email_service.EmailConfigError as e:
        raise AuthError(e.message, status=503)
    except email_service.EmailAuthError as e:
        raise AuthError(e.message, status=502)
    except email_service.EmailRecipientError as e:
        raise AuthError(e.message, status=400)
    except email_service.EmailConnectionError as e:
        raise AuthError(e.message, status=502)
    except email_service.EmailDeliveryError as e:
        raise AuthError(e.message, status=502)
    except Exception:
        raise AuthError("تعذر إرسال رسالة التفعيل حالياً. راجع سجل الخادم للمزيد من التفاصيل.", status=502)


def _issue_code_db(db, user: User, email: str, name: str) -> dict:
    code = f"{secrets.randbelow(10 ** 6):06d}"
    now_dt = datetime.now(timezone.utc)
    
    user.code_hash = _sha(f"{email}:{code}")
    user.code_expires = now_dt + timedelta(seconds=CODE_TTL_SEC)
    user.code_attempts = 0
    user.code_sent_at = now_dt
    db.commit()

    try:
        delivery_result = _dispatch_code_email(email, name, code)
        return delivery_result
    except AuthError:
        # إذا فشل إرسال البريد لا نترك كوداً معلقاً
        user.code_hash = None
        user.code_sent_at = None
        db.commit()
        raise


def register(name: str, email: str, password: str) -> dict:
    name = validate_name(name)
    email = normalize_email(email)
    validate_password(password)
    validate_email_real(email)

    try:
        with _lock, get_db() as db:
            user = db.query(User).filter(User.email == email).first()
            if user and user.verified:
                raise AuthError("الإيميل ده متسجل قبل كده، سجّل دخول", status=409)

            salt = secrets.token_bytes(16)
            pw_hash = _hash_password(password, salt)
            now_dt = datetime.now(timezone.utc)

            if user:
                # التحقق من مهلة إعادة الإرسال
                if user.code_sent_at:
                    elapsed = (now_dt - user.code_sent_at).total_seconds()
                    wait = RESEND_COOLDOWN_SEC - elapsed
                    if wait > 0:
                        raise AuthError(f"استنى {int(wait)} ثانية قبل ما تطلب كود جديد", status=429, retry_after=int(wait))
                
                user.name = name
                user.pw_hash = pw_hash
                user.pw_salt = salt.hex()
            else:
                user = User(
                    name=name,
                    email=email,
                    pw_hash=pw_hash,
                    pw_salt=salt.hex(),
                    verified=False,
                    created_at=now_dt
                )
                db.add(user)
            
            db.commit()
            db.refresh(user)

            # تسجيل في سجل الأمان
            audit = AuditLog(
                user_id=user.id,
                event_type="REGISTER_ATTEMPT",
                details=f"User {email} initiated registration"
            )
            db.add(audit)
            db.commit()

            delivery = _issue_code_db(db, user, email, name)

        return {
            "email": email,
            "resend_after": RESEND_COOLDOWN_SEC,
            "simulated": delivery.get("simulated", False),
            "dev_code": delivery.get("code") if delivery.get("simulated") else None,
            "message": delivery.get("message", ""),
        }
    except DatabaseConnectionError:
        raise AuthError("تعذر الاتصال بقاعدة البيانات السحابية (Neon). يرجى التحقق من اتصال الإنترنت.", status=503)


def resend_code(email: str) -> dict:
    email = normalize_email(email)
    try:
        with _lock, get_db() as db:
            user = db.query(User).filter(User.email == email, User.verified == False).first()
            if not user:
                raise AuthError("مفيش تسجيل معلّق بالإيميل ده", status=404)
            
            now_dt = datetime.now(timezone.utc)
            if user.code_sent_at:
                elapsed = (now_dt - user.code_sent_at).total_seconds()
                wait = RESEND_COOLDOWN_SEC - elapsed
                if wait > 0:
                    raise AuthError(f"استنى {int(wait)} ثانية قبل ما تطلب كود جديد", status=429, retry_after=int(wait))
            
            delivery = _issue_code_db(db, user, email, user.name)

            audit = AuditLog(
                user_id=user.id,
                event_type="OTP_RESEND",
                details=f"OTP resent to {email}"
            )
            db.add(audit)
            db.commit()

        return {
            "resend_after": RESEND_COOLDOWN_SEC,
            "simulated": delivery.get("simulated", False),
            "dev_code": delivery.get("code") if delivery.get("simulated") else None,
            "message": delivery.get("message", ""),
        }
    except DatabaseConnectionError:
        raise AuthError("تعذر الاتصال بقاعدة البيانات السحابية (Neon).", status=503)


def verify_code(email: str, code: str) -> dict:
    email = normalize_email(email)
    code = re.sub(r"\D", "", code or "")
    
    try:
        with _lock, get_db() as db:
            user = db.query(User).filter(User.email == email).first()
            if not user or not user.code_hash:
                raise AuthError("الكود منتهي أو مفيش كود مطلوب، اطلب كود جديد", status=400)
            if user.verified:
                raise AuthError("الحساب ده متفعّل بالفعل، سجّل دخول", status=409)
            
            now_dt = datetime.now(timezone.utc)
            if user.code_expires and now_dt > user.code_expires:
                raise AuthError("الكود انتهت صلاحيته، اطلب كود جديد", status=400)
            if user.code_attempts >= MAX_CODE_ATTEMPTS:
                raise AuthError("محاولات كتير غلط، اطلب كود جديد", status=429)

            if not hmac.compare_digest(user.code_hash, _sha(f"{email}:{code}")):
                attempts = user.code_attempts
                user.code_attempts += 1
                db.commit()
                left = MAX_CODE_ATTEMPTS - attempts - 1
                raise AuthError(f"الكود غلط. باقي {max(left, 0)} محاولات", status=400)

            user.verified = True
            user.code_hash = None
            user.code_expires = None
            user.code_attempts = 0

            audit = AuditLog(
                user_id=user.id,
                event_type="ACCOUNT_VERIFIED",
                details=f"User {email} successfully verified OTP"
            )
            db.add(audit)
            db.commit()
            return {"id": user.id, "name": user.name, "email": email}
    except DatabaseConnectionError:
        raise AuthError("تعذر الاتصال بقاعدة البيانات السحابية (Neon).", status=503)


def login(email: str, password: str) -> dict:
    email = normalize_email(email)
    generic = AuthError("الإيميل أو كلمة السر غير صحيحة", status=401)
    
    try:
        with _lock, get_db() as db:
            user = db.query(User).filter(User.email == email).first()
            if not user:
                _hash_password(password or "", b"0" * 16)  # حماية ضد هجمات التوقيت
                raise generic

            now_dt = datetime.now(timezone.utc)
            if user.locked_until and now_dt < user.locked_until:
                mins = int((user.locked_until - now_dt).total_seconds() // 60) + 1
                raise AuthError(f"الحساب اتقفل مؤقتاً بسبب محاولات غلط. جرب بعد {mins} دقيقة", status=429)

            ok = hmac.compare_digest(user.pw_hash, _hash_password(password or "", bytes.fromhex(user.pw_salt)))
            if not ok:
                fails = user.fail_count + 1
                if fails >= MAX_LOGIN_FAILS:
                    user.fail_count = 0
                    user.locked_until = now_dt + timedelta(seconds=LOGIN_LOCK_SEC)
                    audit = AuditLog(
                        user_id=user.id,
                        event_type="ACCOUNT_LOCKED",
                        details=f"Account locked after {fails} failed login attempts"
                    )
                else:
                    user.fail_count = fails
                    user.locked_until = None
                    audit = AuditLog(
                        user_id=user.id,
                        event_type="LOGIN_FAILED",
                        details=f"Failed login attempt {fails}/{MAX_LOGIN_FAILS}"
                    )
                db.add(audit)
                db.commit()
                raise generic

            if not user.verified:
                raise AuthError("لازم تفعّل الإيميل الأول", status=403, needs_verification=True, email=email)

            user.fail_count = 0
            user.locked_until = None
            audit = AuditLog(
                user_id=user.id,
                event_type="LOGIN_SUCCESS",
                details=f"User {email} logged in successfully"
            )
            db.add(audit)
            db.commit()
            return {"id": user.id, "name": user.name, "email": email}
    except DatabaseConnectionError:
        raise AuthError("تعذر الاتصال بقاعدة البيانات السحابية (Neon).", status=503)


# ---------------------------------------------------------------------------
# الجلسات
# ---------------------------------------------------------------------------
def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now_dt = datetime.now(timezone.utc)
    expires_dt = now_dt + timedelta(days=SESSION_DAYS)
    
    with _lock, get_db() as db:
        # حذف الجلسات المنتهية
        db.query(SessionModel).filter(SessionModel.expires < now_dt).delete()
        new_sess = SessionModel(
            token_hash=_sha(token),
            user_id=user_id,
            expires=expires_dt,
            created_at=now_dt
        )
        db.add(new_sess)
        db.commit()
    return token


def get_user_by_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    try:
        now_dt = datetime.now(timezone.utc)
        h = _sha(token)
        with _lock, get_db() as db:
            row = (
                db.query(User.id, User.name, User.email)
                .join(SessionModel, SessionModel.user_id == User.id)
                .filter(SessionModel.token_hash == h)
                .filter(SessionModel.expires > now_dt)
                .filter(User.verified == True)
                .first()
            )
            if row:
                return {"id": row.id, "name": row.name, "email": row.email}
            return None
    except Exception:
        return None


def destroy_session(token: Optional[str]):
    if not token:
        return
    try:
        h = _sha(token)
        with _lock, get_db() as db:
            db.query(SessionModel).filter(SessionModel.token_hash == h).delete()
            db.commit()
    except Exception:
        pass


def email_configured() -> bool:
    """التحقق مما إذا كانت إعدادات البريد مهيأة وجاهزة للاستخدام."""
    return email_service.is_configured()


def get_email_status_detail() -> Tuple[bool, str, str]:
    """إرجاع تفاصيل حالة إعدادات البريد (جاهز، كود الحالة، رسالة التوضيح)."""
    cfg, status, msg = email_service.load_email_config()
    is_ready = (cfg is not None and status == email_service.ConfigStatus.VALID) or email_service.is_dev_mode()
    return is_ready, status.value, msg


# Initialize tables upon module load
try:
    init_db()
except Exception as _e:
    pass

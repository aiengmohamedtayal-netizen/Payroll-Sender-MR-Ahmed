"""
خدمة إرسال البريد الإلكتروني المخصصة لنظام الرواتب (Email Delivery Service).

المميزات:
- فصل كامل لمنطق البريد الإلكتروني عن مسارات المصادقة (Separation of Concerns).
- دعم كامل لـ Gmail SMTP عبر كلمات مرور التطبيقات (App Passwords) المكونة من 16 حرفاً.
- معالجة تلقائية للمسافات الزائدة في كلمات مرور تطبيقات Google.
- دعم التشفير عبر المنفذ 587 (STARTTLS) والمنفذ 465 (Implicit SSL).
- قوالب HTML و Plain-Text احترافية تدعم اللغة العربية (RTL) وترميز UTF-8.
- تصنيف دقيق للأخطاء (بيانات غير صالحة، فشل المصادقة، فشل الاتصال، رفض المستلم).
- منع طباعة كلمات المرور أو الأكواد السرية في سجلات الإنتاج.
- توافق كامل مع بيئة Windows والملف التنفيذي المعزول (PyInstaller EXE).
"""

import email.utils
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from enum import Enum
import json
import logging
import os
import re
import smtplib
import socket
import ssl
import sys
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("payroll.email")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [EmailService] [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

# تحديد المسارات المتوافقة مع وضع التطوير والملف التنفيذي (EXE)
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG_FILENAME = "email_config.json"


class ConfigStatus(str, Enum):
    VALID = "VALID"
    MISSING_FILE = "MISSING_FILE"
    INVALID_JSON = "INVALID_JSON"
    MISSING_FIELDS = "MISSING_FIELDS"
    PLACEHOLDER_CREDENTIALS = "PLACEHOLDER_CREDENTIALS"
    INVALID_PORT = "INVALID_PORT"


class EmailServiceError(Exception):
    """الاستثناء الأساسي لخدمة البريد الإلكتروني."""
    def __init__(self, message: str, status_code: int = 502, technical_detail: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.technical_detail = technical_detail


class EmailConfigError(EmailServiceError):
    """خطأ في إعدادات البريد أو عدم وجودها."""
    def __init__(self, message: str, technical_detail: str = ""):
        super().__init__(message, status_code=503, technical_detail=technical_detail)


class EmailAuthError(EmailServiceError):
    """فشل المصادقة مع خادم SMTP (بيانات الدخول أو App Password خاطئة)."""
    def __init__(self, message: str, technical_detail: str = ""):
        super().__init__(message, status_code=502, technical_detail=technical_detail)


class EmailConnectionError(EmailServiceError):
    """فشل الاتصال بخادم SMTP (انقطاع الاتصال، خطأ DNS، مهلة زمنية، أو فشل SSL)."""
    def __init__(self, message: str, technical_detail: str = ""):
        super().__init__(message, status_code=502, technical_detail=technical_detail)


class EmailRecipientError(EmailServiceError):
    """رفض عنوان البريد الإلكتروني للمستلم من قِبل الخادم."""
    def __init__(self, message: str, technical_detail: str = ""):
        super().__init__(message, status_code=400, technical_detail=technical_detail)


class EmailDeliveryError(EmailServiceError):
    """فشل إرسال الرسالة بعد الاتصال."""
    def __init__(self, message: str, technical_detail: str = ""):
        super().__init__(message, status_code=502, technical_detail=technical_detail)


def get_config_path() -> str:
    """إرجاع المسار المطلق لملف إعدادات البريد."""
    custom_path = os.environ.get("EMAIL_CONFIG_PATH")
    if custom_path:
        return os.path.abspath(custom_path)
    data_path = os.path.join(DATA_DIR, DEFAULT_CONFIG_FILENAME)
    if os.path.exists(data_path):
        return data_path
    base_path = os.path.join(BASE_DIR, DEFAULT_CONFIG_FILENAME)
    if os.path.exists(base_path):
        return base_path
    cwd_path = os.path.join(os.getcwd(), DEFAULT_CONFIG_FILENAME)
    if os.path.exists(cwd_path):
        return cwd_path
    return data_path


def is_production() -> bool:
    """التحقق مما إذا كان التطبيق يعمل في بيئة الإنتاج."""
    return os.environ.get("APP_ENV", "development").strip().lower() in {"production", "prod"}


def is_dev_mode() -> bool:
    """
    التحقق من تفعيل وضع التطوير المحلي.
    لا يُسمح بتفعيل وضع التطوير في بيئة الإنتاج مطلقاً لضمان الأمان.
    """
    if is_production():
        return False
    return os.environ.get("AUTH_DEV_MODE", "0").strip().lower() in {"1", "true", "yes"}


def sanitize_email_for_log(email_str: str) -> str:
    """إخفاء جزء من الإيميل عند التسجيل في السجلات لحماية الخصوصية."""
    if not email_str or "@" not in email_str:
        return "***"
    local, domain = email_str.rsplit("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[0] + ("*" * (len(local) - 2)) + local[-1]
    return f"{masked_local}@{domain}"


class EmailConfig:
    """كائن يحمل ويدقق إعدادات SMTP."""
    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str = "",
        password: str = "",
        from_name: str = "مرسل رواتب واتساب",
        from_address: str = "",
        use_tls: Optional[bool] = None,
        use_ssl: Optional[bool] = None,
        timeout: int = 20,
    ):
        self.host = (host or "").strip()
        self.port = int(port)
        self.username = (username or "").strip()
        # إزالة المسافات من كلمة المرور (خاصة بأكواد Google App Passwords المكونة من 16 حرفاً مقسمة 4x4)
        raw_pw = str(password or "").strip()
        if " " in raw_pw and ("gmail.com" in self.host.lower() or len(raw_pw.replace(" ", "")) == 16):
            self.password = raw_pw.replace(" ", "")
        else:
            self.password = raw_pw
        self.from_name = (from_name or "مرسل رواتب واتساب").strip()
        self.from_address = (from_address or self.username).strip()
        self.timeout = max(int(timeout), 5)

        # تحديد نوع التشفير تلقائياً حسب المنفذ إذا لم يُحدد صراحة
        if use_ssl is not None:
            self.use_ssl = bool(use_ssl)
        else:
            self.use_ssl = (self.port == 465)

        if use_tls is not None:
            self.use_tls = bool(use_tls)
        else:
            self.use_tls = (self.port == 587 and not self.use_ssl)

    def validate(self) -> Tuple[ConfigStatus, str]:
        """التحقق من صحة واكتمال الإعدادات."""
        if not self.host:
            return ConfigStatus.MISSING_FIELDS, "اسم خادم SMTP (host) مفقود."
        if not self.username:
            return ConfigStatus.MISSING_FIELDS, "اسم مستخدم البريد (username) مفقود."
        if not self.password:
            return ConfigStatus.MISSING_FIELDS, "كلمة مرور البريد (password) مفقودة."
        if not (1 <= self.port <= 65535):
            return ConfigStatus.INVALID_PORT, f"رقم المنفذ ({self.port}) غير صالح."

        upper_pw = self.password.upper()
        upper_user = self.username.upper()
        if "YOUR" in upper_pw or "EXAMPLE" in upper_pw or "YOUR" in upper_user:
            return (
                ConfigStatus.PLACEHOLDER_CREDENTIALS,
                "ملف الإعدادات يحتوي على نصوص توضيحية افتراضية (YOUR_PASSWORD / YOUR_EMAIL) ولم يتم إدخال البيانات الحقيقية.",
            )

        return ConfigStatus.VALID, "الإعدادات مكتملة وصالحة."


def load_email_config() -> Tuple[Optional[EmailConfig], ConfigStatus, str]:
    """
    تحميل إعدادات البريد من متغيرات البيئة أولاً ثم من ملف email_config.json.
    يُرجع: (كائن الإعدادات أو None، حالة الإعدادات، رسالة توضيحية).
    """
    cfg_dict: Dict[str, Any] = {}
    config_file_path = get_config_path()
    file_exists = os.path.exists(config_file_path)

    if file_exists:
        try:
            with open(config_file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg_dict = loaded
                else:
                    return None, ConfigStatus.INVALID_JSON, "محتوى ملف email_config.json ليس كائناً صحيحاً (JSON Object)."
        except json.JSONDecodeError as jde:
            logger.warning(f"Failed to parse {config_file_path}: {jde}")
            return None, ConfigStatus.INVALID_JSON, f"خطأ في قراءة ملف email_config.json: صيغة JSON غير سليمة ({jde.msg})."
        except Exception as ex:
            logger.warning(f"Error reading {config_file_path}: {ex}")
            return None, ConfigStatus.INVALID_JSON, f"تعذر قراءة ملف email_config.json: {str(ex)}"

    # دعم متغيرات البيئة كأولوية قصوى أو كبديل للملف
    env = os.environ
    if env.get("SMTP_HOST"):
        cfg_dict["host"] = env.get("SMTP_HOST")
    if env.get("SMTP_PORT"):
        try:
            cfg_dict["port"] = int(env.get("SMTP_PORT", "587"))
        except ValueError:
            pass
    if env.get("SMTP_USER"):
        cfg_dict["username"] = env.get("SMTP_USER")
    if env.get("SMTP_PASSWORD"):
        cfg_dict["password"] = env.get("SMTP_PASSWORD")
    if env.get("SMTP_FROM_NAME"):
        cfg_dict["from_name"] = env.get("SMTP_FROM_NAME")
    if env.get("SMTP_FROM_ADDRESS"):
        cfg_dict["from_address"] = env.get("SMTP_FROM_ADDRESS")
    if env.get("SMTP_USE_SSL") is not None:
        cfg_dict["use_ssl"] = env.get("SMTP_USE_SSL", "").lower() in {"1", "true", "yes"}
    if env.get("SMTP_USE_TLS") is not None:
        cfg_dict["use_tls"] = env.get("SMTP_USE_TLS", "").lower() in {"1", "true", "yes"}
    if env.get("SMTP_TIMEOUT"):
        try:
            cfg_dict["timeout"] = int(env.get("SMTP_TIMEOUT", "20"))
        except ValueError:
            pass

    if not file_exists and not env.get("SMTP_HOST"):
        return (
            None,
            ConfigStatus.MISSING_FILE,
            f"ملف إعدادات البريد ({DEFAULT_CONFIG_FILENAME}) غير موجود في مسار البرنامج. انسخ email_config.example.json واكتب بياناتك.",
        )

    try:
        config = EmailConfig(
            host=cfg_dict.get("host", ""),
            port=cfg_dict.get("port", 587),
            username=cfg_dict.get("username", ""),
            password=cfg_dict.get("password", ""),
            from_name=cfg_dict.get("from_name", "مرسل رواتب واتساب"),
            from_address=cfg_dict.get("from_address", ""),
            use_tls=cfg_dict.get("use_tls"),
            use_ssl=cfg_dict.get("use_ssl"),
            timeout=cfg_dict.get("timeout", 20),
        )
    except Exception as ex:
        return None, ConfigStatus.INVALID_JSON, f"خطأ في معالجة إعدادات البريد: {str(ex)}"

    status, msg = config.validate()
    if status != ConfigStatus.VALID:
        return None, status, msg

    return config, ConfigStatus.VALID, "جاهز"


def is_configured() -> bool:
    """التحقق السريع مما إذا كانت خدمة البريد مهيأة وجاهزة للاستخدام أو في وضع التطوير."""
    config, status, _ = load_email_config()
    if config is not None and status == ConfigStatus.VALID:
        return True
    return is_dev_mode()


def build_verification_email(
    config: EmailConfig,
    to_email: str,
    recipient_name: str,
    code: str,
    ttl_minutes: int = 10,
) -> EmailMessage:
    """
    إنشاء رسالة البريد الإلكتروني الخاصة بكود التفعيل.
    تحتوي على نسخة نصية عادية (Plain-Text) ونسخة HTML منسقة تدعم اللغة العربية وترميز UTF-8.
    """
    msg = EmailMessage()
    safe_name = recipient_name.strip() or "المستخدم الكريم"
    msg["Subject"] = f"كود تفعيل حسابك: {code} | مرسل رواتب واتساب"
    msg["From"] = formataddr((config.from_name, config.from_address or config.username))
    msg["To"] = to_email.strip()
    msg["Date"] = email.utils.formatdate(localtime=True)

    # إنشاء Message-ID قياسي
    sender_domain = config.username.split("@")[-1] if "@" in config.username else "localhost"
    msg["Message-ID"] = make_msgid(domain=sender_domain)

    # النسخة النصية (Plain-Text Fallback)
    plain_content = (
        f"أهلاً بك {safe_name}،\n\n"
        f"كود تفعيل حسابك في نظام مرسل الرواتب هو:\n"
        f"[{code}]\n\n"
        f"هذا الكود صالح لمدة {ttl_minutes} دقائق فقط، وبحد أقصى 5 محاولات.\n"
        f"إذا لم تكن قد طلبت إنشاء هذا الحساب، يرجى تجاهل هذه الرسالة فوراً.\n\n"
        f"مع تحيات،\nفريق مرسل رواتب واتساب\n"
    )
    msg.set_content(plain_content, charset="utf-8")

    # النسخة المنسقة (HTML Alternative)
    html_content = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>كود تفعيل حسابك</title>
</head>
<body style="margin:0;padding:0;background-color:#f1f5f9;font-family:'Segoe UI',Tahoma,Arial,sans-serif;direction:rtl;text-align:right;color:#1e293b;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#f1f5f9;padding:30px 10px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" max-width="480" cellspacing="0" cellpadding="0" border="0" style="max-width:480px;background-color:#ffffff;border-radius:16px;border:1px solid #e2e8f0;box-shadow:0 4px 6px -1px rgba(0,0,0,0.05);overflow:hidden;">
                    <!-- Header -->
                    <tr>
                        <td style="background-color:#065f46;padding:24px;text-align:center;">
                            <h1 style="margin:0;color:#ffffff;font-size:20px;font-weight:800;letter-spacing:-0.5px;">
                                مرسل رواتب واتساب
                            </h1>
                            <p style="margin:4px 0 0;color:#a7f3d0;font-size:12px;font-weight:600;">
                                نظام إرسال قسائم الرواتب عبر واتساب
                            </p>
                        </td>
                    </tr>
                    <!-- Body Content -->
                    <tr>
                        <td style="padding:28px 24px;">
                            <h2 style="margin:0 0 12px;font-size:17px;font-weight:700;color:#0f172a;">
                                أهلاً بك أ/ {safe_name} 👋
                            </h2>
                            <p style="margin:0 0 20px;font-size:14px;color:#475569;line-height:1.6;">
                                شكراً لتسجيلك في النظام. لإتمام إنشاء حسابك والتحقق من بريدك الإلكتروني، يرجى استخدام رمز التفعيل التالي:
                            </p>
                            <!-- Code Box -->
                            <div style="background-color:#ecfdf5;border:2px dashed #059669;border-radius:12px;padding:18px;margin:24px 0;text-align:center;">
                                <div style="font-family:Courier,'Courier New',monospace;font-size:34px;font-weight:900;letter-spacing:10px;color:#047857;direction:ltr;display:inline-block;">
                                    {code}
                                </div>
                            </div>
                            <p style="margin:0 0 12px;font-size:12px;color:#64748b;line-height:1.5;">
                                ⏱️ هذا الكود صالح لمدة <strong>{ttl_minutes} دقائق</strong> فقط، ولديك 5 محاولات كحد أقصى.
                            </p>
                            <p style="margin:0;font-size:11px;color:#94a3b8;line-height:1.5;border-top:1px solid #f1f5f9;padding-top:16px;">
                                🔒 إذا لم تكن قد طلبت إنشاء حساب في هذا النظام، يمكنك تجاهل هذه الرسالة بأمان دون اتخاذ أي إجراء.
                            </p>
                        </td>
                    </tr>
                    <!-- Footer -->
                    <tr>
                        <td style="background-color:#f8fafc;padding:16px;text-align:center;border-top:1px solid #e2e8f0;">
                            <p style="margin:0;color:#94a3b8;font-size:11px;">
                                تم إرسال هذه الرسالة آلياً بواسطة نظام إدارة الرواتب الداخلي.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""
    msg.add_alternative(html_content, subtype="html")
    return msg


def _send_smtp(config: EmailConfig, msg: EmailMessage):
    """
    تنفيذ عملية الاتصال وإرسال البريد عبر بروتوكول SMTP.
    يتعامل بدقة مع استثناءات smtplib و SSL و Socket.
    """
    ssl_context = ssl.create_default_context()

    if config.use_ssl:
        logger.info(f"Connecting to SMTP server via SSL on {config.host}:{config.port}...")
        try:
            with smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout, context=ssl_context) as server:
                if config.username and config.password:
                    server.login(config.username, config.password)
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP Authentication Error: {e.smtp_code} - {e.smtp_error}")
            raise EmailAuthError(
                "بيانات تسجيل الدخول إلى خادم البريد (SMTP) غير صحيحة. "
                "إذا كنت تستخدم Gmail، تأكد من استخدام كلمة مرور التطبيقات (App Password) المكونة من 16 حرفاً وليس كلمة مرور حسابك العادية.",
                technical_detail=str(e),
            )
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP Recipients Refused: {e.recipients}")
            raise EmailRecipientError(
                "خادم البريد رفض إرسال الرسالة إلى هذا البريد. تأكد من صحة عنوان البريد الإلكتروني للمستلم.",
                technical_detail=str(e),
            )
        except (socket.timeout, TimeoutError) as e:
            logger.error(f"SMTP Connection Timeout: {e}")
            raise EmailConnectionError(
                f"انتهت مهلة الاتصال بخادم البريد ({config.host}:{config.port}). تحقق من اتصال الإنترنت لديك.",
                technical_detail=str(e),
            )
        except (socket.gaierror, ConnectionRefusedError, OSError) as e:
            logger.error(f"SMTP Connection Failure: {e}")
            raise EmailConnectionError(
                f"فشل الاتصال بخادم البريد ({config.host}:{config.port}). تأكد من صحة اسم الخادم وتوفر الاتصال بالشبكة.",
                technical_detail=str(e),
            )
        except ssl.SSLError as e:
            logger.error(f"SSL/TLS Error: {e}")
            raise EmailConnectionError(
                f"فشل تشفير الاتصال الآمن مع خادم البريد: {str(e)}",
                technical_detail=str(e),
            )
        except smtplib.SMTPException as e:
            logger.error(f"SMTP Protocol Error: {e}")
            raise EmailDeliveryError(
                f"حدث خطأ في بروتوكول إرسال البريد: {str(e)}",
                technical_detail=str(e),
            )
    else:
        logger.info(f"Connecting to SMTP server on {config.host}:{config.port}...")
        try:
            with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as server:
                server.ehlo()
                if config.use_tls:
                    server.starttls(context=ssl_context)
                    server.ehlo()
                if config.username and config.password:
                    server.login(config.username, config.password)
                server.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP Authentication Error: {e.smtp_code} - {e.smtp_error}")
            raise EmailAuthError(
                "بيانات تسجيل الدخول إلى خادم البريد (SMTP) غير صحيحة. "
                "إذا كنت تستخدم Gmail، تأكد من استخدام كلمة مرور التطبيقات (App Password) المكونة من 16 حرفاً وليس كلمة مرور حسابك العادية.",
                technical_detail=str(e),
            )
        except smtplib.SMTPRecipientsRefused as e:
            logger.error(f"SMTP Recipients Refused: {e.recipients}")
            raise EmailRecipientError(
                "خادم البريد رفض إرسال الرسالة إلى هذا البريد. تأكد من صحة عنوان البريد الإلكتروني للمستلم.",
                technical_detail=str(e),
            )
        except (socket.timeout, TimeoutError) as e:
            logger.error(f"SMTP Connection Timeout: {e}")
            raise EmailConnectionError(
                f"انتهت مهلة الاتصال بخادم البريد ({config.host}:{config.port}). تحقق من اتصال الإنترنت لديك.",
                technical_detail=str(e),
            )
        except (socket.gaierror, ConnectionRefusedError, OSError) as e:
            logger.error(f"SMTP Connection Failure: {e}")
            raise EmailConnectionError(
                f"فشل الاتصال بخادم البريد ({config.host}:{config.port}). تأكد من صحة اسم الخادم وتوفر الاتصال بالشبكة.",
                technical_detail=str(e),
            )
        except ssl.SSLError as e:
            logger.error(f"SSL/TLS Error: {e}")
            raise EmailConnectionError(
                f"فشل تشفير الاتصال الآمن مع خادم البريد: {str(e)}",
                technical_detail=str(e),
            )
        except smtplib.SMTPException as e:
            logger.error(f"SMTP Protocol Error: {e}")
            raise EmailDeliveryError(
                f"حدث خطأ في بروتوكول إرسال البريد: {str(e)}",
                technical_detail=str(e),
            )


def send_otp_email(to_email: str, recipient_name: str, code: str) -> Dict[str, Any]:
    """
    إرسال كود التفعيل إلى بريد المستلم.

    السلوك:
    - إذا كانت الإعدادات صالحة: يُنفذ إرسالاً حقيقياً عبر SMTP.
    - إذا كانت الإعدادات غير متوفرة ولكن مفعل وضع التطوير (AUTH_DEV_MODE=1):
      يُسجل محاكاة الإرسال ويطبع الكود للمطور مع إرجاع علامة تدل على المحاكاة.
    - في بيئة الإنتاج أو عند عدم تفعيل وضع التطوير: يفشل بإطلاق استثناء دقيق وواضح.
    """
    config, status, msg = load_email_config()

    if config is None or status != ConfigStatus.VALID:
        if is_dev_mode():
            masked_recipient = sanitize_email_for_log(to_email)
            logger.warning(
                f"[DEV_MODE] لم يتم العثور على إعدادات بريد صالحة ({msg}). "
                f"تمت محاكاة إرسال كود التفعيل للمستلم {masked_recipient} بنجاح: {code}"
            )
            return {
                "delivered": False,
                "simulated": True,
                "mode": "dev_simulation",
                "code": code,
                "message": "تمت محاكاة الإرسال في وضع التطوير المحلي.",
            }
        # في بيئة الإنتاج أو بدون وضع التطوير: إطلاق خطأ إعدادات صريح
        raise EmailConfigError(
            f"إعدادات البريد الإلكتروني غير مكتملة أو غير صالحة ({msg}). "
            f"يرجى ضبط ملف {DEFAULT_CONFIG_FILENAME} وفقاً للخطوات في ملف README."
        )

    # إنشاء الرسالة وتنفيذ الإرسال الفعلي
    email_msg = build_verification_email(
        config=config,
        to_email=to_email,
        recipient_name=recipient_name,
        code=code,
    )

    masked_recipient = sanitize_email_for_log(to_email)
    logger.info(f"Sending verification OTP email to {masked_recipient} via {config.host}:{config.port}...")
    _send_smtp(config, email_msg)
    logger.info(f"Verification OTP email successfully delivered to {masked_recipient}.")

    return {
        "delivered": True,
        "simulated": False,
        "mode": "smtp",
        "message": "تم إرسال كود التفعيل إلى بريدك الإلكتروني بنجاح.",
    }

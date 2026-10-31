"""
محرك الربط والإرسال الحقيقي لواتساب ويب (WhatsApp Web Real Integration via Selenium).

المميزات:
- إدارة حقيقية لجلسة المتصفح (Chrome أو Edge) بملف تعريف دائم ومعزول لكل مستخدم.
- استخراج والتقاط كود الـ QR الحقيقي مباشرة من صفحة WhatsApp Web بدون أي محاكاة وهمية.
- رصد وتتبع حالات الاتصال بدقة:
  DISCONNECTED, STARTING, WAITING_FOR_QR, AUTHENTICATING, CONNECTED, RECONNECTING, ERROR.
- رصد حقيقي لحالة تسليم الرسائل:
  SUBMITTED (قيد الإرسال/أيقونة الساعة), SENT (تم الإرسال لخادم واتساب/علامة صح واحدة),
  DELIVERED (تم التسليم لهاتف المستلم/علامتي صح), FAILED (فشل الإرسال أو الرقم غير مسجل).
- دعم قطع الاتصال (Disconnect) وإعادة تحميل الكود (Reload QR).
- تزامن آمن وحماية من تشغيل أكثر من جلسة للمستخدم نفسه (Thread-safe UserContext).
"""

from contextlib import contextmanager
import contextvars
from enum import Enum
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger("payroll.whatsapp")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[%(asctime)s] [WhatsApp] [%(levelname)s] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

# تحديد المسارات
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "user_data")
os.makedirs(DATA_DIR, exist_ok=True)

_ACTIVE_USER = contextvars.ContextVar("sender_user_id", default="anonymous")
_CONTEXTS: Dict[str, "UserContext"] = {}
_CONTEXTS_LOCK = threading.Lock()


class WhatsAppState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    STARTING = "STARTING"
    WAITING_FOR_QR = "WAITING_FOR_QR"
    WAITING_FOR_PAIRING_CODE = "WAITING_FOR_PAIRING_CODE"
    PAIRING_CODE_READY = "PAIRING_CODE_READY"
    AUTHENTICATING = "AUTHENTICATING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


class MessageStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


# محددات عناصر واجهة WhatsApp Web الرسمية
QR_CANVAS_XPATHS = [
    '//div[@data-testid="qrcode"]//canvas',
    '//canvas[@aria-label]',
    '//div[@data-ref]//canvas',
    '//canvas',
]

QR_RELOAD_BTN_XPATHS = [
    '//div[@data-testid="qrcode"]//button',
    '//div[@data-testid="qrcode"]//span[@data-icon="refresh"]',
    '//span[contains(text(), "reload") or contains(text(), "تحميل") or contains(text(), "Reload")]',
    '//div[@role="button"][contains(., "Click to reload") or contains(., "تحميل")]',
]

LOGGED_IN_XPATHS = [
    '//div[@id="pane-side"]',
    '//div[@aria-label="قائمة المحادثات"]',
    '//div[@aria-label="Chat list"]',
    '//div[@data-testid="chat-list"]',
    '//span[@data-icon="chats-filled"]',
    '//span[@data-icon="chat"]',
]

AUTHENTICATING_HINTS = [
    '//div[contains(text(), "Connecting") or contains(text(), "جاري الاتصال")]',
    '//div[contains(text(), "Downloading") or contains(text(), "جاري تنزيل")]',
    '//div[contains(text(), "Loading your chats") or contains(text(), "تحميل المحادثات")]',
    '//progress',
]

SEND_BUTTON_XPATHS = [
    '//button[@aria-label="إرسال"]',
    '//button[@aria-label="Send"]',
    '//span[@data-icon="send"]/ancestor::button',
    '//span[@data-icon="wds-ic-send-filled"]/ancestor::button',
    '//div[@role="button"][@aria-label="Send"]',
]

MESSAGE_BOX_XPATHS = [
    '//footer//div[@contenteditable="true"][@role="textbox"]',
    '//div[@contenteditable="true"][@data-tab="10"]',
    '//footer//div[@contenteditable="true"]',
]

INVALID_NUMBER_HINTS = [
    "رقم الهاتف غير صالح",
    "غير موجود على واتساب",
    "Phone number shared via url is invalid",
    "isn't on WhatsApp",
    "is not on WhatsApp",
]


class Campaign:
    """كائن حالة حملة الإرسال النشطة."""
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.running = False
        self.paused = False
        self.stop_requested = False
        self.skip_requested = False
        self.phase = "idle"  # idle, connecting, waiting_login, sending, waiting, batch_break, done, stopped, error
        self.message = ""
        self.countdown = 0
        self.countdown_total = 0
        self.current_id: Optional[int] = None
        self.current_name = ""
        self.current_phone = ""
        self.results: Dict[int, Dict[str, Any]] = {}
        self.total = 0
        self.linked_number = ""
        self.thread: Optional[threading.Thread] = None

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "paused": self.paused,
                "phase": self.phase,
                "message": self.message,
                "countdown": self.countdown,
                "countdown_total": self.countdown_total,
                "current_id": self.current_id,
                "current_name": self.current_name,
                "current_phone": self.current_phone,
                "results": {str(k): v for k, v in self.results.items()},
                "total": self.total,
                "linked_number": self.linked_number,
            }


class UserContext:
    """سياق معزول لكل مستخدم يحتوي على الجلسة والمتصفح والحالة."""
    def __init__(self, user_id: str):
        self.user_id = str(user_id)
        self.profile_dir = os.path.join(DATA_DIR, self.user_id, "wa_profile")
        self.log_file = os.path.join(DATA_DIR, self.user_id, "sender_log.txt")
        self.campaign = Campaign()
        self.driver: Optional[webdriver.Remote] = None
        self.driver_lock = threading.Lock()
        self.monitor_thread: Optional[threading.Thread] = None
        self.monitor_active = False

        self.wa_state: Dict[str, Any] = {
            "state": WhatsAppState.DISCONNECTED.value,
            "logged_in": False,
            "number": "",
            "qr_image": None,
            "qr_expired": False,
            "browser_open": False,
            "error": "",
            "pairing_code": "",
            "pairing_phone": "",
            "pairing_expired": False,
            "pairing_error": "",
            "pairing_time": 0,
            "last_updated": time.time(),
        }


def _ctx() -> UserContext:
    uid = _ACTIVE_USER.get()
    with _CONTEXTS_LOCK:
        return _CONTEXTS.setdefault(uid, UserContext(uid))


@contextmanager
def user_context(user_id: Any):
    """ربط العمليات الحالية بحساب المستخدم المحدد."""
    token = _ACTIVE_USER.set(str(user_id))
    try:
        yield
    finally:
        _ACTIVE_USER.reset(token)


def current_user_id() -> str:
    return _ACTIVE_USER.get()


def _thread_for_user(user_id: str, target, *args):
    def runner():
        with user_context(user_id):
            target(*args)
    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread


class _StateProxy:
    def __init__(self, attr: str):
        object.__setattr__(self, "_attr", attr)

    def __getattr__(self, name: str):
        return getattr(getattr(_ctx(), self._attr), name)

    def __setattr__(self, name: str, value: Any):
        setattr(getattr(_ctx(), self._attr), name, value)


class _DictProxy(_StateProxy):
    def __getitem__(self, key):
        return getattr(_ctx(), self._attr)[key]

    def __setitem__(self, key, value):
        getattr(_ctx(), self._attr)[key] = value

    def update(self, *args, **kwargs):
        return getattr(_ctx(), self._attr).update(*args, **kwargs)

    def copy(self):
        return getattr(_ctx(), self._attr).copy()

    def __iter__(self):
        return iter(getattr(_ctx(), self._attr))


campaign = _StateProxy("campaign")
wa_state = _DictProxy("wa_state")


def _set(**kw):
    with campaign.lock:
        for k, v in kw.items():
            setattr(campaign, k, v)


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    logger.info(msg)
    try:
        ctx = _ctx()
        os.makedirs(os.path.dirname(ctx.log_file), exist_ok=True)
        with open(ctx.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def snap(driver, name: str):
    """حفظ لقطة شاشة للتشخيص في مجلد المستخدم عند حدوث أخطاء."""
    try:
        ctx = _ctx()
        os.makedirs(os.path.dirname(ctx.log_file), exist_ok=True)
        path = os.path.join(os.path.dirname(ctx.log_file), f"error_{name}_{int(time.time())}.png")
        driver.save_screenshot(path)
        log(f"تم حفظ صورة تشخيصية للشاشة: {path}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# محرك تشغيل وإدارة المتصفح (Chrome / Edge)
# ---------------------------------------------------------------------------
def _detect_browser_paths() -> Tuple[Optional[str], Optional[str]]:
    """فحص مسارات المتصفحات المتاحة على نظام التشغيل."""
    chrome_candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    edge_candidates = [
        shutil.which("msedge"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    chrome_path = next((p for p in chrome_candidates if p and os.path.exists(p)), None)
    edge_path = next((p for p in edge_candidates if p and os.path.exists(p)), None)
    return chrome_path, edge_path


def _build_driver() -> webdriver.Remote:
    """تشغيل متصفح حقيقي (Chrome أو Edge) بملف تعريف دائم ومعزول."""
    ctx = _ctx()
    os.makedirs(ctx.profile_dir, exist_ok=True)
    chrome_path, edge_path = _detect_browser_paths()
    errors = []

    # إذا كان Chrome غير مثبت و Edge مثبت، نبدأ بـ Edge فوراً لاختصار الوقت
    browsers_to_try = []
    if chrome_path:
        browsers_to_try.append(("chrome", chrome_path))
    if edge_path:
        browsers_to_try.append(("edge", edge_path))
    if not browsers_to_try:
        browsers_to_try = [("edge", None), ("chrome", None)]

    for browser_name, binary_path in browsers_to_try:
        if browser_name == "chrome":
            try:
                opts = webdriver.ChromeOptions()
                profile_path = os.path.join(ctx.profile_dir, "chrome")
                opts.add_argument(f"--user-data-dir={profile_path}")
                opts.add_argument("--start-maximized")
                opts.add_argument("--disable-notifications")
                opts.add_argument("--lang=ar")
                opts.add_experimental_option("excludeSwitches", ["enable-automation"])
                opts.add_experimental_option("useAutomationExtension", False)
                if binary_path:
                    opts.binary_location = binary_path
                driver = webdriver.Chrome(options=opts)
                log(f"تم تشغيل متصفح Chrome بنجاح للمستخدم {ctx.user_id}.")
                return driver
            except Exception as e:
                errors.append(f"Chrome: {e}")
        elif browser_name == "edge":
            try:
                opts = webdriver.EdgeOptions()
                profile_path = os.path.join(ctx.profile_dir, "edge")
                opts.add_argument(f"--user-data-dir={profile_path}")
                opts.add_argument("--start-maximized")
                opts.add_argument("--disable-notifications")
                opts.add_argument("--lang=ar")
                opts.add_experimental_option("excludeSwitches", ["enable-automation"])
                opts.add_experimental_option("useAutomationExtension", False)
                if binary_path:
                    opts.binary_location = binary_path
                driver = webdriver.Edge(options=opts)
                log(f"تم تشغيل متصفح Edge بنجاح للمستخدم {ctx.user_id}.")
                return driver
            except Exception as e:
                errors.append(f"Edge: {e}")

    raise RuntimeError(
        "تعذر تشغيل متصفح Chrome أو Edge. تأكد من تثبيت أحدهما وتوفر الاتصال بالإنترنت:\n" + "\n".join(errors)
    )


def _get_driver() -> webdriver.Remote:
    ctx = _ctx()
    with ctx.driver_lock:
        if ctx.driver is not None:
            try:
                _ = ctx.driver.current_url
                return ctx.driver
            except Exception:
                ctx.driver = None
        ctx.driver = _build_driver()
        return ctx.driver


def close_driver():
    """إغلاق المتصفح وإيقاف المراقبة وتنظيف الموارد."""
    ctx = _ctx()
    ctx.monitor_active = False
    with ctx.driver_lock:
        if ctx.driver is not None:
            try:
                ctx.driver.quit()
            except Exception:
                pass
            ctx.driver = None
    wa_state.update(
        state=WhatsAppState.DISCONNECTED.value,
        browser_open=False,
        logged_in=False,
        qr_image=None,
        qr_expired=False,
        number="",
        error="",
        last_updated=time.time(),
    )
    log(f"تم إغلاق متصفح واتساب للمستخدم {ctx.user_id}.")


def _find_first(driver, xpaths: List[str], timeout: float = 0):
    end = time.time() + timeout
    while True:
        for xp in xpaths:
            try:
                els = driver.find_elements(By.XPATH, xp)
                for el in els:
                    if el.is_displayed():
                        return el
            except WebDriverException:
                pass
        if time.time() >= end:
            return None
        time.sleep(0.3)


def _linked_number(driver) -> Optional[str]:
    """استخراج رقم الهاتف المربوط من LocalStorage بعد تسجيل الدخول."""
    try:
        wid = driver.execute_script(
            "return window.localStorage.getItem('last-wid-md') || window.localStorage.getItem('last-wid');"
        )
    except Exception:
        return None
    if not wid:
        return None
    digits = "".join(filter(str.isdigit, str(wid).split("@")[0]))
    return digits or None


def _is_logged_in(driver) -> bool:
    if _linked_number(driver):
        return True
    return bool(_find_first(driver, LOGGED_IN_XPATHS, 0))


# ---------------------------------------------------------------------------
# استخراج كود الـ QR الحقيقي ومراقبة حالة المصادقة
# ---------------------------------------------------------------------------
def _extract_qr_base64(driver) -> Tuple[Optional[str], bool]:
    """
    استخراج كود الـ QR الحقيقي المرسوم داخل صفحة WhatsApp Web.
    يُرجع: (الصورة بتنسيق base64 data URL أو None, هل انتهت صلاحية الكود).
    """
    # 1. فحص زر إعادة تحميل الـ QR المنتهي
    reload_btn = _find_first(driver, QR_RELOAD_BTN_XPATHS, timeout=0)
    is_expired = bool(reload_btn and reload_btn.is_displayed())

    # 2. محاولة قراءة بيانات الـ Canvas عبر JavaScript مباشرة
    try:
        data_url = driver.execute_script("""
            var canvas = document.querySelector('div[data-testid="qrcode"] canvas, canvas[aria-label], div[data-ref] canvas, canvas');
            if (canvas && canvas.width >= 50 && canvas.height >= 50) {
                return canvas.toDataURL('image/png');
            }
            return null;
        """)
        if data_url and data_url.startswith("data:image/png;base64,"):
            return data_url, is_expired
    except Exception:
        pass

    # 3. محاولة أخذ لقطة شاشة دقيقة لعنصر الـ Canvas نفسه عبر Selenium
    try:
        canvas_el = _find_first(driver, QR_CANVAS_XPATHS, timeout=0)
        if canvas_el and canvas_el.is_displayed():
            b64 = canvas_el.screenshot_as_base64
            if b64:
                return f"data:image/png;base64,{b64}", is_expired
    except Exception:
        pass

    return None, is_expired


def _connection_monitor_worker(user_id: str):
    """خيط مراقبة دوري لتحديث حالة الاتصال والـ QR الحقيقي في الخلفية."""
    with user_context(user_id):
        ctx = _ctx()
        log("بدء مراقبة جلسة واتساب...")
        max_duration = 600  # 10 دقائق كحد أقصى للمراقبة المتواصلة في وضع انتظار الدخول
        start_time = time.time()

        while ctx.monitor_active and (time.time() - start_time < max_duration):
            with ctx.driver_lock:
                driver = ctx.driver

            if driver is None:
                wa_state.update(
                    state=WhatsAppState.DISCONNECTED.value,
                    browser_open=False,
                    logged_in=False,
                    qr_image=None,
                    last_updated=time.time(),
                )
                break

            try:
                # 1. فحص إذا تم تسجيل الدخول بالفعل
                if _is_logged_in(driver):
                    num = _linked_number(driver) or ""
                    wa_state.update(
                        state=WhatsAppState.CONNECTED.value,
                        logged_in=True,
                        number=num,
                        qr_image=None,
                        qr_expired=False,
                        pairing_code="",
                        pairing_expired=False,
                        browser_open=True,
                        error="",
                        last_updated=time.time(),
                    )
                    log(f"جلسة واتساب متصلة بنجاح (الرقم: {num or 'غير محدد'}).")
                    break

                # 2. فحص شاشة المزامنة / التوثيق الجاري
                authenticating_el = _find_first(driver, AUTHENTICATING_HINTS, timeout=0)
                if authenticating_el and authenticating_el.is_displayed():
                    wa_state.update(
                        state=WhatsAppState.AUTHENTICATING.value,
                        browser_open=True,
                        qr_image=None,
                        qr_expired=False,
                        last_updated=time.time(),
                    )
                    time.sleep(1.0)
                    continue

                # 2.5 فحص حالة كود الربط بالهاتف (إذا كنا في وضع كود الهاتف)
                curr_state = wa_state.get("state")
                if curr_state in (WhatsAppState.PAIRING_CODE_READY.value, WhatsAppState.WAITING_FOR_PAIRING_CODE.value):
                    pairing_t = wa_state.get("pairing_time", 0)
                    if pairing_t > 0 and (time.time() - pairing_t > 180):
                        wa_state.update(pairing_expired=True, last_updated=time.time())
                    time.sleep(1.5)
                    continue

                # 3. محاولة استخراج كود الـ QR الحقيقي
                qr_b64, is_expired = _extract_qr_base64(driver)
                if qr_b64:
                    wa_state.update(
                        state=WhatsAppState.WAITING_FOR_QR.value,
                        browser_open=True,
                        logged_in=False,
                        qr_image=qr_b64,
                        qr_expired=is_expired,
                        error="",
                        last_updated=time.time(),
                    )
                else:
                    # في انتظار تحميل الـ QR لأول مرة
                    if wa_state["state"] not in (WhatsAppState.WAITING_FOR_QR.value, WhatsAppState.AUTHENTICATING.value):
                        wa_state.update(
                            state=WhatsAppState.STARTING.value,
                            browser_open=True,
                            last_updated=time.time(),
                        )

            except Exception as e:
                log(f"تنبيه أثناء مراقبة المتصفح: {e}")
                wa_state.update(
                    state=WhatsAppState.ERROR.value,
                    error=f"خطأ في الاتصال بالمتصفح: {type(e).__name__}",
                    last_updated=time.time(),
                )

            time.sleep(1.5)

        ctx.monitor_active = False
        log("انتهاء مراقبة جلسة واتساب.")


def connect_whatsapp() -> bool:
    """بدء عملية ربط واتساب الحقيقية وتشغيل المراقبة واستخراج الـ QR."""
    ctx = _ctx()
    with campaign.lock:
        if campaign.running:
            return False

    wa_state.update(
        state=WhatsAppState.STARTING.value,
        browser_open=True,
        logged_in=False,
        qr_image=None,
        qr_expired=False,
        error="",
        last_updated=time.time(),
    )

    def startup_runner():
        try:
            driver = _get_driver()
            log("فتح صفحة web.whatsapp.com...")
            if "web.whatsapp.com" not in (driver.current_url or ""):
                driver.get("https://web.whatsapp.com")

            # بدء خيط المراقبة واستخراج الـ QR
            ctx.monitor_active = True
            t = threading.Thread(target=_connection_monitor_worker, args=(ctx.user_id,), daemon=True)
            ctx.monitor_thread = t
            t.start()
        except Exception as e:
            wa_state.update(
                state=WhatsAppState.ERROR.value,
                error=f"تعذر بدء المتصفح: {str(e)}",
                browser_open=False,
                last_updated=time.time(),
            )
            log(f"فشل بدء متصفح واتساب: {e}")

    _thread_for_user(current_user_id(), startup_runner)
    return True


def reload_qr() -> bool:
    """إعادة توليد كود الـ QR عند انتهائه عبر النقر على زر التحديث في المتصفح."""
    ctx = _ctx()
    with ctx.driver_lock:
        driver = ctx.driver
    if not driver:
        return False
    try:
        btn = _find_first(driver, QR_RELOAD_BTN_XPATHS, timeout=2)
        if btn and btn.is_displayed():
            btn.click()
            time.sleep(1)
            return True
        # إذا لم نجد زر محدد، نحدث الصفحة
        driver.refresh()
        return True
    except Exception as e:
        log(f"تعذر إعادة تحميل الـ QR: {e}")
        return False


def disconnect_whatsapp() -> bool:
    """قطع الاتصال وإغلاق جلسة المتصفح بأمان."""
    close_driver()
    return True


def request_phone_pairing(phone: str, country_code: str = "20") -> Tuple[bool, str, Optional[str]]:
    """
    طلب إنشاء كود ربط حقيقي (Phone Number Pairing Code) من واتساب ويب.
    يُرجع: (نجاح؟، رسالة تفصيلية، كود الربط المكون من 8 خانات).
    """
    ctx = _ctx()
    with campaign.lock:
        if campaign.running:
            return False, "لا يمكن إنشاء كود الربط أثناء تشغيل حملة إرسال.", None

    clean_digits = re.sub(r"\D", "", phone or "")
    country_clean = re.sub(r"\D", "", country_code or "20")

    if country_clean == "20":
        if clean_digits.startswith("20"):
            clean_digits = clean_digits[2:]
        if clean_digits.startswith("0"):
            clean_digits = clean_digits[1:]

        if len(clean_digits) != 10 or not (clean_digits.startswith(("10", "11", "12", "15"))):
            return False, "يرجى إدخال رقم محمول مصري صحيح مكون من 11 رقماً (مثال: 01012345678).", None
    else:
        if len(clean_digits) < 7:
            return False, "رقم الهاتف غير صالح.", None

    formatted_display = f"+{country_clean} {clean_digits}"
    log(f"طلب كود ربط برقم الهاتف: {formatted_display}")

    try:
        driver = _get_driver()
        if "web.whatsapp.com" not in (driver.current_url or ""):
            driver.get("https://web.whatsapp.com")
            time.sleep(2)

        if _is_logged_in(driver):
            num = _linked_number(driver) or ""
            wa_state.update(
                state=WhatsAppState.CONNECTED.value,
                logged_in=True,
                number=num,
                qr_image=None,
                browser_open=True,
            )
            return False, f"واتساب متصل بالفعل بالرقم {num or ''}", None

        wa_state.update(
            state=WhatsAppState.WAITING_FOR_PAIRING_CODE.value,
            pairing_phone=formatted_display,
            pairing_code="",
            pairing_error="",
            pairing_expired=False,
            browser_open=True,
            last_updated=time.time(),
        )

        time.sleep(1.5)
        click_link_js = """
        let btn = document.querySelector('[data-testid="link_device_qr_phone_number_link"]') || 
                  document.querySelector('[data-testid="link_device_qr_phone_number_shortcut_link"]') ||
                  Array.from(document.querySelectorAll('div[role="button"], span, a')).find(e => {
                      let t = (e.innerText || '').trim();
                      return t.includes('تسجيل الدخول برقم الهاتف') || t.includes('الربط برقم الهاتف') || t.includes('Link with phone number');
                  });
        if (btn) {
            btn.click();
            return true;
        }
        return false;
        """
        link_clicked = driver.execute_script(click_link_js)
        if not link_clicked:
            try:
                btn_el = _find_first(driver, [
                    '//*[@data-testid="link_device_qr_phone_number_link"]',
                    '//*[@data-testid="link_device_qr_phone_number_shortcut_link"]',
                    '//*[contains(text(), "تسجيل الدخول برقم الهاتف")]',
                    '//*[contains(text(), "الربط برقم الهاتف")]',
                ], timeout=3)
                if btn_el:
                    btn_el.click()
                    link_clicked = True
            except Exception:
                pass

        time.sleep(1.5)

        inp_el = _find_first(driver, [
            '//input[@data-testid="phone-number-input"]',
            '//input[@type="text"]',
            '//input[contains(@aria-label, "رقم هاتفك")]',
        ], timeout=5)

        if not inp_el:
            wa_state.update(state=WhatsAppState.ERROR.value, pairing_error="لم يتم العثور على حقل إدخال رقم الهاتف في واتساب ويب.")
            return False, "تعذر الوصول لحقل إدخال رقم الهاتف في واتساب ويب.", None

        inp_el.click()
        inp_el.send_keys(Keys.CONTROL, "a")
        inp_el.send_keys(Keys.BACKSPACE)
        time.sleep(0.2)
        inp_el.send_keys(clean_digits)
        time.sleep(0.5)

        next_btn = _find_first(driver, [
            '//button[contains(., "التالي") or contains(., "Next")]',
            '//div[@role="button"][contains(., "التالي") or contains(., "Next")]',
            '//button[@type="submit"]',
        ], timeout=3)

        if not next_btn:
            inp_el.send_keys(Keys.ENTER)
        else:
            next_btn.click()

        log("تم إرسال رقم الهاتف، جاري انتظار توليد الكود من خوادم واتساب...")

        pairing_code = None
        extract_deadline = time.time() + 15
        while time.time() < extract_deadline:
            time.sleep(1.0)

            if _is_logged_in(driver):
                num = _linked_number(driver) or ""
                wa_state.update(
                    state=WhatsAppState.CONNECTED.value,
                    logged_in=True,
                    number=num,
                    pairing_code="",
                    last_updated=time.time(),
                )
                return True, "تم الاتصال بنجاح!", None

            err_text = driver.execute_script("""
                let err = document.querySelector('[data-testid="phone-number-input-error"]') || 
                          Array.from(document.querySelectorAll('div, span')).find(e => {
                              let t = (e.innerText || '').trim();
                              return t.includes('رقم هاتف غير صالح') || t.includes('رقم الهاتف قصير') || t.includes('Invalid phone number');
                          });
                return err ? err.innerText.trim() : null;
            """)
            if err_text:
                wa_state.update(
                    state=WhatsAppState.ERROR.value,
                    pairing_error=err_text,
                    last_updated=time.time(),
                )
                return False, f"تنبيه من واتساب: {err_text}", None

            extract_js = """
            let codeContainer = document.querySelector('[data-link-code]');
            if (codeContainer) {
                let c = codeContainer.getAttribute('data-link-code');
                if (c && c.length >= 8) return c;
            }

            let slots = Array.from(document.querySelectorAll('div[data-testid*="code-slot"], span[data-testid*="code-slot"], div[aria-details]'));
            if (slots.length >= 8) {
                let txt = slots.map(s => s.innerText.trim()).filter(Boolean).join('');
                if (txt.length >= 8) return txt;
            }

            let bodyText = document.body.innerText || '';
            let match = bodyText.match(/\\b([A-Z0-9]{4})-([A-Z0-9]{4})\\b/);
            if (match) return match[1] + match[2];

            let rawMatch = bodyText.match(/\\b([A-Z0-9]{8})\\b/);
            if (rawMatch) return rawMatch[1];

            return null;
            """
            raw_code = driver.execute_script(extract_js)
            if raw_code:
                raw_code = re.sub(r"[^A-Za-z0-9]", "", raw_code).upper()
                if len(raw_code) >= 8:
                    pairing_code = f"{raw_code[:4]}-{raw_code[4:8]}"
                    break

        if not pairing_code:
            snap(driver, "pairing_code_timeout")
            wa_state.update(
                state=WhatsAppState.ERROR.value,
                pairing_error="انتهت مهلة استخراج كود الربط من واتساب ويب.",
                last_updated=time.time(),
            )
            return False, "تعذر استخراج كود الربط في الوقت المحدد. يرجى المحاولة مرة أخرى.", None

        log(f"تم الحصول على كود الربط الحقيقي: {pairing_code}")
        wa_state.update(
            state=WhatsAppState.PAIRING_CODE_READY.value,
            pairing_code=pairing_code,
            pairing_time=time.time(),
            pairing_expired=False,
            pairing_error="",
            last_updated=time.time(),
        )

        if not ctx.monitor_active:
            ctx.monitor_active = True
            t = threading.Thread(target=_connection_monitor_worker, args=(ctx.user_id,), daemon=True)
            ctx.monitor_thread = t
            t.start()

        return True, "تم إنشاء كود الربط بنجاح.", pairing_code

    except Exception as e:
        log(f"خطأ غير متوقع أثناء طلب كود الربط: {e}")
        wa_state.update(
            state=WhatsAppState.ERROR.value,
            pairing_error=str(e),
            last_updated=time.time(),
        )
        return False, f"حدث خطأ أثناء الاتصال بالمتصفح: {str(e)}", None


def cancel_phone_pairing() -> bool:
    """إلغاء وضع كود الربط والعودة لشاشة رمز الـ QR الأصلية."""
    ctx = _ctx()
    with ctx.driver_lock:
        driver = ctx.driver
    if not driver:
        wa_state.update(
            state=WhatsAppState.DISCONNECTED.value,
            pairing_code="",
            pairing_expired=False,
            last_updated=time.time(),
        )
        return True

    try:
        back_js = """
        let qrBtn = document.querySelector('[data-testid="link_device_phone_number_qr_link"]') || 
                    Array.from(document.querySelectorAll('div[role="button"], span, a, button')).find(e => {
                        let t = (e.innerText || '').trim();
                        return t.includes('تسجيل الدخول باستخدام رمز QR') || t.includes('رمز QR') || t.includes('QR code');
                    });
        if (qrBtn) {
            qrBtn.click();
            return true;
        }
        return false;
        """
        clicked = driver.execute_script(back_js)
        if not clicked:
            driver.get("https://web.whatsapp.com")

        wa_state.update(
            state=WhatsAppState.STARTING.value,
            pairing_code="",
            pairing_expired=False,
            pairing_error="",
            last_updated=time.time(),
        )
        time.sleep(1)
        return True
    except Exception as e:
        log(f"تعذر العودة لشاشة الـ QR: {e}")
        try:
            driver.get("https://web.whatsapp.com")
        except Exception:
            pass
        return False


def whatsapp_status() -> Dict[str, Any]:
    """إرجاع الحالة الحقيقية والمحدثة لجلسة واتساب."""
    ctx = _ctx()
    with ctx.driver_lock:
        d = ctx.driver

    if d is None:
        wa_state.update(
            state=WhatsAppState.DISCONNECTED.value,
            browser_open=False,
            logged_in=False,
            number="",
            qr_image=None,
        )
    else:
        # فحص سريع وخفيف إذا كان المتصفح متصلاً
        try:
            num = _linked_number(d)
            if num or _is_logged_in(d):
                wa_state.update(
                    state=WhatsAppState.CONNECTED.value,
                    logged_in=True,
                    number=num or wa_state.get("number", ""),
                    qr_image=None,
                    qr_expired=False,
                    browser_open=True,
                )
        except Exception:
            pass

    return dict(wa_state)


# ---------------------------------------------------------------------------
# إرسال الرسائل الحقيقي مع التحقق الدقيق من حالات التسليم
# ---------------------------------------------------------------------------
def _dismiss_popup(driver):
    for xp in [
        '//button[.//div[text()="موافق"]]',
        '//button[.//div[text()="OK"]]',
        '//div[@role="button"][.//div[text()="OK"]]',
        '//button[@data-testid="popup-controls-ok"]',
    ]:
        try:
            for b in driver.find_elements(By.XPATH, xp):
                if b.is_displayed():
                    b.click()
                    return
        except WebDriverException:
            pass


def _page_has_invalid_hint(driver) -> bool:
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
    except WebDriverException:
        return False
    return any(h in body for h in INVALID_NUMBER_HINTS)


def _inspect_delivery_status(driver) -> Tuple[MessageStatus, str]:
    """
    فحص دقيق لحالة الرسالة الصادرة الأخيرة في المحادثة عبر مؤشرات WhatsApp الرسمية.
    - msg-dblcheck-ack (أزرق) أو msg-dblcheck (رمادي): DELIVERED
    - msg-check (علامة واحدة): SENT
    - msg-time (ساعة): SUBMITTED
    """
    try:
        outgoing_msgs = driver.find_elements(By.CSS_SELECTOR, "div.message-out")
        if not outgoing_msgs:
            return MessageStatus.SUBMITTED, "تم إرسال الرسالة، في انتظار ظهورها في المحادثة"

        last_msg = outgoing_msgs[-1]

        # فحص أيقونات علامات الصح والساعة داخل الرسالة الأخيرة
        has_dblcheck_read = bool(last_msg.find_elements(By.CSS_SELECTOR, 'span[data-icon="msg-dblcheck-ack"], span[data-icon="msg-dblcheck-ack-preview"]'))
        has_dblcheck = bool(last_msg.find_elements(By.CSS_SELECTOR, 'span[data-icon="msg-dblcheck"]'))
        has_single_check = bool(last_msg.find_elements(By.CSS_SELECTOR, 'span[data-icon="msg-check"]'))
        has_time_clock = bool(last_msg.find_elements(By.CSS_SELECTOR, 'span[data-icon="msg-time"]'))

        if has_dblcheck_read:
            return MessageStatus.DELIVERED, "تم التسليم والقراءة (Double Blue Check)"
        if has_dblcheck:
            return MessageStatus.DELIVERED, "تم التسليم لهاتف المستلم بنجاح (Double Check)"
        if has_single_check:
            return MessageStatus.SENT, "تم الإرسال لخوادم واتساب بنجاح (Single Check)"
        if has_time_clock:
            return MessageStatus.SUBMITTED, "تم تقديم الرسالة بنجاح (أيقونة الساعة/قيد الإرسال)"

        return MessageStatus.SENT, "تم إرسال الرسالة بنجاح"
    except Exception as e:
        return MessageStatus.SUBMITTED, f"تم النقر على إرسال (تعذر قراءة مؤشر التسليم: {type(e).__name__})"


def _send_one(driver, phone: str, message: str) -> Tuple[bool, str, MessageStatus]:
    """
    إرسال رسالة واحدة لموظف محدد مع التدقيق والتحقق الفعلي.
    يُرجع: (نجاح؟، تفاصيل الحالة، كود الحالة).
    """
    # تنظيف رقم الهاتف (حذف المسافات وعلامة +)
    clean_phone = re.sub(r"\D", "", phone or "")
    if not clean_phone or len(clean_phone) < 7:
        return False, "رقم الهاتف غير صالح أو ناقص", MessageStatus.FAILED

    url = f"https://web.whatsapp.com/send?phone={clean_phone}&text={urllib.parse.quote(message)}&app_absent=0"
    driver.get(url)

    # معالجة أي alert مفاجئ
    try:
        alert = driver.switch_to.alert
        alert.accept()
    except Exception:
        pass

    # انتظار تحميل صندوق الكتابة أو نافذة رقم غير مسجل
    box = None
    deadline = time.time() + 45
    while time.time() < deadline:
        if campaign.stop_requested:
            return False, "تم إيقاف الحملة بواسطة المستخدم", MessageStatus.FAILED

        box = _find_first(driver, MESSAGE_BOX_XPATHS, timeout=0)
        if box:
            break

        if _page_has_invalid_hint(driver):
            _dismiss_popup(driver)
            return False, "الرقم غير مسجل على واتساب (Invalid Number)", MessageStatus.FAILED

        time.sleep(0.2)

    if not box:
        snap(driver, f"chat_load_timeout_{clean_phone}")
        return False, "لم يتم تحميل المحادثة (تحقق من اتصال الإنترنت وحالة الجلسة)", MessageStatus.FAILED

    # التأكد من وجود نص الرسالة في الصندوق وكتابته بسرعة إذا لزم الأمر
    time.sleep(0.4)
    try:
        current_text = (box.text or "").strip()
    except WebDriverException:
        current_text = ""

    if not current_text:
        try:
            box.click()
            # إدراج سريع وفوري للنص بالكامل عبر المتصفح
            driver.execute_script("""
                const el = arguments[0];
                const text = arguments[1];
                el.focus();
                document.execCommand('insertText', false, text);
            """, box, message)
            time.sleep(0.2)
            current_text = (box.text or "").strip()
            if not current_text:
                lines = message.split("\n")
                for i, line in enumerate(lines):
                    box.send_keys(line)
                    if i < len(lines) - 1:
                        box.send_keys(Keys.SHIFT, Keys.ENTER)
        except WebDriverException as e:
            return False, f"فشل إدخال نص الرسالة: {type(e).__name__}", MessageStatus.FAILED

    # النقر على زر الإرسال أو الضغط على ENTER
    btn = _find_first(driver, SEND_BUTTON_XPATHS, timeout=1.5)
    sent_clicked = False
    try:
        if btn and btn.is_displayed():
            btn.click()
            sent_clicked = True
        else:
            box.send_keys(Keys.ENTER)
            sent_clicked = True
    except WebDriverException:
        pass

    if not sent_clicked:
        try:
            box.send_keys(Keys.ENTER)
        except WebDriverException as e:
            return False, f"فشل النقر على زر الإرسال: {type(e).__name__}", MessageStatus.FAILED

    # انتظار التحقق من الإرسال الفعلي (فور تفريغ الصندوق أو ظهور علامات الإرسال)
    confirm_deadline = time.time() + 8
    while time.time() < confirm_deadline:
        time.sleep(0.4)
        try:
            if not (box.text or "").strip():
                return True, "تم الإرسال بنجاح", MessageStatus.SENT
        except WebDriverException:
            return True, "تم الإرسال بنجاح", MessageStatus.SENT

        status, detail = _inspect_delivery_status(driver)
        if status in (MessageStatus.SENT, MessageStatus.DELIVERED):
            return True, detail, status

    return True, "تم تقديم الرسالة بنجاح (Submitted)", MessageStatus.SUBMITTED


# ---------------------------------------------------------------------------
# إدارة الحملة الآلية (Campaign Execution)
# ---------------------------------------------------------------------------
def _interruptible_sleep(seconds: int, phase: str, label: str) -> bool:
    _set(phase=phase, countdown=seconds, countdown_total=seconds, message=label)
    remaining = seconds
    while remaining > 0:
        if campaign.stop_requested:
            return False
        if campaign.skip_requested:
            return True
        if campaign.paused:
            _set(message="⏸️ الإرسال متوقف مؤقتاً")
            time.sleep(0.5)
            continue
        time.sleep(1)
        remaining -= 1
        _set(countdown=remaining, message=label)
    return True


def _run_campaign(employees: List[dict], settings: dict):
    ctx = _ctx()
    try:
        log("=== بدء حملة إرسال جديدة ===")
        _set(phase="connecting", message="جاري تشغيل والتحقق من متصفح واتساب...")
        driver = _get_driver()

        # التحقق من أن جلسة واتساب مربوطة
        if not _is_logged_in(driver):
            _set(phase="waiting_login", message="في انتظار تسجيل الدخول في واتساب ويب...")
            deadline = time.time() + 300
            while time.time() < deadline:
                if campaign.stop_requested:
                    _set(phase="stopped", message="تم إيقاف الحملة", running=False)
                    return
                if _is_logged_in(driver):
                    break
                time.sleep(1.5)

            if not _is_logged_in(driver):
                _set(phase="error", message="انتهت مهلة تسجيل الدخول في واتساب ويب.", running=False)
                return

        num = _linked_number(driver) or ""
        _set(linked_number=num)
        wa_state.update(state=WhatsAppState.CONNECTED.value, logged_in=True, number=num)

        min_d = max(1, int(settings.get("min_delay", 5)))
        max_d = max(min_d, int(settings.get("max_delay", 10)))
        batch_size = max(1, int(settings.get("batch_size", 15)))
        batch_pause = max(10, int(settings.get("batch_pause", 120)))

        sent_in_batch = 0
        for pos, emp in enumerate(employees):
            if campaign.stop_requested:
                break

            emp_id = emp["id"]
            _set(
                current_id=emp_id,
                current_name=emp["name"],
                current_phone=emp["phone"],
                skip_requested=False,
            )

            with campaign.lock:
                campaign.results[emp_id] = {
                    "status": MessageStatus.PENDING.value,
                    "detail": "جاري الإرسال...",
                    "sent_at": None,
                }

            # فاصل زمني عشوائي قبل كل رسالة (باستثناء الأولى)
            if pos > 0:
                delay = random.randint(min_d, max_d)
                if not _interruptible_sleep(delay, "waiting", "انتظار الفاصل الزمني الآمن..."):
                    break

            if campaign.skip_requested:
                with campaign.lock:
                    campaign.results[emp_id] = {
                        "status": MessageStatus.SKIPPED.value,
                        "detail": "تم التخطي يدوياً بواسطة المستخدم",
                        "sent_at": None,
                    }
                continue

            # استراحة بين الدفعات
            if sent_in_batch >= batch_size:
                if not _interruptible_sleep(batch_pause, "batch_break", "🛑 استراحة تبريد لحماية الرقم..."):
                    break
                sent_in_batch = 0

            _set(phase="sending", message=f"جاري إرسال الرسالة إلى {emp['name']}...", countdown=0)

            try:
                ok, detail, status = _send_one(driver, emp["phone"], emp["message"])
            except WebDriverException as e:
                ok, detail, status = False, f"خطأ في المتصفح: {type(e).__name__}", MessageStatus.FAILED
                try:
                    driver = _get_driver()
                    if not _is_logged_in(driver):
                        break
                except Exception:
                    break

            log(f"[{pos+1}/{len(employees)}] موظف {emp['name']} ({emp['phone']}): {status.value} - {detail}")
            if not ok:
                snap(driver, f"emp_{emp_id}")

            with campaign.lock:
                campaign.results[emp_id] = {
                    "status": status.value if ok else MessageStatus.FAILED.value,
                    "detail": detail,
                    "sent_at": time.strftime("%H:%M:%S") if ok else None,
                }

            if ok:
                sent_in_batch += 1

        if campaign.stop_requested:
            _set(phase="stopped", message="تم إيقاف الحملة")
        else:
            _set(phase="done", message="🎊 اكتمل إرسال جميع الرسائل")

    except Exception as e:
        log(f"حدث خطأ أثناء تنفيذ الحملة: {e!r}")
        _set(phase="error", message=f"حدث خطأ أثناء تنفيذ الحملة: {str(e)}")
    finally:
        _set(running=False, paused=False)


def start_campaign(employees: List[dict], settings: dict) -> bool:
    with campaign.lock:
        if campaign.running:
            return False
        campaign.reset()
        campaign.running = True
        campaign.total = len(employees)
        campaign.phase = "connecting"
        campaign.message = "جاري التحضير لبدء الحملة..."
        for employee in employees:
            campaign.results[employee["id"]] = {
                "status": MessageStatus.PENDING.value,
                "detail": "قيد الانتظار",
                "sent_at": None,
            }
    t = _thread_for_user(current_user_id(), _run_campaign, employees, settings)
    with campaign.lock:
        campaign.thread = t
    return True


def pause_toggle() -> bool:
    with campaign.lock:
        campaign.paused = not campaign.paused
        return campaign.paused


def skip_current():
    with campaign.lock:
        campaign.skip_requested = True


def stop_campaign():
    with campaign.lock:
        campaign.stop_requested = True
        campaign.paused = False

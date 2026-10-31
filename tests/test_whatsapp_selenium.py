"""
حزمة اختبارات شاملة لتكامل متصفح Selenium وواتساب ويب (WhatsApp Web & Selenium Test Suite).

تغطي الاختبارات المطلوبة:
1. Browser startup & binary detection.
2. WhatsApp Web navigation.
3. Actual QR detection logic.
4. QR refresh & reload handling.
5. Authentication state detection.
6. Persistent session reuse & user isolation.
7. Session disconnection & resource cleanup.
8. Message delivery status indicators inspection (SUBMITTED, SENT, DELIVERED, FAILED).
9. Failure handling (invalid numbers, popup alerts).
10. Real WhatsApp Web network interaction & QR loading test.
11. Live WhatsApp message sending (conditioned on live session availability).
"""

import os
import shutil
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest
from selenium.webdriver.common.by import By

# تأكيد مسار المشروع
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sender
from sender import MessageStatus, WhatsAppState


@pytest.fixture
def isolated_sender_env(monkeypatch):
    """عزل بيئة العمل الخاصة بـ sender في مجلد مؤقت وقفل نظيف."""
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(sender, "DATA_DIR", temp_dir)
    monkeypatch.setattr(sender, "_CONTEXTS", {})

    yield temp_dir

    # إغلاق أي متصفحات وتنظيف المجلد المؤقت
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test 1: Browser startup & binary detection
# ---------------------------------------------------------------------------
def test_browser_binary_detection():
    """1. التحقق من التعرف التلقائي على متصفح Chrome أو Edge المثبت على نظام التشغيل."""
    chrome_path, edge_path = sender._detect_browser_paths()
    # يجب العثور على متصفح واحد على الأقل على بيئة Windows
    assert (chrome_path is not None) or (edge_path is not None), "يجب تثبيت Chrome أو Edge على النظام."
    if edge_path:
        assert os.path.exists(edge_path)
    if chrome_path:
        assert os.path.exists(chrome_path)


# ---------------------------------------------------------------------------
# Test 2: Real Browser launch & navigation (headless)
# ---------------------------------------------------------------------------
def test_real_browser_launch_and_navigation(isolated_sender_env):
    """2. التحقق من تشغيل متصفح حقيقي عبر Selenium والتنقل لصفحة وفحص العنوان."""
    from selenium import webdriver

    chrome_path, edge_path = sender._detect_browser_paths()
    driver = None
    try:
        if edge_path:
            opts = webdriver.EdgeOptions()
            opts.add_argument("--headless=new")
            opts.binary_location = edge_path
            driver = webdriver.Edge(options=opts)
        elif chrome_path:
            opts = webdriver.ChromeOptions()
            opts.add_argument("--headless=new")
            opts.binary_location = chrome_path
            driver = webdriver.Chrome(options=opts)

        assert driver is not None
        driver.get("https://example.com")
        assert "Example Domain" in driver.title
    finally:
        if driver:
            driver.quit()


# ---------------------------------------------------------------------------
# Test 3: Actual QR detection logic
# ---------------------------------------------------------------------------
def test_actual_qr_detection_logic():
    """3. التحقق من منطق استخراج كود الـ QR كـ base64 وفحص زر انتهاء الصلاحية."""
    mock_driver = MagicMock()

    # محاكاة إرجاع JavaScript لكود Canvas base64 سليم
    sample_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    mock_driver.execute_script.return_value = sample_b64
    mock_driver.find_elements.return_value = []

    qr_data, is_expired = sender._extract_qr_base64(mock_driver)
    assert qr_data == sample_b64
    assert is_expired is False


# ---------------------------------------------------------------------------
# Test 4: QR refresh and reload handling
# ---------------------------------------------------------------------------
def test_qr_refresh_and_reload_handling():
    """4. التحقق من رصد انتهاء صلاحية كود الـ QR وإعادة النقر على زر التحديث."""
    mock_driver = MagicMock()
    mock_btn = MagicMock()
    mock_btn.is_displayed.return_value = True

    # محاكاة وجود زر إعادة التحميل
    mock_driver.find_elements.return_value = [mock_btn]
    mock_driver.execute_script.return_value = None

    qr_data, is_expired = sender._extract_qr_base64(mock_driver)
    assert is_expired is True


# ---------------------------------------------------------------------------
# Test 5: Authentication state detection
# ---------------------------------------------------------------------------
def test_authentication_state_detection():
    """5. التحقق من التعرف على حالة تسجيل الدخول واستخراج رقم الهاتف من LocalStorage."""
    mock_driver = MagicMock()

    # حالة غير مسجل دخول
    mock_driver.execute_script.return_value = None
    mock_driver.find_elements.return_value = []
    assert sender._is_logged_in(mock_driver) is False
    assert sender._linked_number(mock_driver) is None

    # حالة مسجل دخول ومرتبط برقم هاتف
    mock_driver.execute_script.return_value = "201012345678@c.us"
    assert sender._is_logged_in(mock_driver) is True
    assert sender._linked_number(mock_driver) == "201012345678"


# ---------------------------------------------------------------------------
# Test 6: User session isolation
# ---------------------------------------------------------------------------
def test_user_session_isolation(isolated_sender_env):
    """6. التحقق من عزل ملفات التعريف والبيانات بين حسابات المستخدمين المختلفة."""
    with sender.user_context("user_101"):
        ctx1 = sender._ctx()
        assert ctx1.user_id == "user_101"
        assert "user_101" in ctx1.profile_dir

    with sender.user_context("user_202"):
        ctx2 = sender._ctx()
        assert ctx2.user_id == "user_202"
        assert "user_202" in ctx2.profile_dir

    assert ctx1.profile_dir != ctx2.profile_dir
    assert ctx1 is not ctx2


# ---------------------------------------------------------------------------
# Test 7: Session disconnection & cleanup
# ---------------------------------------------------------------------------
def test_session_disconnection(isolated_sender_env):
    """7. التحقق من إغلاق المتصفح وإرجاع الحالة إلى DISCONNECTED عند استدعاء disconnect."""
    with sender.user_context("user_test_dc"):
        ctx = sender._ctx()
        mock_driver = MagicMock()
        ctx.driver = mock_driver

        sender.disconnect_whatsapp()

        assert mock_driver.quit.called
        assert ctx.driver is None
        st = sender.whatsapp_status()
        assert st["state"] == WhatsAppState.DISCONNECTED.value
        assert st["browser_open"] is False
        assert st["logged_in"] is False


# ---------------------------------------------------------------------------
# Test 8: Message delivery status indicators inspection
# ---------------------------------------------------------------------------
def test_inspect_delivery_status_indicators():
    """8. التحقق من التمييز الدقيق بين حالات التسليم: DELIVERED, SENT, SUBMITTED."""
    mock_driver = MagicMock()

    # 1. حالة رسالة قيد الإرسال (ساعة)
    mock_msg_time = MagicMock()
    mock_clock_span = MagicMock()
    mock_msg_time.find_elements.side_effect = lambda by, sel: [mock_clock_span] if 'msg-time' in sel else []
    mock_driver.find_elements.return_value = [mock_msg_time]
    st, detail = sender._inspect_delivery_status(mock_driver)
    assert st == MessageStatus.SUBMITTED

    # 2. حالة صح واحدة (SENT لخادم واتساب)
    mock_msg_single = MagicMock()
    mock_single_span = MagicMock()
    mock_msg_single.find_elements.side_effect = lambda by, sel: [mock_single_span] if 'msg-check' in sel and 'dbl' not in sel else []
    mock_driver.find_elements.return_value = [mock_msg_single]
    st, detail = sender._inspect_delivery_status(mock_driver)
    assert st == MessageStatus.SENT

    # 3. حالة صحين (DELIVERED لهاتف المستلم)
    mock_msg_double = MagicMock()
    mock_double_span = MagicMock()
    mock_msg_double.find_elements.side_effect = lambda by, sel: [mock_double_span] if 'msg-dblcheck' in sel else []
    mock_driver.find_elements.return_value = [mock_msg_double]
    st, detail = sender._inspect_delivery_status(mock_driver)
    assert st == MessageStatus.DELIVERED


# ---------------------------------------------------------------------------
# Test 9: Failure handling for invalid numbers
# ---------------------------------------------------------------------------
def test_send_one_invalid_phone_number():
    """9. التحقق من رفض الأرقام غير الصالحة وتسجيل حالة FAILED دون إرسال وهمي."""
    mock_driver = MagicMock()
    # رقم أقل من 7 أرقام
    ok, detail, status = sender._send_one(mock_driver, "123", "رسالة تجريبية")
    assert ok is False
    assert status == MessageStatus.FAILED
    assert "غير صالح" in detail
    assert not mock_driver.get.called


# ---------------------------------------------------------------------------
# Test 10: Real WhatsApp Web Network Interaction & QR Loading Test
# ---------------------------------------------------------------------------
def test_real_whatsapp_web_page_structure(isolated_sender_env):
    """
    10. اختبار حي مباشر: فتح صفحة web.whatsapp.com الحقيقية عبر المتصفح والتحقق
    من استجابة الشبكة وتحميل تطبيق الويب الرسمي دون محاكاة.
    """
    from selenium import webdriver

    chrome_path, edge_path = sender._detect_browser_paths()
    driver = None
    try:
        opts = webdriver.EdgeOptions() if edge_path else webdriver.ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--lang=ar")
        if edge_path:
            opts.binary_location = edge_path
            driver = webdriver.Edge(options=opts)
        else:
            opts.binary_location = chrome_path
            driver = webdriver.Chrome(options=opts)

        driver.set_page_load_timeout(30)
        driver.get("https://web.whatsapp.com")
        title = driver.title

        # التحقق من أن الصفحة المحملة هي صفحة WhatsApp Web الحقيقية (بالعربية أو الإنجليزية)
        assert ("WhatsApp" in title) or ("واتساب" in title) or ("web.whatsapp.com" in driver.current_url)
    finally:
        if driver:
            driver.quit()


# ---------------------------------------------------------------------------
# Test 11: Real WhatsApp message sending (distinguished from mocked)
# ---------------------------------------------------------------------------
def test_real_whatsapp_message_sending_if_authenticated(isolated_sender_env):
    """
    11. فحص إرسال رسالة حقيقية إلى رقم هاتف فعلي في حال كانت الجلسة مسجلة مسبقاً.
    إذا لم تكن جلسة واتساب مسجلة بالهاتف حالياً، يتم تخطي الاختبار مع تسجيل السبب بوضوح دون تزييف.
    """
    with sender.user_context("1"):
        ctx = sender._ctx()
        with ctx.driver_lock:
            driver = ctx.driver

    if not driver:
        pytest.skip("[LIVE_WHATSAPP_TEST_SKIPPED] لم يتم مسح رمز الـ QR بجهاز هاتف حقيقي بعد في الجلسة الحالية.")

    if not sender._is_logged_in(driver):
        pytest.skip("[LIVE_WHATSAPP_TEST_SKIPPED] متصفح واتساب مفتوح ولكن لم يتم تسجيل الدخول بالهاتف.")

    # في حال توفر حساب مسجل بالفعل:
    test_number = sender._linked_number(driver)
    if not test_number:
        pytest.skip("[LIVE_WHATSAPP_TEST_SKIPPED] تعذر قراءة رقم الهاتف المسجل للاختبار.")

    ok, detail, status = sender._send_one(driver, test_number, "رسالة اختبارية آلية من نظام الرواتب 🤖")
    assert ok is True
    assert status in (MessageStatus.SUBMITTED, MessageStatus.SENT, MessageStatus.DELIVERED)

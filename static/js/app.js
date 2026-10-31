// ===========================================================================
// Payroll Sender – WhatsApp Payroll Automation System v2.5
// ===========================================================================

// Auto-reload on session expiration (401)
(function () {
    const _fetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
        const res = await _fetch(...args);
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        if (res.status === 401 && !url.includes('/api/auth/')) {
            location.reload();
        }
        return res;
    };
})();

// Web Audio Chime for alerts and completion
function playChime(type = 'success') {
    try {
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = audioCtx.createOscillator();
        const gainNode = audioCtx.createGain();

        if (type === 'success') {
            osc.frequency.setValueAtTime(587.33, audioCtx.currentTime); // D5
            osc.frequency.exponentialRampToValueAtTime(880, audioCtx.currentTime + 0.15); // A5
            gainNode.gain.setValueAtTime(0.15, audioCtx.currentTime);
            gainNode.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.35);
            osc.connect(gainNode);
            gainNode.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + 0.35);
        }
    } catch (e) {
        // AudioContext not supported or blocked by browser policy
    }
}

// ---------------------------------------------------------------------------
// Global Application State
// ---------------------------------------------------------------------------
let employeeQueue = [];
let isDispatching = false;
let isPaused = false;
let waStatus = { logged_in: false, state: 'DISCONNECTED' };
let statusPoller = null;
let waPoller = null;
let gridSyncTimer = null;
let editingEmpId = null;
let currentWaTab = 'qr';
let pairingTimer = null;

// Theme Toggle (Light / Luxury Dark)
function toggleTheme() {
    const isDark = document.documentElement.classList.toggle('dark');
    try {
        localStorage.setItem('payroll_theme', isDark ? 'dark' : 'light');
    } catch (e) {}
    updateThemeIcon(isDark);
}

function updateThemeIcon(isDark) {
    const icon = document.getElementById('themeToggleIcon');
    if (icon) {
        if (isDark) {
            icon.className = 'fas fa-sun text-sm text-amber-400';
        } else {
            icon.className = 'fas fa-moon text-sm text-slate-700';
        }
    }
}
window.toggleTheme = toggleTheme;

// Base columns in sheetGrid
const BASE_COLS = ['اسم الموظف', 'رقم الهاتف', 'إجمالي المرتب', 'الاستقطاعات', 'صافي المرتب', 'الصرفية', 'ملاحظات'];
let GRID_COLS = [...BASE_COLS];
const NUMERIC_COLS = new Set([2, 3, 4]); // Gross, Deductions, Net

// Default Message Template
const DEFAULT_TEMPLATE = `أهلاً بك أ/ {الاسم} 🌹

بيان مفردات: {الصرفية}
ــــــــــــــــــــــــــــــــــــــــ
💵 إجمالي المرتب: {المرتب} ج.م
🔻 إجمالي الاستقطاعات: {الاستقطاع} ج.م
ــــــــــــــــــــــــــــــــــــــــ
💰 صافي المرتب المستحق: {الصافي} ج.م
ــــــــــــــــــــــــــــــــــــــــ
{ملاحظات}

مع تحيات إدارة الموارد البشرية والمالية`;

// ---------------------------------------------------------------------------
// Initialization & State Restoration
// ---------------------------------------------------------------------------
let appInitialized = false;
let listenersAttached = false;

document.addEventListener('DOMContentLoaded', () => {
    if (appInitialized) return;
    appInitialized = true;

    initElements();
    initSheetGrid();
    setupEventListeners();
    pollWaStatus(2500);
    restoreCampaignStateFromBackend();
});

function initElements() {
    updateThemeIcon(document.documentElement.classList.contains('dark'));

    // Check previously cached WhatsApp login state to avoid red badge flash on reload
    try {
        if (localStorage.getItem('payroll_wa_logged_in') === '1') {
            renderWaBadge({ state: 'CONNECTED', logged_in: true });
        }
    } catch (e) {}

    const templateInput = document.getElementById('messageTemplate');
    if (templateInput) {
        const savedTemplate = localStorage.getItem('payroll_message_template');
        if (savedTemplate && savedTemplate.trim()) {
            templateInput.value = savedTemplate;
        } else if (!templateInput.value.trim()) {
            templateInput.value = DEFAULT_TEMPLATE;
        }
        templateInput.addEventListener('input', () => {
            try { localStorage.setItem('payroll_message_template', templateInput.value); } catch (e) {}
            updateTemplatePreview();
        });
    }

    const monthInput = document.getElementById('salaryMonth');
    if (monthInput) {
        const savedMonth = localStorage.getItem('payroll_salary_month');
        if (savedMonth && savedMonth.trim()) {
            monthInput.value = savedMonth;
        } else if (!monthInput.value.trim()) {
            const today = new Date();
            const months = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"];
            monthInput.value = `مرتب شهر ${months[today.getMonth()]} ${today.getFullYear()}`;
        }
        monthInput.addEventListener('input', () => {
            try { localStorage.setItem('payroll_salary_month', monthInput.value); } catch (e) {}
            updateTemplatePreview();
        });
    }

    const minDelay = document.getElementById('minDelaySec');
    const maxDelay = document.getElementById('maxDelaySec');
    if (minDelay) {
        const savedMin = localStorage.getItem('payroll_min_delay');
        if (savedMin) minDelay.value = savedMin;
        minDelay.addEventListener('change', () => {
            try { localStorage.setItem('payroll_min_delay', minDelay.value); } catch (e) {}
        });
    }
    if (maxDelay) {
        const savedMax = localStorage.getItem('payroll_max_delay');
        if (savedMax) maxDelay.value = savedMax;
        maxDelay.addEventListener('change', () => {
            try { localStorage.setItem('payroll_max_delay', maxDelay.value); } catch (e) {}
        });
    }

    updateTemplatePreview();
}

function setupEventListeners() {
    if (listenersAttached) return;
    listenersAttached = true;

    // File upload button trigger
    const fileInput = document.getElementById('excelFileInput');
    const uploadBtn = document.getElementById('btnUploadTrigger');
    if (uploadBtn && fileInput) {
        uploadBtn.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handleFileUpload(e.target.files[0]);
                e.target.value = ''; // Reset input
            }
        });
    }

    // Template live updates
    const previewSelect = document.getElementById('previewEmpSelect');
    if (previewSelect) previewSelect.addEventListener('change', updateTemplatePreview);

    // Tag chip buttons
    document.querySelectorAll('.tag-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tag = btn.getAttribute('data-tag');
            if (tag) insertTagAtCursor(tag);
        });
    });

    // Campaign action buttons
    document.getElementById('startDispatchBtn')?.addEventListener('click', startDispatcher);
    document.getElementById('pauseDispatchBtn')?.addEventListener('click', togglePauseDispatcher);
    document.getElementById('skipDispatchBtn')?.addEventListener('click', skipCurrentEmployee);
    document.getElementById('stopDispatchBtn')?.addEventListener('click', stopDispatcher);
    document.getElementById('exportReportBtn')?.addEventListener('click', exportReportExcel);

    // Logout
    document.getElementById('logoutBtn')?.addEventListener('click', async () => {
        try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (e) {}
        location.reload();
    });
}

// ---------------------------------------------------------------------------
// 1. WhatsApp Connection & Status (Dual Linking: QR + Phone Pairing)
// ---------------------------------------------------------------------------
function openWhatsAppConnectModal() {
    openWhatsAppWebQR();
}
window.openWhatsAppConnectModal = openWhatsAppConnectModal;

async function openWhatsAppWebQR() {
    const btn = document.getElementById('waConnectBtn');
    if (btn) btn.disabled = true;
    try {
        const res = await fetch('/api/whatsapp/connect', { method: 'POST' });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            alert('❌ ' + (data.detail || 'تعذر بدء ربط واتساب'));
            return;
        }
        renderWaBadge({ state: 'STARTING', browser_open: true });
        pollWaStatus(1500);
    } catch (e) {
        alert('❌ تعذر الاتصال بالسيرفر');
    } finally {
        if (btn) btn.disabled = false;
    }
}
window.openWhatsAppWebQR = openWhatsAppWebQR;

async function disconnectWhatsApp() {
    if (!confirm('هل تريد بالتأكيد قطع اتصال واتساب وإغلاق جلسة المتصفح؟')) return;
    const btn = document.getElementById('waDisconnectBtn');
    if (btn) btn.disabled = true;
    try {
        await fetch('/api/whatsapp/disconnect', { method: 'POST' });
        renderWaBadge({ state: 'DISCONNECTED', logged_in: false });
    } catch (e) {
        alert('❌ حدث خطأ أثناء قطع الاتصال');
    } finally {
        if (btn) btn.disabled = false;
    }
}
window.disconnectWhatsApp = disconnectWhatsApp;

async function reloadWhatsAppQR() {
    try {
        const res = await fetch('/api/whatsapp/reload-qr', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (data.status === 'ok') {
            document.getElementById('waQrExpiredOverlay')?.classList.add('hidden');
            document.getElementById('waQrLoading')?.classList.remove('hidden');
            document.getElementById('waQrImg')?.classList.add('hidden');
        }
    } catch (e) {
        alert('❌ تعذر تحديث رمز الـ QR');
    }
}
window.reloadWhatsAppQR = reloadWhatsAppQR;

function switchWaLinkingTab(tab) {
    currentWaTab = tab;
    const tabQr = document.getElementById('tabWaQr');
    const tabPhone = document.getElementById('tabWaPhone');
    const panelQr = document.getElementById('panelWaQr');
    const panelPhone = document.getElementById('panelWaPhone');

    if (tab === 'qr') {
        if (tabQr) {
            tabQr.className = 'flex-1 py-2 text-xs font-bold rounded-lg text-slate-800 dark:text-white bg-white dark:bg-zinc-700 shadow-sm transition';
        }
        if (tabPhone) {
            tabPhone.className = 'flex-1 py-2 text-xs font-bold rounded-lg text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-white transition';
        }
        if (panelQr) panelQr.classList.remove('hidden');
        if (panelPhone) panelPhone.classList.add('hidden');
    } else {
        if (tabPhone) {
            tabPhone.className = 'flex-1 py-2 text-xs font-bold rounded-lg text-slate-800 dark:text-white bg-white dark:bg-zinc-700 shadow-sm transition';
        }
        if (tabQr) {
            tabQr.className = 'flex-1 py-2 text-xs font-bold rounded-lg text-slate-500 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-white transition';
        }
        if (panelPhone) panelPhone.classList.remove('hidden');
        if (panelQr) panelQr.classList.add('hidden');
    }
}
window.switchWaLinkingTab = switchWaLinkingTab;

async function requestPhonePairingCode() {
    const phoneInput = document.getElementById('waPairingPhone');
    const countrySelect = document.getElementById('waPairingCountry');
    const btn = document.getElementById('btnGeneratePairingCode');
    const errEl = document.getElementById('waPairingError');

    const phone = (phoneInput?.value || '').trim();
    const country = countrySelect?.value || '20';

    if (!phone) {
        if (errEl) {
            errEl.textContent = 'يرجى إدخال رقم الهاتف أولاً.';
            errEl.classList.remove('hidden');
        }
        return;
    }

    if (errEl) errEl.classList.add('hidden');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin ml-1.5"></i> جاري إنشاء الكود من واتساب ويب...';
    }

    try {
        const res = await fetch('/api/whatsapp/pair-phone', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phone: phone, country_code: country })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data.detail || 'تعذر إنشاء كود الربط');
        }

        renderPairingCode(data.pairing_code);
        pollWaStatus(1500);
    } catch (e) {
        if (errEl) {
            errEl.textContent = '❌ ' + e.message;
            errEl.classList.remove('hidden');
        }
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-key ml-1.5"></i> إنشاء كود الربط';
        }
    }
}
window.requestPhonePairingCode = requestPhonePairingCode;

function renderPairingCode(code) {
    if (!code) return;
    const inputStep = document.getElementById('phoneInputStep');
    const displayStep = document.getElementById('phoneCodeDisplayStep');
    const slotsContainer = document.getElementById('pairingCodeSlotsContainer');
    const countdownEl = document.getElementById('pairingCountdown');

    if (inputStep) inputStep.classList.add('hidden');
    if (displayStep) displayStep.classList.remove('hidden');

    const clean = code.replace(/[^A-Za-z0-9]/g, '').toUpperCase();
    if (slotsContainer) {
        slotsContainer.innerHTML = '';
        clean.split('').forEach((ch, i) => {
            if (i === 4) {
                const dash = document.createElement('span');
                dash.className = 'text-2xl font-black text-slate-400 dark:text-zinc-600 px-1 select-none';
                dash.textContent = '—';
                slotsContainer.appendChild(dash);
            }
            const slot = document.createElement('div');
            slot.className = 'pairing-code-slot';
            slot.textContent = ch;
            slotsContainer.appendChild(slot);
        });
    }

    let remaining = 180;
    if (countdownEl) countdownEl.textContent = remaining;
    clearInterval(pairingTimer);
    pairingTimer = setInterval(() => {
        remaining--;
        if (countdownEl) countdownEl.textContent = remaining;
        if (remaining <= 0) {
            clearInterval(pairingTimer);
            if (countdownEl) countdownEl.textContent = '0 (منتهي)';
        }
    }, 1000);
}

function copyPairingCode() {
    const slots = document.querySelectorAll('#pairingCodeSlotsContainer .pairing-code-slot');
    const chars = Array.from(slots).map(s => s.textContent.trim()).join('');
    if (!chars) return;
    const formatted = chars.length === 8 ? `${chars.slice(0, 4)}-${chars.slice(4)}` : chars;
    navigator.clipboard.writeText(formatted).then(() => {
        alert(`✅ تم نسخ كود الربط: ${formatted}\nأدخله الآن في تطبيق واتساب بهاتفك.`);
    }).catch(() => {
        prompt('انسخ كود الربط يدوياً:', formatted);
    });
}
window.copyPairingCode = copyPairingCode;

async function cancelPhonePairing() {
    clearInterval(pairingTimer);
    try {
        await fetch('/api/whatsapp/cancel-pairing', { method: 'POST' });
    } catch (e) {}

    const inputStep = document.getElementById('phoneInputStep');
    const displayStep = document.getElementById('phoneCodeDisplayStep');
    if (inputStep) inputStep.classList.remove('hidden');
    if (displayStep) displayStep.classList.add('hidden');
    switchWaLinkingTab('qr');
}
window.cancelPhonePairing = cancelPhonePairing;

function renderWaBadge(st) {
    waStatus = st;
    const badge = document.getElementById('waStatusBadge');
    const connectBtn = document.getElementById('waConnectBtn');
    const disconnectBtn = document.getElementById('waDisconnectBtn');
    const connectBox = document.getElementById('waConnectBox') || document.getElementById('waQrCard');
    const qrImg = document.getElementById('waQrImg');
    const qrLoading = document.getElementById('waQrLoading');
    const qrExpired = document.getElementById('waQrExpiredOverlay');
    const statusHelp = document.getElementById('waStatusHelp');

    if (!badge) return;
    const base = 'px-2.5 py-0.5 rounded-full text-xs font-semibold border ';
    const state = st.state || (st.logged_in ? 'CONNECTED' : (st.connecting ? 'STARTING' : 'DISCONNECTED'));

    if (state === 'CONNECTED' || st.logged_in) {
        try { localStorage.setItem('payroll_wa_logged_in', '1'); } catch (e) {}
        badge.className = base + 'bg-emerald-100 dark:bg-emerald-950/60 text-emerald-800 dark:text-emerald-400 border-emerald-200 dark:border-emerald-800';
        badge.innerHTML = '<i class="fas fa-check-circle text-emerald-600 dark:text-emerald-400 ml-1"></i> متصل' + (st.number ? ' (+' + st.number + ')' : '');
        if (connectBtn) connectBtn.classList.add('hidden');
        if (disconnectBtn) disconnectBtn.classList.remove('hidden');
        if (connectBox) connectBox.classList.add('hidden');
        if (statusHelp) statusHelp.textContent = 'جلسة واتساب ويب متصلة وجاهزة لإرسال الرسائل الحقيقية للموظفين.';
    } else if (state === 'WAITING_FOR_QR') {
        badge.className = base + 'bg-amber-100 dark:bg-amber-950/60 text-amber-800 dark:text-amber-400 border-amber-200 dark:border-amber-800';
        badge.innerHTML = '<i class="fas fa-qrcode text-amber-600 dark:text-amber-400 ml-1"></i> في انتظار مسح الـ QR...';
        if (connectBtn) connectBtn.classList.add('hidden');
        if (disconnectBtn) disconnectBtn.classList.remove('hidden');
        if (connectBox) connectBox.classList.remove('hidden');

        if (st.qr_image) {
            if (qrLoading) qrLoading.classList.add('hidden');
            if (qrImg) {
                qrImg.src = st.qr_image;
                qrImg.classList.remove('hidden');
            }
        } else {
            if (qrLoading) qrLoading.classList.remove('hidden');
            if (qrImg) qrImg.classList.add('hidden');
        }

        if (qrExpired) {
            if (st.qr_expired) qrExpired.classList.remove('hidden');
            else qrExpired.classList.add('hidden');
        }
        if (statusHelp) statusHelp.textContent = 'امسح رمز الـ QR المعروض أدناه من تطبيق واتساب على هاتفك.';
    } else if (state === 'PAIRING_CODE_READY' || (st.pairing_code && !st.logged_in)) {
        badge.className = base + 'bg-blue-100 dark:bg-blue-950/60 text-blue-800 dark:text-blue-400 border-blue-200 dark:border-blue-800';
        badge.innerHTML = '<i class="fas fa-key text-blue-600 dark:text-blue-400 ml-1"></i> كود الربط جاهز: ' + (st.pairing_code || '');
        if (connectBtn) connectBtn.classList.add('hidden');
        if (disconnectBtn) disconnectBtn.classList.remove('hidden');
        if (connectBox) connectBox.classList.remove('hidden');
        switchWaLinkingTab('phone');
        renderPairingCode(st.pairing_code);
        if (statusHelp) statusHelp.textContent = 'أدخل كود الربط الظاهر في واتساب على هاتفك لتأكيد الاتصال.';
    } else if (state === 'WAITING_FOR_PAIRING_CODE') {
        badge.className = base + 'bg-blue-100 dark:bg-blue-950/60 text-blue-800 dark:text-blue-400 border-blue-200 dark:border-blue-800';
        badge.innerHTML = '<i class="fas fa-spinner fa-spin text-blue-600 dark:text-blue-400 ml-1"></i> جاري إنشاء كود الربط...';
        if (connectBtn) connectBtn.classList.add('hidden');
        if (disconnectBtn) disconnectBtn.classList.remove('hidden');
        if (connectBox) connectBox.classList.remove('hidden');
    } else if (state === 'AUTHENTICATING') {
        badge.className = base + 'bg-blue-100 dark:bg-blue-950/60 text-blue-800 dark:text-blue-400 border-blue-200 dark:border-blue-800';
        badge.innerHTML = '<i class="fas fa-spinner fa-spin text-blue-600 dark:text-blue-400 ml-1"></i> جاري التوثيق...';
        if (connectBtn) connectBtn.classList.add('hidden');
        if (disconnectBtn) disconnectBtn.classList.remove('hidden');
        if (connectBox) connectBox.classList.remove('hidden');
        if (qrImg) qrImg.classList.add('hidden');
        if (qrLoading) {
            qrLoading.classList.remove('hidden');
            qrLoading.innerHTML = '<i class="fas fa-circle-check text-3xl text-emerald-600 mb-2"></i><div class="text-xs font-bold text-slate-700 dark:text-zinc-200">تم مسح الكود! جاري تحميل المحادثات...</div>';
        }
    } else if (state === 'STARTING') {
        badge.className = base + 'bg-slate-100 dark:bg-zinc-800 text-slate-800 dark:text-zinc-200 border-slate-200 dark:border-zinc-700';
        badge.innerHTML = '<i class="fas fa-spinner fa-spin text-slate-600 dark:text-zinc-400 ml-1"></i> جاري تشغيل المتصفح...';
        if (connectBtn) connectBtn.classList.add('hidden');
        if (disconnectBtn) disconnectBtn.classList.remove('hidden');
        if (connectBox) connectBox.classList.remove('hidden');
        if (qrLoading) {
            qrLoading.classList.remove('hidden');
            qrLoading.innerHTML = '<i class="fas fa-spinner fa-spin text-3xl text-emerald-600 mb-2"></i><div class="text-xs font-bold text-slate-700 dark:text-zinc-200">جاري تشغيل المتصفح وفتح واتساب ويب...</div>';
        }
    } else if (state === 'ERROR') {
        badge.className = base + 'bg-rose-100 dark:bg-rose-950/60 text-rose-800 dark:text-rose-400 border-rose-200 dark:border-rose-800';
        badge.innerHTML = '<i class="fas fa-circle-exclamation text-rose-600 ml-1"></i> خطأ في الاتصال';
        if (connectBtn) {
            connectBtn.classList.remove('hidden');
            connectBtn.innerHTML = '<i class="fas fa-rotate-right ml-1"></i> إعادة المحاولة';
        }
        if (disconnectBtn) disconnectBtn.classList.add('hidden');
        if (connectBox) connectBox.classList.add('hidden');
    } else {
        try { localStorage.removeItem('payroll_wa_logged_in'); } catch (e) {}
        badge.className = base + 'bg-rose-100 dark:bg-rose-950/60 text-rose-800 dark:text-rose-400 border-rose-200 dark:border-rose-800';
        badge.innerHTML = '<i class="fas fa-circle-xmark ml-1"></i> غير متصل';
        if (connectBtn) {
            connectBtn.classList.remove('hidden');
            connectBtn.innerHTML = '<i class="fas fa-link ml-1"></i> بدء ربط واتساب';
        }
        if (disconnectBtn) disconnectBtn.classList.add('hidden');
        if (connectBox) connectBox.classList.add('hidden');
        if (statusHelp) statusHelp.textContent = 'اختر طريقة الاتصال المناسبة لك: مسح رمز الـ QR بالكاميرا أو إدخال رقم هاتفك وتأكيد الكود.';
    }
}

function pollWaStatus(intervalMs = 2500) {
    clearInterval(waPoller);
    const tick = async () => {
        try {
            const res = await fetch('/api/whatsapp/status');
            if (res.ok) {
                const st = await res.json();
                renderWaBadge(st);
                if (st.logged_in && intervalMs < 6000) {
                    pollWaStatus(6000);
                }
            }
        } catch (e) {}
    };
    tick();
    waPoller = setInterval(tick, intervalMs);
}

async function restoreCampaignStateFromBackend() {
    try {
        const res = await fetch('/api/campaign/status');
        if (!res.ok) return;
        const st = await res.json();

        if (st.running) {
            isDispatching = true;
            isPaused = !!st.paused;

            document.getElementById('startDispatchBtn')?.classList.add('hidden');
            document.getElementById('pauseDispatchBtn')?.classList.remove('hidden');
            document.getElementById('stopDispatchBtn')?.classList.remove('hidden');
            document.getElementById('skipDispatchBtn')?.classList.remove('hidden');
            document.getElementById('campaignActiveBanner')?.classList.remove('hidden');

            const pauseBtn = document.getElementById('pauseDispatchBtn');
            if (pauseBtn) {
                pauseBtn.innerHTML = isPaused
                    ? '<i class="fas fa-play"></i> استئناف'
                    : '<i class="fas fa-pause"></i> إيقاف مؤقت';
            }

            clearInterval(statusPoller);
            statusPoller = setInterval(pollCampaignStatus, 1000);
            pollCampaignStatus();
        } else if (st.results && Object.keys(st.results).length > 0) {
            Object.entries(st.results).forEach(([id, r]) => {
                const emp = employeeQueue.find(e => e.id === Number(id));
                if (!emp) return;
                if (r.status === 'sent') {
                    emp.status = 'sent';
                    emp.statusText = 'تم الإرسال بنجاح';
                    emp.sentAt = r.sent_at || '';
                    emp.errorDetail = '';
                } else if (r.status === 'failed') {
                    emp.status = 'failed';
                    emp.statusText = 'فشل: ' + (r.detail || 'تعذر الإرسال');
                    emp.errorDetail = r.detail || '';
                } else if (r.status === 'skipped') {
                    emp.status = 'failed';
                    emp.statusText = 'تم التخطي يدوياً';
                }
                updateEmployeeRowStatus(emp);
            });
            updateCounters();
        }
    } catch (e) {}
}

// ---------------------------------------------------------------------------
// 2. Main Payroll Data Grid (Sheet)
// ---------------------------------------------------------------------------
function extraColNames() {
    return GRID_COLS.slice(BASE_COLS.length);
}

function gridAddRow(vals = []) {
    const tbody = document.getElementById('sheetGridBody');
    if (!tbody) return;
    const n = tbody.rows.length;
    const tr = tbody.insertRow();
    tr.className = 'hover:bg-slate-50 dark:hover:bg-zinc-800/50 transition border-b border-slate-200 dark:border-zinc-800';

    // # Number cell
    const num = tr.insertCell();
    num.className = 'sheet-num-cell px-2 py-1.5';
    num.textContent = n + 1;

    // Data cells
    GRID_COLS.forEach((_, c) => {
        const td = tr.insertCell();
        td.contentEditable = 'true';
        td.dataset.c = c;
        td.className = 'sheet-cell';
        if (vals[c] !== undefined && vals[c] !== null) {
            td.textContent = String(vals[c]);
        }
    });

    // Delete Row button cell
    const delCell = tr.insertCell();
    delCell.className = 'sheet-del-cell';
    delCell.innerHTML = `<button type="button" onclick="gridDeleteRow(this)" class="inline-flex items-center justify-center w-6 h-6 rounded-md text-slate-400 hover:text-rose-600 hover:bg-rose-50 dark:hover:text-rose-400 dark:hover:bg-rose-950/40 transition text-xs" title="حذف هذا الصف"><i class="fas fa-trash-can text-xs"></i></button>`;
}

function gridEnsureRows(count) {
    const tbody = document.getElementById('sheetGridBody');
    if (!tbody) return;
    while (tbody.rows.length < count) {
        gridAddRow();
    }
}

window.gridDeleteRow = function(btn) {
    if (isDispatching) {
        alert('لا يمكن حذف صفوف أثناء تشغيل حملة إرسال.');
        return;
    }
    const tr = btn.closest('tr');
    if (tr) {
        tr.remove();
        // Re-index row numbers
        const tbody = document.getElementById('sheetGridBody');
        for (let i = 0; i < tbody.rows.length; i++) {
            tbody.rows[i].cells[0].textContent = i + 1;
        }
        syncFromGrid(false);
    }
};

function gridCell(r, c) {
    return document.getElementById('sheetGridBody')?.rows[r]?.cells[c + 1];
}

function parseClipboardTable(text) {
    text = text.replace(/\r/g, '');
    const rows = [];
    let row = [], cur = '', inQ = false;
    for (let i = 0; i < text.length; i++) {
        const ch = text[i];
        if (inQ) {
            if (ch === '"' && text[i + 1] === '"') { cur += '"'; i++; }
            else if (ch === '"') inQ = false;
            else cur += ch;
        } else if (ch === '"' && cur === '') inQ = true;
        else if (ch === '\t') { row.push(cur); cur = ''; }
        else if (ch === '\n') { row.push(cur); rows.push(row); row = []; cur = ''; }
        else cur += ch;
    }
    if (cur !== '' || row.length) { row.push(cur); rows.push(row); }
    return rows.filter(r => r.some(v => String(v).trim() !== ''));
}

function mapHeaderRow(row) {
    const keys = [
        ['اسم', 'موظف', 'name'],
        ['هاتف', 'موبايل', 'تليفون', 'واتس', 'phone'],
        ['اجمالي', 'اجمالى', 'إجمالي', 'إجمالى', 'مرتب', 'gross'],
        ['استقطاع', 'خصم', 'تأمين', 'deduct'],
        ['صافي', 'صافى', 'net'],
        ['صرفي', 'صرفه', 'payment'],
        ['ملاحظ', 'بيان', 'notes']
    ];
    const map = new Array(row.length).fill(-1);
    let hits = 0;
    row.forEach((v, i) => {
        const t = String(v).trim().toLowerCase();
        const idx = keys.findIndex((ks, ki) => !map.includes(ki) && ks.some(k => t.includes(k)));
        if (idx >= 0) {
            map[i] = idx;
            hits++;
        } else {
            const e = GRID_COLS.indexOf(String(v).trim());
            if (e >= BASE_COLS.length) map[i] = e;
        }
    });
    return hits >= 2 ? map : null;
}

function pasteIntoGrid(text, startR, startC) {
    let rows = parseClipboardTable(text);
    if (!rows.length) return;
    let colMap = null;
    const hm = mapHeaderRow(rows[0]);
    if (hm) {
        // Detect and register any extra columns in header
        rows[0].forEach((headerText, j) => {
            if (hm[j] === -1) {
                const cleanHeader = String(headerText).trim().replace(/[{}]/g, '');
                if (cleanHeader) {
                    if (!GRID_COLS.includes(cleanHeader)) {
                        addExtraColumn(cleanHeader, true);
                    }
                    hm[j] = GRID_COLS.indexOf(cleanHeader);
                }
            }
        });
        colMap = hm;
        rows = rows.slice(1);
        startC = 0;
    }
    gridEnsureRows(startR + rows.length + 3);
    rows.forEach((vals, i) => {
        vals.forEach((v, j) => {
            const c = colMap ? colMap[j] : startC + j;
            if (c < 0 || c >= GRID_COLS.length) return;
            const cell = gridCell(startR + i, c);
            if (cell) cell.textContent = String(v).trim();
        });
    });
}

function addExtraColumn(name, autoAddToTemplate = true) {
    name = (name || '').trim().replace(/[{}]/g, '');
    if (!name) return false;
    if (GRID_COLS.includes(name)) {
        alert('هذا العمود موجود بالفعل.');
        return false;
    }
    if (isDispatching) {
        alert('لا يمكن إضافة عمود أثناء تشغيل حملة إرسال.');
        return false;
    }

    GRID_COLS.push(name);
    const c = GRID_COLS.length - 1;

    // Header <th> with delete button
    const headRow = document.getElementById('sheetGridHeadRow');
    if (headRow) {
        const th = document.createElement('th');
        th.className = 'border border-slate-700 px-3 py-2.5 font-bold text-right bg-slate-800 text-teal-300';
        th.dataset.colName = name;
        th.innerHTML = `
            <div class="flex items-center justify-between gap-1.5">
                <span>${escapeHtml(name)}</span>
                <button type="button" onclick="removeExtraColumn('${escapeHtml(name)}')" class="text-teal-300 hover:text-rose-400 text-xs px-1" title="حذف هذا العمود">&times;</button>
            </div>
        `;
        // Insert before last column (Delete action column)
        headRow.insertBefore(th, headRow.lastElementChild);
    }

    // Cells for all existing rows
    const tbody = document.getElementById('sheetGridBody');
    if (tbody) {
        for (const tr of tbody.rows) {
            const td = tr.insertCell(tr.cells.length - 1);
            td.contentEditable = 'true';
            td.dataset.c = c;
            td.className = 'sheet-cell text-slate-800';
        }
    }

    // Add tag button in template editor
    const chips = document.getElementById('extraTagChips');
    if (chips && !chips.querySelector(`button[data-tag="{${name}}"]`)) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'tag-btn text-[11px] font-bold px-2.5 py-1 bg-teal-50 text-teal-700 hover:bg-teal-100 border border-teal-200 rounded-md transition';
        b.dataset.tag = '{' + name + '}';
        b.textContent = '+ {' + name + '}';
        b.addEventListener('click', () => insertTagAtCursor('{' + name + '}'));
        chips.appendChild(b);
    }

    // Automatically insert placeholder into message template if needed
    if (autoAddToTemplate) {
        const templateInput = document.getElementById('messageTemplate');
        if (templateInput && !templateInput.value.includes('{' + name + '}')) {
            const lineToAdd = `🔹 ${name}: {${name}}`;
            let text = templateInput.value;
            if (text.includes('💰 صافي المرتب المستحق:')) {
                text = text.replace('💰 صافي المرتب المستحق:', `${lineToAdd}\n\n💰 صافي المرتب المستحق:`);
            } else if (text.includes('{ملاحظات}')) {
                text = text.replace('{ملاحظات}', `${lineToAdd}\n\n{ملاحظات}`);
            } else {
                text = text.trim() + `\n${lineToAdd}`;
            }
            templateInput.value = text;
        }
    }

    syncFromGrid(false);
    updateTemplatePreview();
    return true;
}

window.removeExtraColumn = function(name) {
    if (isDispatching) {
        alert('لا يمكن حذف عمود أثناء تشغيل حملة إرسال.');
        return;
    }
    if (!confirm(`هل تريد بالتأكيد حذف عمود "${name}"؟`)) return;

    const idx = GRID_COLS.indexOf(name);
    if (idx === -1) return;

    GRID_COLS.splice(idx, 1);

    // Remove <th> from header
    const headRow = document.getElementById('sheetGridHeadRow');
    if (headRow) {
        const th = headRow.cells[idx + 1];
        if (th) th.remove();
    }

    // Remove <td> from each row in tbody and re-index dataset.c
    const tbody = document.getElementById('sheetGridBody');
    if (tbody) {
        for (const tr of tbody.rows) {
            const td = tr.cells[idx + 1];
            if (td) td.remove();
            for (let c = 0; c < GRID_COLS.length; c++) {
                if (tr.cells[c + 1]) tr.cells[c + 1].dataset.c = c;
            }
        }
    }

    // Remove tag chip
    const chips = document.getElementById('extraTagChips');
    if (chips) {
        chips.querySelectorAll(`button[data-tag="{${name}}"]`).forEach(b => b.remove());
    }

    // Remove placeholder line from template
    const templateInput = document.getElementById('messageTemplate');
    if (templateInput) {
        const tag = '{' + name + '}';
        const lines = templateInput.value.split('\n');
        templateInput.value = lines.filter(l => !l.includes(tag)).join('\n').replace(/\n{3,}/g, '\n\n').trim();
    }

    syncFromGrid(false);
    updateTemplatePreview();
};

function readGridRows() {
    const out = [];
    const tbody = document.getElementById('sheetGridBody');
    if (!tbody) return out;

    for (const tr of tbody.rows) {
        const vals = GRID_COLS.map((_, c) => (tr.cells[c + 1]?.textContent || '').trim());
        if (!vals.some(v => v !== '')) continue; // Skip completely empty rows
        
        const rec = {};
        GRID_COLS.forEach((name, c) => {
            let v = vals[c];
            if (NUMERIC_COLS.has(c)) {
                v = v.replace(/[,\s]/g, '');
            }
            rec[name] = v;
        });
        out.push(rec);
    }
    return out;
}

function normalizePhoneNumber(rawPhone) {
    if (!rawPhone) return { clean: '', formatted: '', valid: false };
    let phoneStr = String(rawPhone).trim().replace(/\.0+$/, '').replace(/[^\d+]/g, '').trim();
    if (!phoneStr) return { clean: '', formatted: '', valid: false };

    if (phoneStr.startsWith('+')) {
        phoneStr = phoneStr.substring(1);
    }

    // Egypt handling: 010... -> 2010...
    if (phoneStr.startsWith('01')) {
        phoneStr = '20' + phoneStr.substring(1);
    } else if (phoneStr.startsWith('1') && phoneStr.length === 10) {
        phoneStr = '20' + phoneStr;
    } else if (!phoneStr.startsWith('20') && phoneStr.length === 11 && phoneStr.startsWith('0')) {
        phoneStr = '20' + phoneStr.substring(1);
    }

    const isValid = phoneStr.length >= 10 && phoneStr.length <= 15;
    return {
        clean: phoneStr,
        formatted: '+' + phoneStr,
        valid: isValid
    };
}

function formatNumber(val) {
    if (val === null || val === undefined || val === '') return '0';
    const num = Number(val);
    if (isNaN(num)) return String(val);
    return num.toLocaleString('en-US');
}

function syncFromGrid(fromPaste = false) {
    const rows = readGridRows();
    const countBadge = document.getElementById('gridCount');
    if (countBadge) {
        countBadge.textContent = rows.length ? `${rows.length} موظف` : '';
    }

    if (isDispatching) return;

    // Preserve custom messages if previously modified
    const prevCustom = {};
    employeeQueue.forEach(e => {
        if (e.customMessage) prevCustom[e.name + '|' + e.phone] = e.customMessage;
    });

    const colName = 'اسم الموظف';
    const colPhone = 'رقم الهاتف';
    const colSalary = 'إجمالي المرتب';
    const colDeductions = 'الاستقطاعات';
    const colNet = 'صافي المرتب';
    const colPayment = 'الصرفية';
    const colNotes = 'ملاحظات';
    const extras = extraColNames();

    employeeQueue = rows.map((row, idx) => {
        const name = String(row[colName] || `موظف #${idx + 1}`).trim();
        const rawPhone = row[colPhone] || '';
        const phoneData = normalizePhoneNumber(rawPhone);
        const salary = row[colSalary] !== undefined ? row[colSalary] : '0';
        const deductions = row[colDeductions] !== undefined ? row[colDeductions] : '0';
        const net = row[colNet] !== undefined ? row[colNet] : '0';
        const payment = String(row[colPayment] || '').trim();
        const notes = String(row[colNotes] || '').trim();

        const empKey = name + '|' + phoneData.clean;
        return {
            id: idx,
            name: name,
            rawPhone: rawPhone,
            phone: phoneData.clean,
            phoneFormatted: phoneData.formatted,
            phoneValid: phoneData.valid,
            salary: formatNumber(salary),
            deductions: formatNumber(deductions),
            net: formatNumber(net),
            payment: payment,
            notes: notes ? `📌 ملاحظة: ${notes}` : '',
            extras: Object.fromEntries(extras.map(n => [n, String(row[n] ?? '').trim()])),
            status: phoneData.valid ? 'pending' : 'invalid_phone',
            statusText: phoneData.valid ? 'قيد الانتظار' : 'رقم غير صحيح',
            sentAt: null,
            errorDetail: phoneData.valid ? '' : 'صيغة رقم الهاتف غير صالحة',
            customMessage: prevCustom[empKey] || ''
        };
    });

    updateCounters();
    updatePreviewDropdown();
    renderResultsTable();
    updateTemplatePreview();
    persistStateToStorage(rows, extras);
}

function persistStateToStorage(rows, extras) {
    try {
        const toSave = rows || readGridRows();
        const extraNames = extras || extraColNames();
        localStorage.setItem('payroll_grid_rows', JSON.stringify(toSave));
        localStorage.setItem('payroll_extra_cols', JSON.stringify(extraNames));

        const customMap = {};
        employeeQueue.forEach(e => {
            if (e.customMessage) customMap[e.name + '|' + e.phone] = e.customMessage;
        });
        localStorage.setItem('payroll_custom_messages', JSON.stringify(customMap));
    } catch (e) {}
}

function restoreGridFromStorage() {
    const tbody = document.getElementById('sheetGridBody');
    if (!tbody) return false;

    try {
        const savedExtras = JSON.parse(localStorage.getItem('payroll_extra_cols') || '[]');
        if (Array.isArray(savedExtras)) {
            savedExtras.forEach(name => {
                if (name && !GRID_COLS.includes(name)) {
                    addExtraColumn(name, false);
                }
            });
        }

        const savedRows = JSON.parse(localStorage.getItem('payroll_grid_rows') || '[]');
        if (Array.isArray(savedRows) && savedRows.length > 0) {
            tbody.innerHTML = '';
            savedRows.forEach(rowObj => {
                const vals = GRID_COLS.map(col => rowObj[col] ?? '');
                gridAddRow(vals);
            });
            gridEnsureRows(Math.max(15, savedRows.length));
            syncFromGrid(false);
            return true;
        }
    } catch (e) {
        console.warn('Could not restore grid from localStorage:', e);
    }
    gridEnsureRows(15);
    return false;
}

let gridEventsAttached = false;
function initSheetGrid() {
    const tbody = document.getElementById('sheetGridBody');
    if (!tbody) return;

    restoreGridFromStorage();

    if (gridEventsAttached) return;
    gridEventsAttached = true;

    // Paste handling directly in table
    tbody.addEventListener('paste', (e) => {
        const td = e.target.closest('td[data-c]');
        if (!td) return;
        e.preventDefault();
        const text = (e.clipboardData || window.clipboardData).getData('text/plain');
        pasteIntoGrid(text, td.parentElement.rowIndex - 1, Number(td.dataset.c));
        syncFromGrid(true);
    });

    // Inline input handling
    tbody.addEventListener('input', () => {
        clearTimeout(gridSyncTimer);
        gridSyncTimer = setTimeout(() => syncFromGrid(false), 400);
    });

    // Enter key moves to next cell
    tbody.addEventListener('keydown', (e) => {
        const td = e.target.closest('td[data-c]');
        if (!td) return;
        if (e.key === 'Enter') {
            e.preventDefault();
            const next = gridCell(td.parentElement.rowIndex, Number(td.dataset.c));
            if (next) next.focus();
        }
    });

    // Toolbar buttons
    document.getElementById('gridAddRowBtn')?.addEventListener('click', () => {
        gridAddRow();
        persistStateToStorage();
    });

    document.getElementById('gridAddColBtn')?.addEventListener('click', () => {
        const name = prompt('أدخل اسم العمود الجديد (مثلاً: حافز، بدل انتقال، كود الموظف):');
        if (name) addExtraColumn(name);
    });

    document.getElementById('gridClearBtn')?.addEventListener('click', () => {
        if (isDispatching) {
            alert('لا يمكن مسح البيانات أثناء تشغيل حملة إرسال.');
            return;
        }
        if (!confirm('هل تريد مسح جميع البيانات من الجدول؟')) return;
        try {
            localStorage.removeItem('payroll_grid_rows');
            localStorage.removeItem('payroll_extra_cols');
            localStorage.removeItem('payroll_custom_messages');
        } catch (e) {}
        tbody.innerHTML = '';
        gridEnsureRows(15);
        syncFromGrid(false);
    });
}

// ---------------------------------------------------------------------------
// 3. File Upload Handler (Excel / CSV)
// ---------------------------------------------------------------------------
async function handleFileUpload(file) {
    const formData = new FormData();
    formData.append('file', file);

    const indicator = document.getElementById('uploadIndicator');
    if (indicator) indicator.classList.remove('hidden');

    try {
        const res = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });
        const result = await res.json();
        if (!res.ok) {
            throw new Error(result.detail || 'حدث خطأ أثناء قراءة الملف');
        }

        loadUploadedDataIntoGrid(result);
    } catch (err) {
        alert('❌ خطأ: ' + err.message);
    } finally {
        if (indicator) indicator.classList.add('hidden');
    }
}

function loadUploadedDataIntoGrid(result) {
    const rows = result.preview_data || [];
    if (!rows.length) {
        alert('الملف فارغ أو لا يحتوي على صفوف بيانات.');
        return;
    }

    const mapping = result.detected_mapping || {};
    const colName = mapping.name || 'اسم الموظف';
    const colPhone = mapping.phone || 'رقم الهاتف';
    const colSalary = mapping.salary || 'إجمالي المرتب';
    const colDeductions = mapping.deductions || 'الاستقطاعات';
    const colNet = mapping.net || 'صافي المرتب';
    const colPayment = mapping.payment || '';
    const colNotes = mapping.notes || '';

    // Identify extra columns from uploaded file
    const standardKeys = new Set([colName, colPhone, colSalary, colDeductions, colNet, colPayment, colNotes].filter(Boolean));
    const extraColsInFile = (result.columns || []).filter(c => !standardKeys.has(c));

    extraColsInFile.forEach(c => {
        const clean = String(c).trim().replace(/[{}]/g, '');
        if (clean && !GRID_COLS.includes(clean)) {
            addExtraColumn(clean, true);
        }
    });

    const tbody = document.getElementById('sheetGridBody');
    if (tbody) tbody.innerHTML = '';

    rows.forEach(r => {
        const rowVals = [
            r[colName] || '',
            r[colPhone] || '',
            r[colSalary] || '',
            r[colDeductions] || '',
            r[colNet] || '',
            (colPayment && r[colPayment]) || '',
            (colNotes && r[colNotes]) || ''
        ];
        // Extra columns
        extraColNames().forEach(n => {
            rowVals.push(r[n] || '');
        });
        gridAddRow(rowVals);
    });

    syncFromGrid(false);
}

// ---------------------------------------------------------------------------
// 4. Message Settings & Live Preview
// ---------------------------------------------------------------------------
function insertTagAtCursor(tag) {
    const textarea = document.getElementById('messageTemplate');
    if (!textarea) return;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const text = textarea.value;
    textarea.value = text.substring(0, start) + tag + text.substring(end);
    textarea.focus();
    textarea.selectionStart = textarea.selectionEnd = start + tag.length;
    updateTemplatePreview();
}

function formatEmployeeMessage(emp) {
    let template = document.getElementById('messageTemplate')?.value || DEFAULT_TEMPLATE;
    const paymentDesc = (emp.payment && emp.payment.trim()) || document.getElementById('salaryMonth')?.value || 'مرتب هذا الشهر';

    let msg = template
        .replace(/{الاسم}/g, emp.name)
        .replace(/{المرتب}/g, emp.salary)
        .replace(/{الاستقطاع}/g, emp.deductions)
        .replace(/{الصافي}/g, emp.net)
        .replace(/{الصرفية}/g, paymentDesc)
        .replace(/{ملاحظات}/g, emp.notes || '');

    // Extra custom columns
    Object.entries(emp.extras || {}).forEach(([k, v]) => {
        const val = (v !== null && v !== undefined) ? String(v).trim() : '';
        const tag = '{' + k + '}';
        if (msg.includes(tag)) {
            msg = msg.split(tag).join(val);
        } else if (val) {
            const line = `🔹 ${k}: ${val}`;
            if (msg.includes('💰 صافي المرتب المستحق:')) {
                msg = msg.replace('💰 صافي المرتب المستحق:', `${line}\n\n💰 صافي المرتب المستحق:`);
            } else {
                msg = msg.trim() + `\n${line}`;
            }
        }
    });

    return msg.replace(/\n{3,}/g, '\n\n').trim();
}

function getFinalMessage(emp) {
    if (emp.customMessage && emp.customMessage.trim()) {
        return emp.customMessage;
    }
    return formatEmployeeMessage(emp);
}

function updatePreviewDropdown() {
    const sel = document.getElementById('previewEmpSelect');
    if (!sel) return;
    const currentVal = sel.value;
    sel.innerHTML = '';

    if (!employeeQueue.length) {
        const opt = document.createElement('option');
        opt.value = '0';
        opt.textContent = 'معاينة افتراضية';
        sel.appendChild(opt);
        return;
    }

    employeeQueue.forEach((e, idx) => {
        const opt = document.createElement('option');
        opt.value = String(idx);
        opt.textContent = `${idx + 1}. ${e.name}`;
        if (String(idx) === currentVal) opt.selected = true;
        sel.appendChild(opt);
    });
}

function updateTemplatePreview() {
    const previewBox = document.getElementById('templatePreviewText');
    if (!previewBox) return;

    let emp = null;
    if (employeeQueue.length > 0) {
        const selIdx = parseInt(document.getElementById('previewEmpSelect')?.value || '0');
        emp = employeeQueue[selIdx] || employeeQueue[0];
    } else {
        emp = {
            name: 'أحمد محمود حسن',
            phone: '201012345678',
            salary: '8,500',
            deductions: '500',
            net: '8,000',
            payment: document.getElementById('salaryMonth')?.value || 'مرتب شهر أكتوبر 2026',
            notes: '📌 ملاحظة: حافز انتظام 500 ج.م',
            extras: Object.fromEntries(extraColNames().map(n => [n, '500']))
        };
    }

    previewBox.textContent = getFinalMessage(emp);
}

// ---------------------------------------------------------------------------
// 5. Results Table & Progress
// ---------------------------------------------------------------------------
function updateCounters() {
    const total = employeeQueue.length;
    const sent = employeeQueue.filter(e => e.status === 'sent').length;
    const pending = employeeQueue.filter(e => e.status === 'pending').length;
    const failed = employeeQueue.filter(e => e.status === 'failed' || e.status === 'invalid_phone').length;

    document.getElementById('counterTotal').textContent = total;
    document.getElementById('counterSent').textContent = sent;
    document.getElementById('counterPending').textContent = pending;
    document.getElementById('counterFailed').textContent = failed;

    const percent = total > 0 ? Math.round((sent / total) * 100) : 0;
    const bar = document.getElementById('mainProgressBar');
    if (bar) bar.style.width = `${percent}%`;
    const pctText = document.getElementById('mainProgressPercent');
    if (pctText) pctText.textContent = `${percent}%`;
}

function renderResultsTable() {
    const tbody = document.getElementById('resultsTableBody');
    if (!tbody) return;

    if (!employeeQueue.length) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="px-4 py-8 text-center text-slate-400 dark:text-zinc-500">
                    لا توجد بيانات موظفين حالياً. الصق أو ارفع بيانات المرتبات في الجدول أعلاه للبدء.
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = '';
    employeeQueue.forEach((emp, idx) => {
        const tr = document.createElement('tr');
        tr.id = `row-emp-${emp.id}`;
        tr.className = 'hover:bg-slate-50 dark:hover:bg-zinc-800/40 transition border-b border-slate-100 dark:border-zinc-800/80 text-xs';

        let badgeClass = 'badge-pending';
        if (emp.status === 'sent') badgeClass = 'badge-sent';
        if (emp.status === 'sending') badgeClass = 'badge-sending';
        if (emp.status === 'failed' || emp.status === 'invalid_phone') badgeClass = 'badge-failed';

        tr.innerHTML = `
            <td class="px-3.5 py-3 text-center text-slate-500 dark:text-zinc-400 font-mono">${idx + 1}</td>
            <td class="px-3.5 py-3 font-bold text-slate-800 dark:text-zinc-100">${escapeHtml(emp.name)}</td>
            <td class="px-3.5 py-3 font-mono text-slate-600 dark:text-zinc-300 dir-ltr text-right">
                ${emp.phoneValid ? `<span class="text-emerald-700 dark:text-emerald-400 font-semibold">${emp.phoneFormatted}</span>` : `<span class="text-rose-500 line-through">${escapeHtml(emp.rawPhone)}</span>`}
            </td>
            <td class="px-3.5 py-3 font-bold text-emerald-700 dark:text-emerald-400">${emp.net} ج.م</td>
            <td class="px-3.5 py-3">
                <span id="badge-emp-${emp.id}" class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold ${badgeClass}">
                    ${emp.statusText}
                </span>
            </td>
            <td class="px-3.5 py-3 text-center whitespace-nowrap">
                <button type="button" onclick="openMessageEditor(${emp.id})" class="inline-flex items-center gap-1 px-2.5 py-1 ${emp.customMessage ? 'bg-amber-500 hover:bg-amber-600' : 'bg-slate-600 dark:bg-zinc-700 hover:bg-slate-700 dark:hover:bg-zinc-600'} text-white rounded-lg transition ml-1 shadow-xs" title="تعديل نص الرسالة لهذا الموظف">
                    <i class="fas fa-pen text-[10px]"></i> ${emp.customMessage ? 'معدّلة' : 'تعديل'}
                </button>
                <button type="button" onclick="sendSingleDirect(${emp.id})" class="inline-flex items-center gap-1 px-2.5 py-1 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg transition ml-1 shadow-xs" title="إرسال مباشر لهذا الموظف">
                    <i class="fab fa-whatsapp text-xs"></i> إرسال
                </button>
                <button type="button" onclick="openDetailsModal(${emp.id})" class="inline-flex items-center gap-1 px-2.5 py-1 bg-slate-100 hover:bg-slate-200 dark:bg-zinc-800 dark:hover:bg-zinc-700 text-slate-700 dark:text-zinc-200 rounded-lg transition border border-slate-200 dark:border-zinc-700" title="عرض تفاصيل الموظف">
                    <i class="fas fa-eye text-[10px]"></i> التفاصيل
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function updateEmployeeRowStatus(emp) {
    const badge = document.getElementById(`badge-emp-${emp.id}`);
    if (!badge) return;

    badge.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold';
    if (emp.status === 'sent') {
        badge.classList.add('badge-sent');
        badge.innerHTML = `<i class="fas fa-circle-check ml-1 text-xs"></i> تم (${emp.sentAt || ''})`;
    } else if (emp.status === 'sending') {
        badge.classList.add('badge-sending');
        badge.innerHTML = `<i class="fas fa-spinner fa-spin ml-1 text-xs"></i> جاري الإرسال...`;
    } else if (emp.status === 'failed' || emp.status === 'invalid_phone') {
        badge.classList.add('badge-failed');
        badge.innerHTML = `<i class="fas fa-triangle-exclamation ml-1 text-xs"></i> ${escapeHtml(emp.statusText)}`;
    } else {
        badge.classList.add('badge-pending');
        badge.innerHTML = `<i class="fas fa-clock ml-1 text-xs"></i> قيد الانتظار`;
    }
}

// ---------------------------------------------------------------------------
// 6. Campaign Sending Controls (Start / Pause / Skip / Stop)
// ---------------------------------------------------------------------------
function getCampaignSettings() {
    return {
        min_delay: parseInt(document.getElementById('minDelaySec')?.value || '5'),
        max_delay: parseInt(document.getElementById('maxDelaySec')?.value || '10'),
        batch_size: 25,
        batch_pause: 60
    };
}

async function postJSON(url, body) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'حدث خطأ غير متوقع');
    return data;
}

async function startDispatcher() {
    syncFromGrid(false);
    if (!employeeQueue.length) {
        alert('⚠️ يرجى إدخال أو رفع بيانات المرتبات أولاً.');
        return;
    }
    if (isDispatching) return;

    if (!waStatus.logged_in) {
        alert('⚠️ واتساب ويب غير متصل حالياً! يرجى الضغط على "بدء ربط واتساب" ومسح رمز الـ QR من هاتفك أولاً.');
        return;
    }

    const pendingList = employeeQueue.filter(e => e.status === 'pending' && e.phoneValid);
    if (!pendingList.length) {
        alert('🎉 لا يوجد موظفون قيد الانتظار للإرسال.');
        return;
    }

    const editedCount = pendingList.filter(e => e.customMessage).length;
    let confirmMsg = `هل تريد بدء إرسال مفردات المرتبات لعدد ${pendingList.length} موظف؟`;
    if (editedCount) {
        confirmMsg += `\n(منهم ${editedCount} رسالة مخصصة تم تعديلها يدوياً).`;
    }

    if (!confirm(confirmMsg)) return;

    try {
        await launchCampaign(pendingList);
    } catch (err) {
        alert('❌ ' + err.message);
    }
}

async function launchCampaign(list) {
    const payload = {
        employees: list.map(e => ({
            id: e.id,
            name: e.name,
            phone: e.phone,
            message: getFinalMessage(e)
        })),
        settings: getCampaignSettings()
    };

    await postJSON('/api/campaign/start', payload);

    isDispatching = true;
    isPaused = false;
    list.forEach(e => {
        e.status = 'pending';
        e.statusText = 'قيد الانتظار';
    });

    // Update UI controls
    document.getElementById('startDispatchBtn')?.classList.add('hidden');
    document.getElementById('pauseDispatchBtn')?.classList.remove('hidden');
    document.getElementById('stopDispatchBtn')?.classList.remove('hidden');
    document.getElementById('skipDispatchBtn')?.classList.remove('hidden');
    document.getElementById('campaignActiveBanner')?.classList.remove('hidden');

    clearInterval(statusPoller);
    statusPoller = setInterval(pollCampaignStatus, 1000);
    pollCampaignStatus();
}

async function togglePauseDispatcher() {
    try {
        const r = await postJSON('/api/campaign/pause');
        isPaused = r.paused;
        const btn = document.getElementById('pauseDispatchBtn');
        if (btn) {
            btn.innerHTML = isPaused
                ? '<i class="fas fa-play"></i> استئناف'
                : '<i class="fas fa-pause"></i> إيقاف مؤقت';
        }
    } catch (e) {
        alert('❌ ' + e.message);
    }
}

async function skipCurrentEmployee() {
    try {
        await postJSON('/api/campaign/skip');
    } catch (e) {
        alert('❌ ' + e.message);
    }
}

async function stopDispatcher() {
    if (!confirm('هل تريد بالتأكيد إنهاء حملة الإرسال الحالية؟')) return;
    try {
        await postJSON('/api/campaign/stop');
    } catch (e) {}
}

async function pollCampaignStatus() {
    let st;
    try {
        const res = await fetch('/api/campaign/status');
        st = await res.json();
    } catch (e) {
        return;
    }

    // Update statuses from backend results
    Object.entries(st.results || {}).forEach(([id, r]) => {
        const emp = employeeQueue.find(e => e.id === Number(id));
        if (!emp) return;
        const prev = emp.status;

        if (r.status === 'sent') {
            emp.status = 'sent';
            emp.statusText = 'تم الإرسال بنجاح';
            emp.sentAt = r.sent_at || '';
            emp.errorDetail = '';
        } else if (r.status === 'failed') {
            emp.status = 'failed';
            emp.statusText = 'فشل: ' + (r.detail || 'تعذر الإرسال');
            emp.errorDetail = r.detail || '';
        } else if (r.status === 'skipped') {
            emp.status = 'failed';
            emp.statusText = 'تم التخطي يدوياً';
        } else if (r.status === 'sending') {
            emp.status = 'sending';
            emp.statusText = 'جاري الإرسال...';
        }

        if (prev !== emp.status) {
            updateEmployeeRowStatus(emp);
            if (emp.status === 'sent') playChime('success');
        }
    });

    updateCounters();

    // Update live banner
    const banner = document.getElementById('campaignActiveBanner');
    const currEmpText = document.getElementById('campaignCurrentEmp');
    const statusText = document.getElementById('campaignStatusText');
    const countdown = document.getElementById('campaignCountdown');

    if (banner) {
        if (st.current_name) {
            if (currEmpText) currEmpText.textContent = `جاري معالجة: ${st.current_name} (+${st.current_phone || ''})`;
        } else {
            if (currEmpText) currEmpText.textContent = 'استراحة فاصلة بين الرسائل...';
        }
        if (statusText) statusText.textContent = st.paused ? '⏸️ الإرسال متوقف مؤقتاً' : (st.message || 'جاري الإرسال...');
        if (countdown) countdown.textContent = st.countdown || 0;
    }

    // Handle Campaign Completion
    if (!st.running && ['done', 'stopped', 'error'].includes(st.phase)) {
        clearInterval(statusPoller);
        isDispatching = false;

        document.getElementById('startDispatchBtn')?.classList.remove('hidden');
        document.getElementById('pauseDispatchBtn')?.classList.add('hidden');
        document.getElementById('stopDispatchBtn')?.classList.add('hidden');
        document.getElementById('skipDispatchBtn')?.classList.add('hidden');
        document.getElementById('campaignActiveBanner')?.classList.add('hidden');

        employeeQueue.forEach(e => {
            if (e.status === 'sending') {
                e.status = 'pending';
                e.statusText = 'قيد الانتظار';
                updateEmployeeRowStatus(e);
            }
        });
        updateCounters();

        if (st.phase === 'done') {
            playChime('success');
            const failedCount = employeeQueue.filter(e => e.status === 'failed').length;
            alert(failedCount ? `انتهت الحملة. فشل الإرسال لعدد ${failedCount} موظف.` : '🎊 تم إرسال جميع الرسائل بنجاح!');
        } else if (st.phase === 'error') {
            alert('❌ ' + st.message);
        }
    }
}

// ---------------------------------------------------------------------------
// 7. Single Message Sending & Modal Editors
// ---------------------------------------------------------------------------
window.sendSingleDirect = async function(empId) {
    const emp = employeeQueue.find(e => e.id === empId);
    if (!emp) return;
    if (!emp.phoneValid) {
        alert('❌ لا يمكن الإرسال: رقم الهاتف غير صالح.');
        return;
    }
    if (isDispatching) {
        alert('⚠️ توجد حملة إرسال قيد التشغيل حالياً.');
        return;
    }
    if (!waStatus.logged_in) {
        alert('⚠️ واتساب غير متصل. يرجى مسح رمز الـ QR والاتصال أولاً.');
        return;
    }

    if (!confirm(`هل تريد إرسال رسالة المرتب فوراً إلى: ${emp.name}؟`)) return;

    try {
        await launchCampaign([emp]);
    } catch (err) {
        alert('❌ ' + err.message);
    }
};

window.openMessageEditor = function(empId) {
    const emp = employeeQueue.find(e => e.id === empId);
    if (!emp) return;
    editingEmpId = empId;
    document.getElementById('editMsgEmployeeName').textContent = emp.name;
    document.getElementById('editMsgTextarea').value = getFinalMessage(emp);
    document.getElementById('editMessageModal')?.classList.remove('hidden');
    document.getElementById('editMsgTextarea')?.focus();
};

window.saveEditedMessage = function() {
    const emp = employeeQueue.find(e => e.id === editingEmpId);
    if (!emp) return;
    const text = document.getElementById('editMsgTextarea')?.value.trim();
    if (!text) {
        alert('نص الرسالة فارغ! يرجى كتابة نص الرسالة أو اختيار إرجاع للنص الأصلي.');
        return;
    }
    emp.customMessage = text;
    persistStateToStorage();
    closeMessageEditor();
    renderResultsTable();
    updateTemplatePreview();
};

window.resetEditedMessage = function() {
    const emp = employeeQueue.find(e => e.id === editingEmpId);
    if (!emp) return;
    emp.customMessage = '';
    persistStateToStorage();
    const textarea = document.getElementById('editMsgTextarea');
    if (textarea) textarea.value = formatEmployeeMessage(emp);
    renderResultsTable();
    updateTemplatePreview();
};

window.closeMessageEditor = function() {
    document.getElementById('editMessageModal')?.classList.add('hidden');
    editingEmpId = null;
};

// Details Modal
window.openDetailsModal = function(empId) {
    const emp = employeeQueue.find(e => e.id === empId);
    if (!emp) return;

    document.getElementById('detailEmpName').textContent = emp.name;
    document.getElementById('detailEmpPhone').textContent = emp.phoneFormatted || emp.rawPhone;
    document.getElementById('detailEmpGross').textContent = emp.salary;
    document.getElementById('detailEmpDeduct').textContent = emp.deductions;
    document.getElementById('detailEmpNet').textContent = emp.net;
    document.getElementById('detailEmpPayment').textContent = emp.payment || document.getElementById('salaryMonth')?.value || '-';

    const statusBadge = document.getElementById('detailEmpStatus');
    if (statusBadge) {
        statusBadge.textContent = emp.statusText;
        statusBadge.className = 'font-bold ' + (emp.status === 'sent' ? 'text-emerald-700' : (emp.status === 'failed' ? 'text-rose-700' : 'text-amber-700'));
    }

    document.getElementById('detailEmpMessage').textContent = getFinalMessage(emp);
    document.getElementById('detailsModal')?.classList.remove('hidden');
};

window.closeDetailsModal = function() {
    document.getElementById('detailsModal')?.classList.add('hidden');
};

// ---------------------------------------------------------------------------
// 8. Export Report to Excel
// ---------------------------------------------------------------------------
async function exportReportExcel() {
    if (!employeeQueue.length) {
        alert('لا توجد بيانات موظفين لتصديرها.');
        return;
    }

    const reportRows = employeeQueue.map(e => {
        const row = {
            "كود": e.id + 1,
            "اسم الموظف": e.name,
            "رقم الهاتف": e.phoneFormatted || e.rawPhone,
            "إجمالي المرتب": e.salary,
            "الصرفية": e.payment || (document.getElementById('salaryMonth')?.value || ''),
            "الاستقطاعات": e.deductions,
            "صافي المرتب": e.net
        };
        extraColNames().forEach(n => {
            row[n] = (e.extras && e.extras[n]) || '';
        });
        row["حالة الإرسال"] = e.statusText;
        row["وقت الإرسال"] = e.sentAt || '-';
        row["ملاحظات إضافية"] = e.errorDetail || '';
        return row;
    });

    try {
        const response = await fetch('/api/export-report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ report_data: reportRows })
        });

        if (!response.ok) {
            let detail = '';
            try { detail = (await response.json()).detail || ''; } catch (e) {}
            throw new Error(`فشل تصدير التقرير (${response.status}) ${detail}`);
        }

        const blob = await response.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `تقرير_إرسال_رواتب_واتساب_${new Date().toLocaleDateString('ar-EG').replace(/\//g, '-')}.xlsx`;
        document.body.appendChild(a);
        a.click();
        a.remove();
    } catch (e) {
        alert('حدث خطأ أثناء تحميل التقرير: ' + e.message);
    }
}

// Escape helper for safe HTML rendering
function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

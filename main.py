import io
import urllib.parse
import os
import re
import sys
from typing import List, Optional

# Ensure UTF-8 output encoding on Windows consoles
if sys.platform.startswith('win'):
    try:
        if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel, Field

import sender
import auth
import email_service

app = FastAPI(title="نظام إرسال رواتب الموظفين عبر واتساب", version="1.0.0")

# في الإنتاج: شغّل التطبيق خلف HTTPS واضبط APP_ENV=production.
# في التشغيل المحلي عبر HTTP تظل Secure معطلة حتى تعمل الجلسات على localhost.
APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
COOKIE_SECURE = os.environ.get(
    "COOKIE_SECURE", "1" if APP_ENV == "production" else "0"
).strip().lower() in {"1", "true", "yes"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# Setup directories
FROZEN = getattr(sys, "frozen", False)
# الملفات المدمجة (صفحات وستايل): داخل الـ exe لو متحزّم
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
# مكان الكتابة (ملفات مؤقتة وتقارير): جنب الـ exe
DATA_DIR = os.path.dirname(sys.executable) if FROZEN else BASE_DIR
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, "css"), exist_ok=True)
os.makedirs(os.path.join(STATIC_DIR, "js"), exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


APP_VERSION = "2.5"


@app.get("/api/version")
async def version():
    return {"version": APP_VERSION}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

PUBLIC_PREFIXES = ("/static/", "/api/auth/")
PUBLIC_PATHS = {"/", "/api/version"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    request.state.user = auth.get_user_by_token(request.cookies.get(auth.SESSION_COOKIE))
    if request.state.user is None and path not in PUBLIC_PATHS and not path.startswith(PUBLIC_PREFIXES):
        return JSONResponse({"detail": "سجّل دخول الأول"}, status_code=401)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def serve_home(request: Request):
    user = request.state.user
    if user is None:
        email_ready, status_code, status_msg = auth.get_email_status_detail()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "email_ready": email_ready,
                "email_status": status_code,
                "email_message": status_msg,
                "is_dev": email_service.is_dev_mode(),
            },
        )
    return templates.TemplateResponse(request=request, name="index.html", context={"user": user})


@app.get("/api/auth/email-status")
def auth_email_status():
    email_ready, status_code, status_msg = auth.get_email_status_detail()
    return {
        "ready": email_ready,
        "status": status_code,
        "message": status_msg,
        "is_dev": email_service.is_dev_mode(),
    }


# ---------------------------------------------------------------------------
# الحسابات
# ---------------------------------------------------------------------------
class RegisterBody(BaseModel):
    name: str
    email: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


class VerifyBody(BaseModel):
    email: str
    code: str


class EmailBody(BaseModel):
    email: str


def _auth_call(fn, *args):
    try:
        return fn(*args)
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status, detail={"message": e.message, **e.extra})


def _set_session(response: Response, user: dict):
    token = auth.create_session(user["id"])
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


@app.post("/api/auth/register")
def auth_register(body: RegisterBody):
    return _auth_call(auth.register, body.name, body.email, body.password)


@app.post("/api/auth/resend")
def auth_resend(body: EmailBody):
    return _auth_call(auth.resend_code, body.email)


@app.post("/api/auth/verify")
def auth_verify(body: VerifyBody, response: Response):
    user = _auth_call(auth.verify_code, body.email, body.code)
    _set_session(response, user)
    return {"status": "ok", "name": user["name"]}


@app.post("/api/auth/login")
def auth_login(body: LoginBody, response: Response):
    user = _auth_call(auth.login, body.email, body.password)
    _set_session(response, user)
    return {"status": "ok", "name": user["name"]}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(
        auth.SESSION_COOKIE,
        path="/",
        secure=COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
    return {"status": "ok"}

def detect_best_column(columns: List[str], keywords: List[str]) -> Optional[str]:
    for col in columns:
        cleaned_col = str(col).strip().lower()
        for kw in keywords:
            if kw in cleaned_col:
                return col
    return None

@app.post("/api/upload")
async def upload_excel(file: UploadFile = File(...)):
    fname = (file.filename or '').lower()
    if not fname.endswith(('.xlsx', '.xls', '.csv')):
        raise HTTPException(status_code=400, detail="يرجى رفع ملف بصيغة Excel (.xlsx, .xls) أو CSV")

    # اقرأ حدًا أقصى + بايت واحد حتى نكتشف الملفات الأكبر دون تحميلها بالكامل.
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="حجم الملف أكبر من الحد المسموح (10 ميجابايت)")

    try:
        if fname.endswith('.csv'):
            try:
                df = pd.read_csv(io.BytesIO(contents), encoding='utf-8', dtype=str)
            except UnicodeDecodeError:
                df = pd.read_csv(io.BytesIO(contents), encoding='cp1256', dtype=str)
        else:
            df = pd.read_excel(io.BytesIO(contents), dtype=str)
    except Exception:
        # لا نعرض تفاصيل المكتبات أو مسارات الملفات الداخلية للمستخدم.
        raise HTTPException(status_code=400, detail="تعذر قراءة الملف. تأكد من سلامة الملف وصيغته.")

    if df.empty:
        raise HTTPException(status_code=400, detail="الملف المرفوع فارغ!")

    # Clean empty rows and string values
    df = df.dropna(how='all')
    df = df.fillna('')
    columns = [str(c).strip() for c in df.columns.tolist()]
    df.columns = columns

    # Intelligent Column Auto-Detection
    detected_name = detect_best_column(columns, ['اسم', 'موظف', 'name', 'employee'])
    detected_phone = detect_best_column(columns, ['هاتف', 'موبايل', 'تليفون', 'واتس', 'phone', 'mobile', 'whats', 'tel'])
    detected_salary = detect_best_column(columns, ['اجمالي', 'اجمالى', 'إجمالي', 'إجمالى', 'أساسي', 'اساسي', 'مرتب', 'راتب', 'salary', 'basic', 'gross'])
    detected_deductions = detect_best_column(columns, ['استقطاع', 'خصم', 'خصومات', 'تأمين', 'deduct', 'deduction'])
    detected_net = detect_best_column(columns, ['صافي', 'صافى', 'net', 'total net', 'final'])
    detected_payment = detect_best_column(columns, ['صرفي', 'صرفه', 'payment', 'type'])
    detected_notes = detect_best_column(columns, ['ملاحظ', 'بيان', 'notes', 'note', 'remark'])

    # تحويل كل القيم لأنواع بسيطة تتحوّل JSON بأمان (أرقام الهاتف بدون .0، والتواريخ نص)
    def clean(v):
        try:
            if isinstance(v, float) and v.is_integer():
                return int(v)
            if hasattr(v, 'item'):
                v = v.item()
                if isinstance(v, float) and v.is_integer():
                    return int(v)
            if isinstance(v, (int, float, str, bool)):
                return v
            return str(v)
        except Exception:
            return str(v)

    records = [{k: clean(v) for k, v in row.items()} for row in df.to_dict(orient='records')]
    
    return {
        "status": "success",
        "filename": file.filename,
        "total_rows": len(df),
        "columns": columns,
        "detected_mapping": {
            "name": detected_name or (columns[0] if len(columns) > 0 else ""),
            "phone": detected_phone or (columns[1] if len(columns) > 1 else ""),
            "salary": detected_salary or (columns[2] if len(columns) > 2 else ""),
            "deductions": detected_deductions or (columns[3] if len(columns) > 3 else ""),
            "net": detected_net or (columns[4] if len(columns) > 4 else ""),
            "payment": detected_payment or "",
            "notes": detected_notes or ""
        },
        "preview_data": records[:150]
    }

class ExportRequest(BaseModel):
    report_data: List[dict]

@app.post("/api/export-report")
async def export_report(req: ExportRequest):
    if not req.report_data:
        raise HTTPException(status_code=400, detail="لا توجد بيانات لتصديرها")

    df = pd.DataFrame(req.report_data)

    # امنع تحويل النصوص التي تبدأ بعلامات الصيغ إلى معادلات داخل Excel.
    def neutralize_excel_formula(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    df = df.apply(lambda column: column.map(neutralize_excel_formula))
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name="تقرير إرسال المرتبات")
        ws = writer.sheets["تقرير إرسال المرتبات"]
        ws.sheet_view.rightToLeft = True
        for i, col in enumerate(df.columns, 1):
            longest = max([len(str(col))] + [len(str(v)) for v in df[col].astype(str).tolist()])
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(max(12, longest + 4), 50)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=payroll_report.xlsx; filename*=UTF-8''" + urllib.parse.quote("تقرير_إرسال_رواتب_واتساب.xlsx")}
    )


# ---------------------------------------------------------------------------
# الإرسال التلقائي الحقيقي (Selenium)
# ---------------------------------------------------------------------------
class CampaignEmployee(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=5, max_length=30)
    message: str = Field(min_length=1, max_length=3000)


class CampaignSettings(BaseModel):
    # حدود تحقق من مدخلات الـ API، وليست وسيلة لتجاوز سياسات المنصة.
    min_delay: int = Field(default=15, ge=1, le=3600)
    max_delay: int = Field(default=30, ge=1, le=3600)
    batch_size: int = Field(default=15, ge=1, le=100)
    batch_pause: int = Field(default=120, ge=0, le=86400)


class CampaignRequest(BaseModel):
    employees: List[CampaignEmployee]
    settings: CampaignSettings = Field(default_factory=CampaignSettings)


@app.post("/api/campaign/start")
async def campaign_start(req: CampaignRequest, request: Request):
    if not req.employees:
        raise HTTPException(status_code=400, detail="لا يوجد موظفين للإرسال")
    if len(req.employees) > 5000:
        raise HTTPException(status_code=413, detail="عدد الموظفين أكبر من الحد المسموح (5000)")
    if req.settings.min_delay > req.settings.max_delay:
        raise HTTPException(status_code=422, detail="الحد الأدنى للتأخير لا يمكن أن يكون أكبر من الحد الأقصى")
    with sender.user_context(request.state.user["id"]):
        ok = sender.start_campaign([e.dict() for e in req.employees], req.settings.dict())
    if not ok:
        raise HTTPException(status_code=409, detail="يوجد إرسال أو ربط واتساب قيد التشغيل بالفعل")
    return {"status": "started"}


@app.get("/api/campaign/status")
async def campaign_status(request: Request):
    with sender.user_context(request.state.user["id"]):
        return sender.campaign.snapshot()


@app.post("/api/campaign/pause")
async def campaign_pause(request: Request):
    with sender.user_context(request.state.user["id"]):
        return {"paused": sender.pause_toggle()}


@app.post("/api/campaign/skip")
async def campaign_skip(request: Request):
    with sender.user_context(request.state.user["id"]):
        sender.skip_current()
    return {"status": "ok"}


@app.post("/api/campaign/stop")
async def campaign_stop(request: Request):
    with sender.user_context(request.state.user["id"]):
        sender.stop_campaign()
    return {"status": "ok"}


@app.post("/api/whatsapp/connect")
async def whatsapp_connect(request: Request):
    with sender.user_context(request.state.user["id"]):
        started = sender.connect_whatsapp()
    if not started:
        raise HTTPException(status_code=409, detail="لا يمكن بدء الربط أثناء حملة أو ربط قائم")
    return {"status": "started"}


@app.post("/api/whatsapp/disconnect")
async def whatsapp_disconnect(request: Request):
    with sender.user_context(request.state.user["id"]):
        sender.disconnect_whatsapp()
    return {"status": "disconnected"}


@app.post("/api/whatsapp/reload-qr")
async def whatsapp_reload_qr(request: Request):
    with sender.user_context(request.state.user["id"]):
        ok = sender.reload_qr()
    return {"status": "ok" if ok else "failed"}


class PairPhoneRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=30)
    country_code: str = Field(default="20", max_length=10)


@app.post("/api/whatsapp/pair-phone")
async def whatsapp_pair_phone(req: PairPhoneRequest, request: Request):
    with sender.user_context(request.state.user["id"]):
        ok, msg, code = sender.request_phone_pairing(req.phone, req.country_code)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "ok", "message": msg, "pairing_code": code}


@app.post("/api/whatsapp/cancel-pairing")
async def whatsapp_cancel_pairing(request: Request):
    with sender.user_context(request.state.user["id"]):
        sender.cancel_phone_pairing()
    return {"status": "ok"}


class SendSingleRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=30)
    message: str = Field(min_length=1, max_length=3000)


@app.post("/api/whatsapp/send-single")
async def whatsapp_send_single(req: SendSingleRequest, request: Request):
    with sender.user_context(request.state.user["id"]):
        st = sender.whatsapp_status()
        if not st.get("logged_in"):
            raise HTTPException(status_code=400, detail="واتساب غير مربوط حالياً. قم بمسح كود الـ QR أولاً.")
        try:
            driver = sender._get_driver()
            ok, detail, status = sender._send_one(driver, req.phone, req.message)
            return {"success": ok, "detail": detail, "status": status.value}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"خطأ أثناء الإرسال: {type(e).__name__}")


@app.get("/api/whatsapp/status")
async def whatsapp_status(request: Request):
    with sender.user_context(request.state.user["id"]):
        return sender.whatsapp_status()

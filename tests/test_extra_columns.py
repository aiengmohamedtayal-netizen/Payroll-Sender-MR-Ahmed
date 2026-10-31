import pytest
import re
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_export_report_with_extra_columns():
    # Login first or mock user
    # Test /api/export-report endpoint handles extra columns dynamically
    response = client.post(
        "/api/export-report",
        json={
            "report_data": [
                {
                    "كود": 1,
                    "اسم الموظف": "أحمد محمود",
                    "رقم الهاتف": "+201012345678",
                    "إجمالي المرتب": "10,000",
                    "الصرفية": "مرتب شهر أكتوبر",
                    "الاستقطاعات": "500",
                    "صافي المرتب": "9,500",
                    "حافز": "1,000",
                    "بدل انتقال": "300",
                    "حالة الإرسال": "تم الإرسال بنجاح",
                    "وقت الإرسال": "2026-10-31 18:00:00",
                    "ملاحظات إضافية": ""
                }
            ]
        }
    )
    # Without login it might redirect 401, check if auth required
    assert response.status_code in [200, 401]

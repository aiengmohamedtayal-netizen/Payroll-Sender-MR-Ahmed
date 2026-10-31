"""نقطة تشغيل البرنامج (بتتحوّل لـ PayrollSender.exe)."""
import os
import socket
import sys
import threading
import time
import webbrowser

if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))

import uvicorn
from main import app


def free_port(start=8000):
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


if __name__ == "__main__":
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    print("=" * 56)
    print("  مرسل رواتب واتساب - شغّال")
    print(f"  الرابط: {url}")
    print("  ماتقفلش النافذة دي طول ما بتستخدم الموقع!")
    print("=" * 56)
    threading.Thread(target=lambda: (time.sleep(2), webbrowser.open(url)), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

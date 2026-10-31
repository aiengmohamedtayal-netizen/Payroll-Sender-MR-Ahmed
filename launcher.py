"""نقطة تشغيل البرنامج (بتتحوّل لـ PayrollSender.exe)."""
import os
import socket
import sys
import threading
import time
import webbrowser
import multiprocessing

# Safe UTF-8 reconfiguration on Windows console
if sys.platform.startswith("win"):
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

if getattr(sys, "frozen", False):
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
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
    multiprocessing.freeze_support()
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    print("=" * 60)
    print("  مرسل رواتب واتساب - قيد التشغيل")
    print(f"  الرابط: {url}")
    print("  يرجى عدم إغلاق هذه النافذة أثناء استخدام البرنامج.")
    print("=" * 60)
    threading.Thread(target=lambda: (time.sleep(2), webbrowser.open(url)), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

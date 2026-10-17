# -*- coding: utf-8 -*-
"""srvtest.py — быстрая проверка, что локальный HTTP-сервер отдаёт ui/index.html.

Путь к ui/ вычисляется от __file__ (абсолютные пути песочницы не хардкодим).
"""
import functools
import http.server
import os
import threading
import time
import urllib.request

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")

h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=UI_DIR)
s = http.server.ThreadingHTTPServer(("127.0.0.1", 8799), h)
threading.Thread(target=s.serve_forever, daemon=True).start()
time.sleep(0.5)
try:
    r = urllib.request.urlopen("http://127.0.0.1:8799/index.html", timeout=5)
    print("index.html:", r.status, len(r.read()), "байт")
except Exception as e:
    print("ОШИБКА:", e)
s.shutdown()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zoom_test.py — тест окна-лупы (ZOOM_VIEWER_HTML из api.py) в headless-Chromium.

Проверяет именно то, на что жаловался пользователь:
  1. окно можно двигать за верхнюю панель (pywebviewMoveWindow);
  2. увеличенную картинку можно перемещать внутри окна (панорамирование);
  3. зум колесом работает и меняет процент;
  4. кнопки +/−/сброс и клавиатура (стрелки, Esc) работают.

Запуск:  python3 tests/zoom_test.py
"""
import os
import re
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from playwright.sync_api import sync_playwright  # noqa: E402

# Достаём шаблон viewer'а прямо из исходника api.py (без импорта тяжёлых модулей)
api_src = open(os.path.join(PROJECT_ROOT, "api.py"), encoding="utf-8").read()
m = re.search(r'ZOOM_VIEWER_HTML = """(.*?)"""', api_src, re.S)
if not m:
    print("НЕ НАЙДЕН ZOOM_VIEWER_HTML в api.py")
    sys.exit(1)
html = m.group(1)

import base64 as _b64
import io as _io
from PIL import Image as _Image, ImageDraw as _Draw
_im = _Image.new("RGB", (320, 220), "white")
_d = _Draw.Draw(_im)
_d.rectangle([20, 20, 300, 200], outline="black", width=3)
_d.text((40, 100), "ZOOM TEST 123", fill="black")
_buf = _io.BytesIO()
_im.save(_buf, "PNG")
TINY_PNG = "data:image/png;base64," + _b64.b64encode(_buf.getvalue()).decode("ascii")
html = html.replace("__SRC__", TINY_PNG).replace("__NAME__", "zoom_test.png")

tmpdir = tempfile.mkdtemp(prefix="zoom_test_")
page_path = os.path.join(tmpdir, "zoom.html")
with open(page_path, "w", encoding="utf-8") as f:
    f.write(html)

MOCK = """
window.__moves = [];
window.__closed = 0;
window.__errors = [];
window.addEventListener('error', e => window.__errors.push('error: ' + e.message));
window.pywebview = {
  _jsApiCallback: function(name, params, id) { window.__moves.push({name: String(name), params: params}); },
  api: { close_zoom: async function() { window.__closed++; return {ok: true}; } }
};
"""

results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("✓" if cond else "✗"), name, ("" if cond else extra))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 900, "height": 640})
    page.add_init_script(MOCK)
    page.goto("file://" + page_path)
    page.wait_for_timeout(600)   # появление + анимация

    # 1) стартовый масштаб
    pct0 = page.inner_text("#zoomPct")
    check("стартовый масштаб 100%", pct0.strip() == "100%", pct0)

    # 2) зум колесом к курсору
    page.mouse.move(450, 340)
    page.mouse.wheel(0, -120); page.wait_for_timeout(250)
    page.mouse.wheel(0, -120); page.wait_for_timeout(250)
    pct1 = int(page.inner_text("#zoomPct").replace("%", "").strip() or 0)
    check("колесо увеличивает масштаб (>100%)", pct1 > 100, str(pct1))
    tr = page.evaluate("document.getElementById('img').style.transform")
    check("зум меняет transform (scale + translate)", "scale" in tr and "translate" in tr, tr[:80])

    # 3) панорамирование картинки мышью (главная жалоба: «не двигается внутри»)
    t_before = page.evaluate("document.getElementById('img').style.transform")
    page.mouse.move(450, 340)
    page.mouse.down()
    page.mouse.move(530, 390, steps=8)
    page.mouse.up()
    page.wait_for_timeout(200)
    t_after = page.evaluate("document.getElementById('img').style.transform")
    check("картинка сдвинулась за мышью (translate изменился)", t_before != t_after,
          f"{t_before[:50]} -> {t_after[:50]}")
    tx = float(re.search(r"translate\(([-\d.]+)px", t_after).group(1))
    check("сдвиг вправо на ~+80px", 60 < tx < 100, str(tx))

    # 4) перетаскивание ОКНА за верхнюю панель
    page.evaluate("window.__moves = []")
    page.mouse.move(200, 18)          # панель заголовка (не по кнопкам)
    page.mouse.down()
    page.mouse.move(320, 108, steps=10)
    page.mouse.up()
    page.wait_for_timeout(200)
    moves = page.evaluate("window.__moves")
    drag_ok = any(mv.get("name") == "pywebviewMoveWindow" for mv in moves)
    check("перетаскивание за панель двигает ОКНО (pywebviewMoveWindow)", drag_ok,
          str(moves[:2])[:120])

    # 5) кнопки зума и сброс
    page.click("#zoomIn"); page.wait_for_timeout(200)
    pct2 = int(page.inner_text("#zoomPct").replace("%", "").strip() or 0)
    check("кнопка «+» увеличивает", pct2 > pct1, f"{pct1}->{pct2}")
    page.click("#zoomOut"); page.wait_for_timeout(200)
    pct3 = int(page.inner_text("#zoomPct").replace("%", "").strip() or 0)
    check("кнопка «−» уменьшает", pct3 < pct2, f"{pct2}->{pct3}")
    page.click("#zoomPct"); page.wait_for_timeout(250)
    check("клик по процентам сбрасывает в 100%",
          page.inner_text("#zoomPct").strip() == "100%")

    # 6) клавиатура: стрелки двигают картинку, Esc закрывает
    t0 = page.evaluate("document.getElementById('img').style.transform")
    page.keyboard.press("ArrowRight"); page.wait_for_timeout(150)
    t1 = page.evaluate("document.getElementById('img').style.transform")
    check("стрелка → сдвиг картинки", t0 != t1)
    page.keyboard.press("Escape"); page.wait_for_timeout(250)
    closed = page.evaluate("window.__closed")
    check("Esc закрывает окно (close_zoom)", closed >= 1, str(closed))

    errs = page.evaluate("window.__errors")
    check("нет JS-ошибок в viewer'е", len(errs) == 0, str(errs)[:200])
    browser.close()

ok = sum(1 for _, c in results if c)
print(f"\nИТОГ: {ok}/{len(results)} проверок пройдено")
sys.exit(0 if ok == len(results) else 1)

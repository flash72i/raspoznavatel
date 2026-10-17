#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_screenshots.py — красивые скриншоты интерфейса для README (главная GitHub).

Генерирует docs/screenshots/:
  main.png      — главное окно: загруженная картинка, распознанный текст, перевод;
  settings.png  — панель настроек (горячие клавиши, автозапуск, звук, ✕, папка);
  zoom.png      — окно-лупа с увеличенной картинкой.

Запуск:  python3 tests/make_screenshots.py
"""
import base64
import io
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from playwright.sync_api import sync_playwright  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

OUT = os.path.join(PROJECT_ROOT, "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)

# --- образец «документа» для превью ---
def sample_doc_png() -> str:
    try:
        f1 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
        f2 = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except Exception:
        f1 = f2 = ImageFont.load_default()
    im = Image.new("RGB", (640, 400), "#fdfdf7")
    d = ImageDraw.Draw(im)
    d.rectangle([8, 8, 632, 392], outline="#c8c8b8", width=2)
    d.text((30, 34), "ИНВОЙС № INV-2026-114", fill="#111", font=f1)
    d.text((30, 96), "Поставщик: Romashka Trading LLC", fill="#222", font=f2)
    d.text((30, 136), "Contract dated 12 October 2026", fill="#222", font=f2)
    d.text((30, 176), "Наименование: Equipment set A-12", fill="#222", font=f2)
    d.text((30, 216), "Total amount: 12 500,00 RUB", fill="#222", font=f2)
    d.text((30, 256), "Срок действия: до 31.12.2026", fill="#222", font=f2)
    d.rectangle([30, 300, 200, 302], fill="#888")
    d.text((30, 316), "Подпись: A. Ivanov", fill="#333", font=f2)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


MOCK = r"""
window.__calls = [];
const TINY = 'DOCDataURL';
const responses = {
  get_info: {name:'Распознаватель', version:'2.4.0', frozen:true, data_dir:'C:/Rasp/Raspoznavatel_data', screenshots_dir:'C:/Rasp/Screenshots'},
  get_settings: {ocr_langs:'auto', auto_copy:true, smart_hotkey:true, hotkey_recognize:'ctrl+shift+s', hotkey_screenshot:'ctrl+shift+p', autostart:true, preload:true, sound:true, screenshots_dir:'', close_action:'tray'},
  save_settings: (a) => Object.assign({ok:true}, a[0]),
  get_available_langs: [{code:'rus',name:'Русский'},{code:'eng',name:'Английский'}],
  get_translation_pairs: {ok:true, pairs:[{src:'en',tgt:'ru',label:'Английский → Русский'},{src:'ru',tgt:'en',label:'Русский → Английский'}]},
  get_autostart: {enabled:true, path:'Startup/Raspoznavatel.lnk'},
  preload_translator: {ok:true},
  js_log: {ok:true}, js_error: {ok:true},
  get_log: '[20:41:02] [INFO] Запуск «Распознаватель» v2.4.0\n[20:41:03] [OK] Интерфейс загружен. Языков OCR доступно: 15\n[20:41:03] [OK] Горячая клавиша распознавания: ctrl+shift+s\n[20:41:03] [OK] Горячая клавиша скриншота: ctrl+shift+p\n[20:41:05] [OK] Загружен файл: invoice.png (187 КБ)\n[20:41:07] [OK] Авто: найдено кодов 0 — распознаю текст\n[20:41:09] [OK] OCR завершён: 214 симв., уверенность 96.2%, языки rus+eng\n[20:41:21] [OK] Перевод en→ru готов (188 симв.)',
  open_screenshots_dir: {ok:true}, open_data_dir: {ok:true},
  check_components: {ocr:{ok:true,version:'5.5.3',langs:['rus','eng','chi_sim'],error:null},
    qr:{'zxing-cpp':true,'pyzbar (ZBar)':true,'opencv':true,ok:true},
    screen:true, clipboard:true, tray:true,
    translator:{ok:true,library:true,models:['translate-en_ru','translate-ru_en'],error:null}},
  win_minimize:{ok:true}, win_maximize_toggle:{ok:true,maximized:false}, win_close:{ok:true}, win_resize:{ok:true},
};
window.pywebview = { api: new Proxy({}, { get(t, p) {
  return async (...args) => { const r = responses[p];
    if (r === undefined) return {ok:true};
    return typeof r === 'function' ? r(args) : JSON.parse(JSON.stringify(r)); };
}})};
""".replace("DOCDataURL", "")

RECOGNIZED = ("ИНВОЙС № INV-2026-114\n"
              "Поставщик: Romashka Trading LLC\n"
              "Contract dated 12 October 2026\n"
              "Наименование: Equipment set A-12\n"
              "Total amount: 12 500,00 RUB\n"
              "Срок действия: до 31.12.2026\n"
              "Подпись: A. Ivanov")
TRANSLATED = ("СЧЁТ-ФАКТУРА № INV-2026-114\n"
              "Поставщик: Ромашка Трейдинг ООО\n"
              "Договор от 12 октября 2026 года\n"
              "Наименование: Комплект оборудования А-12\n"
              "Итого: 12 500,00 руб.\n"
              "Действителен до: 31.12.2026\n"
              "Подпись: А. Иванов")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    # --- локальный сервер для ui/ ---
    import http.server, threading
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8899), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    page = browser.new_page(viewport={"width": 1240, "height": 860}, device_scale_factor=2)
    page.add_init_script(MOCK)
    page.goto("http://127.0.0.1:8899/ui/index.html")
    page.wait_for_timeout(2600)

    doc_url = sample_doc_png()
    # заполняем окно «результатами работы»
    page.evaluate("""(doc) => {
        setPreview(doc, 'invoice.png (187 КБ)');
        showTextResult(%s, 'Распознанный текст · языки: RUS + ENG');
        document.querySelector('#mainOutput').value = %s;
        document.querySelector('#trOutput').value = %s;
        setStatus('Перевод завершён · EN → RU');
        window.onLogBulk(get_log_lines());
        function get_log_lines(){ return []; }
    }""" % (repr(RECOGNIZED), repr(RECOGNIZED), repr(TRANSLATED)), doc_url)
    page.wait_for_timeout(3200)   # ждём завершение typewriter-анимации
    page.screenshot(path=os.path.join(OUT, "main.png"))
    print("main.png ✓")

    # --- настройки ---
    page.click("#settingsBtn")
    page.wait_for_timeout(900)
    page.screenshot(path=os.path.join(OUT, "settings.png"))
    print("settings.png ✓")
    page.click("#drawerClose")
    page.wait_for_timeout(400)
    page.close()

    # --- окно-лупа ---
    api_src = open(os.path.join(PROJECT_ROOT, "api.py"), encoding="utf-8").read()
    html = re.search(r'ZOOM_VIEWER_HTML = """(.*?)"""', api_src, re.S).group(1)
    html = html.replace("__SRC__", doc_url).replace("__NAME__", "invoice.png — лупа")
    zpath = os.path.join(OUT, "_zoom.html")
    open(zpath, "w", encoding="utf-8").write(html)
    zpage = browser.new_page(viewport={"width": 980, "height": 700}, device_scale_factor=2)
    zpage.add_init_script("window.pywebview={api:{close_zoom:async()=>({ok:true})},_jsApiCallback:function(){}};")
    zpage.goto("file://" + zpath)
    zpage.wait_for_timeout(800)
    # имитируем: увеличено до ~200% и сдвинуто, чтобы было видно «чтение текста»
    zpage.evaluate("""() => {
        scale = 2.1; tx = -140; ty = 90; apply();
        img.classList.add('shown');
    }""")
    zpage.wait_for_timeout(500)
    zpage.screenshot(path=os.path.join(OUT, "zoom.png"))
    print("zoom.png ✓")
    os.remove(zpath)
    browser.close()
    httpd.shutdown()

for f in ("main.png", "settings.png", "zoom.png"):
    pth = os.path.join(OUT, f)
    print(f, os.path.getsize(pth) // 1024, "КБ")

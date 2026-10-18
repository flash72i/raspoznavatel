#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_test.py — автоматический тест интерфейса «Распознаватель» в headless-Chromium.

Подменяет Python-API заглушкой (window.pywebview.api), кликает ВСЕ кнопки,
проверяет реакции интерфейса и ловит любые JS-ошибки. Скриншоты сохраняются
в /tmp/ui_shots/ для визуальной проверки.
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

PAGE = "http://127.0.0.1:8765/ui/index.html"
SHOTS = "/tmp/ui_shots"

# локальный HTTP-сервер (headless-браузер не любит file://)
import http.server
import threading
import os
os.makedirs(SHOTS, exist_ok=True)
# ВАЖНО: путь вычисляем от __file__ (абсолютные /home/... в файлах не работают в песочнице)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)   # отдаём ui/ относительно корня проекта
sys.path.insert(0, PROJECT_ROOT)
import config as app_config          # noqa: E402
APP_VER = app_config.APP_VERSION     # версия берётся из config — тест не устаревает
_httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), http.server.SimpleHTTPRequestHandler)
threading.Thread(target=_httpd.serve_forever, daemon=True).start()

MOCK_JS = r"""
window.__calls = [];
window.__errors = [];
window.addEventListener('error', e => window.__errors.push('error: ' + e.message));
window.addEventListener('unhandledrejection', e => window.__errors.push('reject: ' + e.reason));

const TINY_PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';

const responses = {
  get_info: {name:'Распознаватель', version:'__APP_VERSION__', frozen:true, data_dir:'C:/Raspoznavatel/Raspoznavatel_data', screenshots_dir:'C:/Raspoznavatel/Screenshots'},
  get_settings: {ocr_langs:'auto', auto_copy:true, smart_hotkey:true, hotkey_recognize:'ctrl+shift+s', hotkey_screenshot:'ctrl+shift+p', autostart:false, preload:true, sound:true, screenshots_dir:'', close_action:'exit'},
  save_settings: (args) => Object.assign({ok: true}, args[0]),
  get_available_langs: [{code:'rus',name:'Русский'},{code:'eng',name:'Английский'},{code:'deu',name:'Немецкий'}],
  pick_image: {ok:true, path:'C:/test/img.png', name:'img.png', base64:TINY_PNG},
  save_dropped_file: {ok:true, path:'C:/tmp/drop_qr.png', name:'drop_qr.png', base64:TINY_PNG},
  recognize_file: (args) => String(args[0]).indexOf('qr') >= 0
    ? {ok:true, mode:'qr', count:2, results:[{type:'QRCode', text:'https://example.com/привет', engine:'zxing-cpp'},{type:'EAN-13', text:'4600000000005', engine:'zxing-cpp'}]}
    : {ok:true, mode:'ocr', text:'Hello world. Проверка OCR.', lang:'rus+eng', confidence:95.5, words:4, detected:'rus+eng'},
  get_translation_pairs: {ok:true, pairs:[{src:'en',tgt:'ru',label:'Английский → Русский'},{src:'ru',tgt:'en',label:'Русский → Английский'}]},
  open_image_zoom: {ok:true},
  ocr_image: {ok:true, text:'Hello world. Проверка OCR.', lang:'eng', confidence:95.5, words:4, detected:'eng'},
  decode_image: {ok:true, count:2, results:[{type:'QRCode', text:'https://example.com/привет', engine:'zxing-cpp'},{type:'EAN-13', text:'4600000000005', engine:'zxing-cpp'}]},
  capture_smart: {ok:true, mode:'ocr', shot:{base64:TINY_PNG, width:300, height:120}, text:'Текст с экрана распознан', lang:'rus', confidence:91.2, detected:'rus'},
  capture_and_ocr: {ok:true, mode:'ocr', shot:{base64:TINY_PNG, width:300, height:120}, text:'Текст с экрана', lang:'rus', confidence:90, detected:'rus'},
  capture_screenshot: {ok:true, path:'C:/Raspoznavatel/Screenshots/Скриншот_2026-10-16_12-00-00.png', width:400, height:200, base64:TINY_PNG},
  translate_text: (args) => ({ok:true, translated:'Привет, мир. Проверка перевода.', src:args[1], tgt:args[2]}),
  copy_to_clipboard: {ok:true},
  get_log: '[12:00:00] [INFO] тестовая строка 1\n[12:00:01] [OK] тестовая строка 2\n[12:00:02] [ERROR] тестовая ошибка 3',
  clear_log: {ok:true},
  check_components: {
    ocr:{ok:true, version:'5.5.3', langs:['rus','eng','chi_sim','deu'], error:null},
    qr:{'zxing-cpp':true,'pyzbar (ZBar)':true,'opencv':true, ok:true},
    screen:true, clipboard:true, tray:true,
    translator:{ok:true, library:true, models:['translate-en_ru-1_9.argosmodel','translate-ru_en-1_9.argosmodel'], pairs:['en->ru','ru->en'], error:null}
  },
  get_autostart: {enabled:false, path:'C:/Users/test/AppData/.../Startup/Raspoznavatel.lnk'},
  preload_translator: {ok:true},
  open_data_dir: {ok:true, path:'C:/data'},
  open_screenshots_dir: {ok:true, path:'C:/shots'},
  choose_screenshots_dir: {ok:true, path:'C:/MyShots', default:'C:/Raspoznavatel/Screenshots'},
  reset_screenshots_dir: {ok:true, path:'C:/Raspoznavatel/Screenshots', default:'C:/Raspoznavatel/Screenshots'},
  get_history: (args) => ({ok:true, total:2, items:[
    {id:'h1', ts:'2026-10-17T12:00:00', kind:'ocr', source:'screen', langs:'rus', confidence:93.1, text:'Текст из истории распознавания'},
    {id:'h2', ts:'2026-10-17T11:30:00', kind:'qr', source:'file', count:1, text:'https://example.com/qr'}]}),
  history_delete: {ok:true},
  history_clear: {ok:true},
  get_translation_langs: {ok:true,
    catalog:{en:'Английский', ru:'Русский', de:'Немецкий', fr:'Французский'},
    packs:[{src:'en',tgt:'ru',size:205258752,bundled:true,path:'x'},{src:'ru',tgt:'en',size:156237824,bundled:true,path:'x'}],
    installed:[['en','ru'],['ru','en']],
    remote:[{src:'en',tgt:'de',size:91000000},{src:'de',tgt:'en',size:88000000},{src:'en',tgt:'fr',size:95000000}]},
  download_translation_pack: {ok:true, path:'x', size:91000000},
  remove_translation_pack: {ok:true},
  win_minimize: {ok:true, via:'win32'},
  win_maximize_toggle: {ok:true, maximized:true, via:'win32'},
  win_close: {ok:true},
  win_resize: {ok:true},
  js_log: {ok:true},
  js_error: {ok:true},
};

window.pywebview = {
  api: new Proxy({}, {
    get(target, prop) {
      return async function(...args) {
        window.__calls.push({m: String(prop), args: args.map(a => { try { return typeof a === 'object' ? JSON.stringify(a).slice(0,300) : String(a).slice(0,80); } catch(e) { return String(a); } })});
        const r = responses[prop];
        if (r === undefined) throw new Error('mock: неизвестный метод ' + String(prop));
        return typeof r === 'function' ? r(args) : JSON.parse(JSON.stringify(r));
      };
    }
  })
};
"""

MOCK_JS = MOCK_JS.replace("__APP_VERSION__", APP_VER)

results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("✓" if cond else "✗"), name, extra if not cond else "")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    page.add_init_script(MOCK_JS)
    page.goto(PAGE)
    page.wait_for_timeout(2200)  # onReady + анимации

    def calls(method):
        return page.evaluate(f"window.__calls.filter(c => c.m === '{method}').length")

    # --- 1. Кнопки панели заголовка ---
    page.click("#btnMin"); page.wait_for_timeout(300)
    check("кнопка «свернуть» вызывает win_minimize", calls("win_minimize") == 1)
    page.click("#btnMax"); page.wait_for_timeout(300)
    check("кнопка «развернуть» вызывает win_maximize_toggle", calls("win_maximize_toggle") == 1)

    # --- 1б. v2.3.1: значок папки виден СРАЗУ; лоадер не блокирует; прогресс в статус-баре ---
    check("значок папки скриншотов виден сразу", page.is_visible("#statusFolderBtn"))
    page.evaluate("showLoader('проверка блокировки')")
    page.wait_for_timeout(300)
    page.click("#settingsBtn"); page.wait_for_timeout(600)   # кликабельно ПОД лоадером?
    check("лоадер НЕ блокирует интерфейс (настройки открылись под ним)",
          page.evaluate("document.querySelector('#settingsDrawer').classList.contains('open')"))
    page.click("#drawerClose"); page.wait_for_timeout(300)
    page.evaluate("window.onProgress(66, 'стадия X')")
    page.wait_for_timeout(350)
    sp = page.evaluate("document.querySelector('#statusProgressFill').style.width")
    sp_active = page.evaluate("document.querySelector('#statusProgress').classList.contains('active')")
    check("прогресс виден В СТАТУС-БАРЕ (66%)", sp == "66%" and sp_active, f"{sp} active={sp_active}")
    page.evaluate("hideLoader(); window.onProgress(-1, '')")
    page.wait_for_timeout(250)

    # --- 2. Загрузка изображения + АВТОраспознавание ---
    page.click("#pickBtn"); page.wait_for_timeout(2600)
    check("«Загрузить изображение» → pick_image", calls("pick_image") == 1)
    check("превью появилось", page.is_visible("#previewCard"))
    check("автораспознавание БЕЗ кнопок (recognize_file)", calls("recognize_file") == 1)
    val = page.input_value("#mainOutput")
    check("распознанный текст сразу в поле", "Hello world" in val, val[:40])
    check("кнопки «Распознать текст»/«Найти QR» убраны",
          page.evaluate("!document.querySelector('#runOcrBtn') && !document.querySelector('#runQrBtn')"))
    check(f"версия {APP_VER} в интерфейсе", page.inner_text("#versionLabel") == APP_VER,
          page.inner_text("#versionLabel"))

    # --- 3. Авто-режим QR (путь с 'qr' → мок вернёт коды) ---
    page.evaluate("window.autoRecognize('C:/tmp/drop_qr.png')")
    page.wait_for_timeout(1500)
    check("QR-режим: карточки видны", page.is_visible("#qrResults"))
    qr_texts = page.inner_text("#qrResults")
    check("QR-текст отображён", "example.com" in qr_texts, qr_texts[:50])

    # --- 3б. Лупа: клик по превью открывает отдельное окно ---
    page.click("#previewImg"); page.wait_for_timeout(400)
    check("клик по картинке → open_image_zoom", calls("open_image_zoom") == 1)

    # --- 3в. Шкала прогресса ---
    page.evaluate("window.onProgress(42, 'тестовая стадия')")
    page.wait_for_timeout(450)
    w42 = page.evaluate("document.querySelector('#progressFill').style.width")
    check("шкала прогресса показывает 42%", w42 == "42%", w42)
    page.evaluate("window.onProgress(-1, '')")
    page.wait_for_timeout(200)

    # --- 4. Копирование / перевод + выбор направления ---
    page.click("#copyBtn"); page.wait_for_timeout(300)
    check("«Копировать» → copy_to_clipboard", calls("copy_to_clipboard") >= 1)
    n_opts = page.evaluate("document.querySelector('#trFrom').options.length")
    check("селекторы направления заполнены (EN/RU)", n_opts >= 2, str(n_opts))
    page.click("#trSwap"); page.wait_for_timeout(250)
    swapped = page.evaluate("document.querySelector('#trFrom').value")
    both = page.evaluate("document.querySelector('#trFrom2').value")
    check("⇄ меняет направление на RU→EN", swapped == "ru", swapped)
    check("селекторы синхронны в ОБОИХ окнах", both == "ru", both)
    page.click("#translateBtn"); page.wait_for_timeout(1200)
    check("«Перевести» → translate_text", calls("translate_text") == 1)
    targs = page.evaluate("window.__calls.filter(c=>c.m==='translate_text').slice(-1)[0].args")
    check("направление (ru,en) передано в API", len(targs) >= 3 and targs[1] == "ru" and targs[2] == "en", str(targs))
    tr = page.input_value("#trOutput")
    check("перевод появился", "перевода" in tr, tr[:40])

    # --- 5. Экран и скриншоты ---
    page.click("#screenBtn"); page.wait_for_timeout(1400)
    check("«Распознать с экрана» → capture_smart", calls("capture_smart") == 1)
    page.click("#shotBtn"); page.wait_for_timeout(800)
    check("«Скриншот области» → capture_screenshot(False)", calls("capture_screenshot") == 1)
    check("кнопка «Сохранить как…» убрана", page.evaluate("!document.querySelector('#shotAsBtn')"))
    check("значок папки появился в статус-баре", page.is_visible("#statusFolderBtn"))
    page.click("#statusFolderBtn"); page.wait_for_timeout(300)
    check("клик по значку → open_screenshots_dir", calls("open_screenshots_dir") >= 1)

    # --- 6. Настройки: drawer, журнал, диагностика, звук ---
    page.click("#settingsBtn"); page.wait_for_timeout(700)
    check("панель настроек открылась", page.evaluate("document.querySelector('#settingsDrawer').classList.contains('open')"))
    logv = page.inner_text("#logBox")
    check("журнал загружен в панель", "тестовая строка" in logv, logv[:40])
    check("строки журнала окрашены по уровням (ok/info)",
          page.evaluate("document.querySelectorAll('#logBox .log-ok').length") >= 1 and
          page.evaluate("document.querySelectorAll('#logBox .log-line').length") >= 2)
    check("ошибки КРАСНЫМ (.log-err)",
          page.evaluate("document.querySelectorAll('#logBox .log-err').length") >= 1)
    page.click("label:has(#setSound)"); page.wait_for_timeout(400)
    snd = page.evaluate("window.__calls.filter(c=>c.m==='save_settings').slice(-1)[0].args[0]")
    check("переключатель звука → save_settings c sound=false", '"sound":false' in snd, snd[-120:])
    page.select_option("#setCloseAction", "tray"); page.wait_for_timeout(400)
    ca = page.evaluate("window.__calls.filter(c=>c.m==='save_settings').slice(-1)[0].args[0]")
    check("действие ✕ «Свернуть в трей» → save_settings c close_action=tray", '"close_action":"tray"' in ca, ca[-140:])
    page.select_option("#setCloseAction", "exit"); page.wait_for_timeout(300)
    page.click("#chooseShotsDirBtn"); page.wait_for_timeout(400)
    check("«Выбрать папку…» → choose_screenshots_dir", calls("choose_screenshots_dir") == 1)
    shots = page.inner_text("#shotsDirPath")
    check("новая папка показана в настройках", "C:/MyShots" in shots, shots[:60])
    page.click("#resetShotsDirBtn"); page.wait_for_timeout(400)
    check("«По умолчанию» → reset_screenshots_dir", calls("reset_screenshots_dir") == 1)
    shots2 = page.inner_text("#shotsDirPath")
    check("сброс папки показан", "по умолчанию" in shots2.lower() or "Screenshots" in shots2, shots2[:60])
    page.click("#copyLogBtn"); page.wait_for_timeout(300)
    check("«Копировать журнал» → copy_to_clipboard", calls("copy_to_clipboard") >= 2)
    page.click("#checkBtn"); page.wait_for_timeout(800)
    diag = page.inner_text("#diagResult")
    check("диагностика отрисована", "Перевод" in diag and "Иконка" in diag, diag[:80])
    page.screenshot(path=f"{SHOTS}/drawer.png")

    # --- 7. Запись горячей клавиши ---
    page.click("#hkRecBtn"); page.wait_for_timeout(200)
    page.keyboard.press("F5"); page.wait_for_timeout(300)
    check("одиночная клавиша (F5) НЕ записывается — ждём комбинацию",
          page.input_value("#hkRecognize") == "", page.input_value("#hkRecognize"))
    page.keyboard.down("Control"); page.keyboard.press("F9"); page.keyboard.up("Control")
    page.wait_for_timeout(600)
    hk = page.input_value("#hkRecognize")
    check("комбинация записана и красиво отображена (Ctrl + F9)", hk == "Ctrl + F9", hk)
    saved = page.evaluate("window.__calls.filter(c=>c.m==='save_settings').slice(-1)[0].args[0]")
    check("в save_settings ушло сырое значение ctrl+f9", "ctrl+f9" in saved, saved[:80])
    stat = page.inner_text("#statusHotkeys")
    check("строка статуса обновилась (Ctrl + F9)", "Ctrl + F9" in stat, stat[:80])

    # --- 8. Автозапуск ---
    page.click("label:has(#setAutostart)"); page.wait_for_timeout(400)
    check("автозапуск → save_settings с autostart=true",
          "true" in page.evaluate("window.__calls.filter(c=>c.m==='save_settings').slice(-1)[0].args[0]").lower())

    # --- 9. Переключатели поведения ---
    page.click("label:has(#setAutoCopy)"); page.wait_for_timeout(300)
    check("переключатель auto_copy сработал", calls("save_settings") >= 3)

    page.click("#drawerClose"); page.wait_for_timeout(500)
    check("панель настроек закрылась", not page.evaluate("document.querySelector('#settingsDrawer').classList.contains('open')"))

    # --- 10. Очистка результата ---
    page.click("#clearBtn"); page.wait_for_timeout(300)
    check("«Очистить» очищает поле", page.input_value("#mainOutput") == "")

    # --- 10б. v2.5: история распознаваний ---
    page.click("#historyBtn"); page.wait_for_timeout(700)
    check("«История» открывает панель истории",
          page.evaluate("document.querySelector('#historyDrawer').classList.contains('open')"))
    check("история отрисована (2 записи)",
          page.evaluate("document.querySelectorAll('#historyList .hist-item').length") == 2)
    page.fill("#historySearch", "example"); page.wait_for_timeout(800)
    check("поиск шлёт запрос в get_history",
          page.evaluate("window.__calls.filter(c=>c.m==='get_history').slice(-1)[0].args[1]") == "example")
    page.fill("#historySearch", ""); page.wait_for_timeout(700)
    page.click("#historyList .hist-use"); page.wait_for_timeout(900)
    check("«Вставить в результат» подставляет текст",
          "истории" in page.input_value("#mainOutput"), page.input_value("#mainOutput")[:40])
    check("панель истории закрылась после вставки",
          not page.evaluate("document.querySelector('#historyDrawer').classList.contains('open')"))
    page.click("#historyBtn"); page.wait_for_timeout(500)
    page.click("#historyList .hist-del"); page.wait_for_timeout(500)
    check("удаление записи → history_delete", calls("history_delete") == 1)
    page.click("#historyClose"); page.wait_for_timeout(400)
    check("панель истории закрылась крестиком",
          not page.evaluate("document.querySelector('#historyDrawer').classList.contains('open')"))

    # --- 10в. v2.5: языки перевода — «Авто» и скачиваемые пакеты ---
    check("в исходном языке есть пункт «Авто»",
          page.evaluate("Array.from(document.querySelector('#trFrom').options).some(o=>o.value==='auto')"))
    check("в языках перевода доступны скачиваемые (немецкий)",
          page.evaluate("Array.from(document.querySelector('#trTo').options).some(o=>o.value==='de')"))
    packs = page.evaluate("document.querySelectorAll('#packsList .pack-row').length")
    check("список пакетов перевода отрисован (2 встроенных)", packs == 2, str(packs))

    # --- 11. JS-ошибки за всё время ---
    errs = page.evaluate("window.__errors")
    check("нет JS-ошибок", len(errs) == 0, json.dumps(errs, ensure_ascii=False)[:300])

    # --- 12. Скриншоты интерфейса ---
    page.screenshot(path=f"{SHOTS}/main.png")
    page.click("#btnClose"); page.wait_for_timeout(300)
    check("крестик вызывает win_close", calls("win_close") == 1)

    browser.close()

ok = sum(1 for _, c, _ in results if c)
print(f"\nИТОГ: {ok}/{len(results)} проверок пройдено")
sys.exit(0 if ok == len(results) else 1)

# -*- coding: utf-8 -*-
"""
test_logic.py — юнит-тесты логики «Распознаватель» v2.2 (работают без GUI,
в т.ч. на Linux). Проверяют ИМЕННО те места, где были баги v2.1:

  python3 tests/test_logic.py

1. журнал до загрузки страницы буферизуется и НЕ блокирует старт (evaluate_js
   не вызывается до loaded — раньше каждая строка висела по 20 с);
2. после события loaded буфер долетает до интерфейса одним пакетом;
3. сбои evaluate_js при доставке журнала не вызывают рекурсию;
4. safe_call: любое исключение метода API пишется в файл журнала;
5. диалоги файлов: OPEN=10, SAVE=30, параметр dialog_type (не type);
6. валидация горячих клавиш (минимум 2 клавиши, запрет системных, дубликаты);
7. сохранение настроек и откат при ошибке;
8. ротация файла журнала.
"""

import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="raspoz_test_")

import config  # noqa: E402
import log     # noqa: E402

# изолируем настройки и журнал теста от боевых файлов
config.SETTINGS_FILE = os.path.join(TMP, "settings.json")
log._log_file = os.path.join(TMP, "app.log")
config.HISTORY_FILE = os.path.join(TMP, "history.json")       # v2.5: история
config.USER_MODELS_DIR = os.path.join(TMP, "models", "argos")  # v2.5: пакеты
config.ARGOS_INDEX_CACHE = os.path.join(TMP, "argos_index.json")

import api as api_mod  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name}  {extra}")


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def set(self):
        for h in list(self.handlers):
            h()


class FakeEvents:
    def __init__(self):
        self.loaded = FakeEvent()
        self.closing = FakeEvent()
        self.shown = FakeEvent()


class FakeWindow:
    """Мини-двойник webview.Window: записывает всё, что с ним делают."""

    def __init__(self):
        self.events = FakeEvents()
        self.js_calls = []
        self.ops = []
        self.dialog_kwargs = None
        self.dialog_result = None
        self.fail_evaluate_js = False

    def evaluate_js(self, code, *a, **kw):
        if self.fail_evaluate_js:
            raise RuntimeError("bridge down")
        self.js_calls.append(code)
        return None

    def minimize(self): self.ops.append("minimize")
    def maximize(self): self.ops.append("maximize")
    def restore(self): self.ops.append("restore")
    def hide(self): self.ops.append("hide")
    def show(self): self.ops.append("show")
    def destroy(self): self.ops.append("destroy")
    def resize(self, w, h): self.ops.append(("resize", w, h))

    def create_file_dialog(self, dialog_type=10, **kw):
        self.dialog_kwargs = dict(kw)
        self.dialog_kwargs["dialog_type"] = dialog_type
        return self.dialog_result


def log_text():
    try:
        with open(log._log_file, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def fresh_api(**attach_kw):
    w = FakeWindow()
    a = api_mod.Api()
    a.attach_window(w, **attach_kw)
    return a, w


# ---------------------------------------------------------------------------
print("\n--- 1. Буферизация журнала до загрузки (фикс «вечного запуска») ---")
a, w = fresh_api()
t0 = time.time()
for i in range(100):
    a.push_log_line(f"строка {i}")
elapsed = time.time() - t0
check("100 строк до loaded не вызывают evaluate_js", len(w.js_calls) == 0, str(len(w.js_calls)))
check("и не блокируют поток (elapsed < 0.5 c)", elapsed < 0.5, f"{elapsed:.2f} c")

w.events.loaded.set()          # событие загрузки страницы
time.sleep(1.2)                # ждём фоновый flush
bulk = [c for c in w.js_calls if "onLogBulk" in c]
check("после loaded буфер ушёл пакетом onLogBulk", len(bulk) == 1, str(len(bulk)))
# пейлоад сериализуется с ensure_ascii=True (защита от GTK-бага len(script)),
# поэтому проверяем содержимое после JSON-декодирования
import json as _json
_decoded = ""
if bulk:
    _payload = bulk[0][bulk[0].find("["):bulk[0].rfind("]") + 1]
    try:
        _decoded = "\n".join(_json.loads(_payload))
    except Exception:
        _decoded = _payload
check("в пакете все 100 строк", "строка 99" in _decoded and "строка 0" in _decoded, _decoded[:80])
check("пейлоад ASCII-безопасен (ensure_ascii)", bulk and all(ord(ch) < 128 for ch in bulk[0]))
a.push_log_line("живая строка")
check("после загрузки строки идут сразу (onLogEntry)",
      any("onLogEntry" in c for c in w.js_calls[-1:]))

print("\n--- 2. Сбой моста при доставке журнала не вызывает рекурсию ---")
w.fail_evaluate_js = True
for i in range(10):
    a.push_log_line(f"аварийная {i}")
check("исключений нет, рекурсии нет", "evaluate_js не прошёл" not in log_text())
w.fail_evaluate_js = False

print("\n--- 3. safe_call: исключение метода → запись в файл журнала ---")
class BrokenWindow(FakeWindow):
    def resize(self, w, h):
        raise RuntimeError("resize boom")
a2 = api_mod.Api()
bw = BrokenWindow()
a2.attach_window(bw)
bw.events.loaded.set()
time.sleep(0.8)
r = a2.win_resize(800, 600)
check("метод вернул ok=False с ошибкой", isinstance(r, dict) and r.get("ok") is False and "resize boom" in str(r.get("error")))
check("ошибка записана в файл журнала", "НЕПРЕДВИДЕННАЯ ОШИБКА в win_resize" in log_text())

print("\n--- 4. Диалоги файлов: правильные коды и имя параметра ---")
# фильтр файлов должен соответствовать валидатору pywebview (parse_file_type)
import re
import re as _re
_FILTER_RE = r"^([\w ]+)\((\*(?:\.(?:\w+|\*))*(?:;\*\.\w+)*)\)$"
_filters_ok = all(_re.search(_FILTER_RE, f) for f in api_mod.IMAGE_FILTER)
check("IMAGE_FILTER проходит валидацию pywebview (отдельные строки)", _filters_ok,
      str(api_mod.IMAGE_FILTER))
a3, w3 = fresh_api()
w3.events.loaded.set(); time.sleep(0.8)
img = os.path.join(TMP, "pic.png")
with open(img, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
w3.dialog_result = (img,)
r = a3.pick_image()
check("pick_image отработал без TypeError", isinstance(r, dict) and r.get("ok") is True, str(r)[:120])
check("OPEN_DIALOG=10 передан как dialog_type", w3.dialog_kwargs.get("dialog_type") == 10, str(w3.dialog_kwargs))
check("имя параметра 'type' больше не используется", "type" not in w3.dialog_kwargs)

# save-диалог через capture_screenshot(ask_folder=True)
shot_src = os.path.join(TMP, "shot_src.png")
with open(shot_src, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
a3._do_capture = lambda: {"path": shot_src, "base64": "", "width": 10, "height": 10}
dst = os.path.join(TMP, "save_target")
w3.dialog_result = (dst,)
r = a3.capture_screenshot(True)
check("capture_screenshot(ask_folder) вернул ok", isinstance(r, dict) and r.get("ok") is True, str(r)[:150])
check("SAVE_DIALOG=30", w3.dialog_kwargs.get("dialog_type") == 30, str(w3.dialog_kwargs.get("dialog_type")))
check("save_filename предложен", bool(w3.dialog_kwargs.get("save_filename", "")))
check("файл сохранён с добавлением .png", os.path.isfile(dst + ".png"))

print("\n--- 5. Валидация горячих клавиш ---")
cases = [
    ("ctrl+f9", "ctrl+f9", None),
    ("Ctrl + Shift + S", "ctrl+shift+s", None),
    ("alt+q", "alt+q", None),
    ("F5", None, "err"),                       # одна клавиша
    ("ctrl+alt+del", None, "err"),             # системная
    ("alt+f4", None, "err"),                   # системная
    ("windows+l", None, "err"),                # системная
    ("ctrl+shift+ы", None, "err"),             # неизвестная клавиша
    ("ctrl+ctrl+s", None, "err"),              # дубль модификатора
    ("ctrl+shift", None, "err"),               # нет основной клавиши
    ("ctrl+page up", "ctrl+page up", None),
    ("shift+f13", "shift+f13", None),
    ("ctrl+num 5", "ctrl+num 5", None),
    ("", "", None),                            # пусто — допустимо
]
ok_all = True
for src, want, want_err in cases:
    got, err = api_mod.normalize_hotkey(src)
    if want_err:
        good = got is None and err
    else:
        good = got == want and err is None
    if not good:
        ok_all = False
        print(f"   провал: {src!r} -> ({got!r}, {err!r}), ожидалось ({want!r}, {want_err!r})")
check(f"normalize_hotkey: {len(cases)} кейсов", ok_all)

print("\n--- 6. save_settings: отклонение/принятие/дубликаты ---")
base = dict(api_mod.DEFAULT_SETTINGS)
r = a3.save_settings({**base, "hotkey_recognize": "f5"})
check("«f5» отклонена (ok=False + error)", r.get("ok") is False and bool(r.get("error")), str(r)[:100])
check("при отклонении возвращены текущие settings", isinstance(r.get("settings"), dict))
r = a3.save_settings({**base, "hotkey_recognize": "alt+f4"})
check("«alt+f4» отклонена", r.get("ok") is False)
r = a3.save_settings({**base, "hotkey_recognize": "ctrl+f9", "hotkey_screenshot": "ctrl+f9"})
check("одинаковые комбинации отклонены", r.get("ok") is False)
r = a3.save_settings({**base, "hotkey_recognize": "ctrl+f9"})
check("«ctrl+f9» принята (ok=True)", r.get("ok") is True, str(r)[:120])
import json
with open(config.SETTINGS_FILE, encoding="utf-8") as f:
    saved = json.load(f)
check("значение сохранено в settings.json", saved.get("hotkey_recognize") == "ctrl+f9")
check("в журнал попала запись о сохранении", "Настройки сохранены" in log_text())

a4 = api_mod.Api()   # новый экземпляр — должен прочитать файл
check("настройки переживают перезапуск", a4._settings.get("hotkey_recognize") == "ctrl+f9")
r = a4.save_settings({**a4._settings, "auto_copy": False})
check("булевы флаги сохраняются", r.get("ok") is True and r.get("auto_copy") is False)

print("\n--- 7. Видимость окна (захват экрана в режиме трея) ---")
a5, w5 = fresh_api(started_hidden=True)
check("started_hidden → окно считается невидимым", a5._visible is False)
a5.win_show()
check("win_show → видимо", a5._visible is True)
a5.win_hide_to_tray()
check("win_hide_to_tray → невидимо", a5._visible is False)
w5.ops.clear()   # сбрасываем историю: дальше проверяем, что захват её НЕ пополняет
a5._do_capture = lambda: None
r = a5.capture_smart()
check("захват при невидимом окне не дёргает hide/show",
      not w5.ops and r.get("cancelled") is True, str(w5.ops))

print("\n--- 8. Ротация журнала ---")
with open(log._log_file, "w", encoding="utf-8") as f:
    f.write("x" * (5_000_001))
log.info("триггер ротации")
check("app.log.old создан", os.path.isfile(log._log_file + ".old"))
check("новый app.log маленький", os.path.getsize(log._log_file) < 10_000)

print("\n--- 9. v2.3: выбор языков OCR (смешанный текст) ---")
import ocr  # noqa: E402
avail = ["rus", "eng", "chi_sim", "ukr", "deu", "fra"]
cases_langs = [
    # (пробы {lang:(text,conf)}, ожидаемый набор)
    ({"rus": ("Привет мир текст", 92.0), "eng": ("", -1.0), "chi_sim": ("", -1.0)}, ["rus"]),
    ({"rus": ("Hello Привет", 80.0), "eng": ("Hello world text", 88.0), "chi_sim": ("", -1.0)}, None),  # rus+eng (порядок по уверенности)
    ({"rus": ("", -1.0), "eng": ("Hello world", 90.0), "chi_sim": ("你好世界测试", 85.0)}, None),        # eng+chi_sim
    ({"rus": ("Привет", 60.0), "eng": ("Strasse Grüße", 70.0), "chi_sim": ("", -1.0)}, None),          # eng+deu(+rus?)
]
r1 = ocr.choose_langs(cases_langs[0][0], avail)
check("чистая кириллица → ['rus']", r1 == ["rus"], str(r1))
r2 = ocr.choose_langs(cases_langs[1][0], avail)
check("смесь рус+англ → оба языка в наборе", set(r2) >= {"rus", "eng"}, str(r2))
r3 = ocr.choose_langs(cases_langs[2][0], avail)
check("англ+китайский → оба в наборе", set(r3) >= {"eng", "chi_sim"}, str(r3))
r4 = ocr.choose_langs(cases_langs[3][0], avail)
check("немецкая диакритика → deu в наборе", "deu" in r4, str(r4))
r5 = ocr.choose_langs({"rus": ("", -1), "eng": ("", -1), "chi_sim": ("", -1)}, avail)
check("пустые пробы → не пустой набор (fallback)", isinstance(r5, list) and len(r5) >= 1, str(r5))
r6 = ocr.choose_langs({"rus": ("Привет мир текст", 92.0), "eng": ("Hello", 40.0),
                       "chi_sim": ("你好世界测试", 62.0)}, avail)
check("галлюцинация chi-пробы (62%) НЕ добавляет китайский", "chi_sim" not in r6, str(r6))
r7 = ocr.choose_langs({"rus": ("", -1.0), "eng": ("", -1.0),
                       "chi_sim": ("你好世界测试内容", 88.0)}, avail)
check("настоящий китайский (88%) → chi_sim выбран", "chi_sim" in r7, str(r7))
r8 = ocr.choose_langs({"rus": ("Привет", 85.0), "eng": ("Hello", 85.0),
                       "chi_sim": ("你好世界", 80.0)}, avail)
check("три письменности сразу → все три языка", set(r8) >= {"rus", "eng", "chi_sim"}, str(r8))
check("набор не длиннее 3 языков", all(len(ocr.choose_langs(c[0], avail)) <= 3 for c in cases_langs))
sc = ocr._script_counts("Привет Hello 你好")
check("_script_counts: кириллица/латиница/CJK", sc == {"cyr": 6, "lat": 5, "cjk": 2}, str(sc))

print("\n--- 10. v2.3: пары перевода (resources/argos) ---")
import translator  # noqa: E402
_fake_argos = os.path.join(TMP, "argos_fake")
os.makedirs(_fake_argos, exist_ok=True)
for nm in ("translate-en_ru-1_9.argosmodel", "translate-ru_en-1_9.argosmodel", "мусор.txt"):
    open(os.path.join(_fake_argos, nm), "wb").write(b"x")
_old_argos = config.ARGOS_DIR
config.ARGOS_DIR = _fake_argos
try:
    pairs = translator.available_pairs()
    check("available_pairs находит en→ru и ru→en",
          ("en", "ru") in pairs and ("ru", "en") in pairs, str(pairs))
    check("мусорные файлы игнорируются", len(pairs) == 2, str(pairs))
finally:
    config.ARGOS_DIR = _old_argos

a6, w6 = fresh_api()
w6.events.loaded.set(); time.sleep(0.6)
r = a6.get_translation_pairs()
check("get_translation_pairs вернул структуру", r.get("ok") is True and isinstance(r.get("pairs"), list))

print("\n--- 11. v2.3: recognize_file (QR → текст) ---")
import qr_reader  # noqa: E402
_img = os.path.join(TMP, "pic.png")
with open(_img, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
_orig_decode = qr_reader.decode_image
_orig_ocr = api_mod.ocr.ocr_image
a7, w7 = fresh_api()
w7.events.loaded.set(); time.sleep(0.6)
# сценарий 1: найден QR
qr_reader.decode_image = lambda p: {"count": 1, "results": [{"type": "QR-CODE", "text": "https://x.y", "engine": "t"}]}
r = a7.recognize_file(_img)
check("QR найден → mode=qr, без OCR", r.get("ok") is True and r.get("mode") == "qr" and r.get("count") == 1)
# сценарий 2: QR нет → OCR
qr_reader.decode_image = lambda p: {"count": 0, "results": []}
api_mod.ocr.ocr_image = lambda p, langs=None, progress_cb=None: {"text": "Привет", "lang": "rus", "confidence": 90, "words": 1, "detected": "rus"}
r = a7.recognize_file(_img)
check("QR нет → mode=ocr с текстом", r.get("ok") is True and r.get("mode") == "ocr" and r.get("text") == "Привет")
prog = [c for c in w7.js_calls if "onProgress" in c]
check("прогресс отправлялся в интерфейс (onProgress)", len(prog) >= 2, str(len(prog)))
# сценарий 3: файла нет
r = a7.recognize_file(os.path.join(TMP, "no-such-file.png"))
check("нет файла → ok=False с ошибкой", r.get("ok") is False and bool(r.get("error")))
qr_reader.decode_image = _orig_decode
api_mod.ocr.ocr_image = _orig_ocr

print("\n--- 12. v2.3: направление перевода + звук в настройках ---")
_captured = {}
def _fake_translate(text, src="en", tgt="ru", progress_cb=None):
    _captured.update({"text": text, "src": src, "tgt": tgt})
    return {"translated": "МОК", "ok": True, "error": None, "src": src, "tgt": tgt}
_orig_tr = api_mod.translator.translate
api_mod.translator.translate = _fake_translate
a8, w8 = fresh_api()
w8.events.loaded.set(); time.sleep(0.6)
r = a8.translate_text("Hello", "ru", "en")
check("translate_text передал пару ru→en", _captured.get("src") == "ru" and _captured.get("tgt") == "en", str(_captured))
check("результат перевода возвращён", r.get("ok") is True and r.get("translated") == "МОК")
api_mod.translator.translate = _orig_tr
r = a8.save_settings({**a8._settings, "sound": False})
check("настройка звука сохранена (sound=False)", r.get("ok") is True and r.get("sound") is False)
r = a8.save_settings({**a8._settings, "sound": True})
check("звук снова включён", r.get("ok") is True and r.get("sound") is True)
r = a8.open_image_zoom(os.path.join(TMP, "no-such.png"))
check("open_image_zoom: нет файла → ok=False", r.get("ok") is False)

print("\n--- 13. v2.3.1: папка скриншотов (настройка/выбор/сброс) ---")
a9, w9 = fresh_api()
w9.events.loaded.set(); time.sleep(0.6)
r = a9.save_settings({**a9._settings, "screenshots_dir": os.path.join(TMP, "нет-такой-папки")})
check("несуществующая папка отклонена", r.get("ok") is False and "не существует" in str(r.get("error")))
custom_dir = os.path.join(TMP, "my_shots")
os.makedirs(custom_dir, exist_ok=True)
r = a9.save_settings({**a9._settings, "screenshots_dir": custom_dir})
check("существующая папка принята", r.get("ok") is True and r.get("screenshots_dir") == custom_dir)
check("_effective_screenshots_dir = своя папка", a9._effective_screenshots_dir() == custom_dir)
w9.dialog_result = (custom_dir,)
r = a9.choose_screenshots_dir()
check("choose_screenshots_dir вернул ok+path", r.get("ok") is True and r.get("path") == custom_dir)
check("диалог выбора ПАПКИ (dialog_type=20)", w9.dialog_kwargs.get("dialog_type") == 20,
      str(w9.dialog_kwargs.get("dialog_type")))
r = a9.reset_screenshots_dir()
check("reset_screenshots_dir → по умолчанию",
      r.get("ok") is True and a9._settings.get("screenshots_dir") == "")
check("после сброса effective = Screenshots рядом с программой",
      a9._effective_screenshots_dir() == config.get_screenshots_dir())

print("\n--- 14. v2.4: действие крестика (close_action) ---")
a10, w10 = fresh_api()
w10.events.loaded.set(); time.sleep(0.6)
r = a10.save_settings({**a10._settings, "close_action": "минимизировать"})
check("недопустимое значение close_action отклонено", r.get("ok") is False and bool(r.get("error")))
r = a10.save_settings({**a10._settings, "close_action": "tray"})
check("close_action=tray сохранён", r.get("ok") is True and r.get("close_action") == "tray")
class _FakeTray:
    stopped = False
    icon_shown = True
    def stop(self): self.stopped = True
    def ensure_icon(self, timeout=0): return self.icon_shown
    def notify(self, title, text): return True
a10._tray = _FakeTray()
r = a10.win_close()
check("✕ при tray: окно спрятано, НЕ уничтожено",
      r.get("minimized_to_tray") is True and "hide" in w10.ops and "destroy" not in w10.ops, str(w10.ops))
class _DeadTray(_FakeTray):
    icon_shown = False
    def ensure_icon(self, timeout=0): return False
a10._tray = _DeadTray()
a10.save_settings({**a10._settings, "close_action": "tray"})
w10.ops.clear()
r = a10.win_close()
time.sleep(0.8)
check("v2.6: трей НЕздоров → ✕ закрывает полностью (нет процесса без окна)",
      r.get("minimized_to_tray") is not True and "destroy" in w10.ops, str(w10.ops))
a10._tray = _FakeTray()
r = a10.save_settings({**a10._settings, "close_action": "exit"})
check("close_action=exit сохранён", r.get("ok") is True)
w10.ops.clear()
r = a10.win_close()
time.sleep(0.8)   # destroy вызывается с задержкой 0.5 с (мост успевает ответить)
check("✕ при exit: окно уничтожено (программа закрыта)", "destroy" in w10.ops, str(w10.ops))
check("трей остановлен при выходе", a10._tray.stopped is True)


print("\n--- 15. v2.4.0: автозапуск РЕАЛЬНО применяется при сохранении ---")
a11, w11 = fresh_api()
w11.events.loaded.set(); time.sleep(0.6)
applied = []
a11._apply_autostart = lambda enabled: applied.append(enabled)   # без PowerShell в тесте
r = a11.save_settings({**a11._settings, "autostart": True})
check("autostart=True принят (ok=True)", r.get("ok") is True and r.get("autostart") is True, str(r)[:120])
check("_apply_autostart(True) вызван — ярлык создаётся", applied == [True], str(applied))
r = a11.save_settings({**a11._settings, "autostart": True})
check("повторное True не вызывает лишнего применения", applied == [True], str(applied))
r = a11.save_settings({**a11._settings, "autostart": False})
check("autostart=False → _apply_autostart(False)", applied == [True, False], str(applied))
applied.clear()
r = a11.save_settings({**a11._settings, "close_action": "tray"})
check("смена close_action НЕ трогает автозапуск", applied == [] and r.get("ok") is True, str(applied))

print("\n--- 16. v2.5: история распознаваний ---")
a16, w16 = fresh_api()
a16.history_clear()   # предыдущие секции тоже пишут в историю (recognize_file)
a16._history_add("ocr", "Текст один", {"source": "screen", "langs": "rus", "confidence": 90})
a16._history_add("qr", "https://example.com/qr", {"source": "file", "count": 1})
a16._history_add("ocr", "   ")   # пустой текст НЕ пишется
h = a16.get_history()
check("две записи, новейшие сверху", len(h["items"]) == 2 and h["items"][0]["kind"] == "qr",
      str([i["kind"] for i in h["items"]]))
check("мета-поля сохранены (source/langs/confidence)",
      h["items"][1].get("source") == "screen" and h["items"][1].get("langs") == "rus"
      and h["items"][1].get("confidence") == 90)
h2 = a16.get_history(100, "один")
check("поиск по тексту истории", len(h2["items"]) == 1 and h2["items"][0]["kind"] == "ocr")
rid = h["items"][0]["id"]
r = a16.history_delete(rid)
check("удаление записи по id", r.get("ok") is True and len(a16.get_history()["items"]) == 1)
r = a16.history_delete("нет-такого")
check("удаление несуществующей записи — ok=False", r.get("ok") is False)
a16.history_clear()
check("очистка истории", len(a16.get_history()["items"]) == 0)
import json as _json
for i in range(7):
    a16._history_add("ocr", f"запись {i}")
check("история переживает перезапись файла (7 записей)",
      len(a16.get_history()["items"]) == 7)

print("\n--- 17. v2.5: турнир вариантов OCR (чистые функции) ---")
import ocr as ocr_mod  # noqa: E402
check("пустой кандидат отклоняется", ocr_mod.score_candidate("", -1.0, 0) == -1.0)
check("однобуквенная галлюцинация отклоняется", ocr_mod.score_candidate("x", 95.0, 1) == -1.0)
check("оценка растёт с уверенностью и объёмом",
      ocr_mod.score_candidate("Привет мир", 80.0, 2) < ocr_mod.score_candidate("Привет мир друг", 90.0, 3))
best = ocr_mod.pick_best([
    ("otsu", 3, "", -1.0, 0),
    ("plain", 6, "Договор аренды номер 45", 91.0, 4),
    ("otsu_inv", 6, "ш у м", 30.0, 3),
])
check("турнир выбирает уверенный вариант plain/psm6", best is not None and best[0] == "plain" and best[1] == 6,
      str(best))
check("турнир из одних пустышек → None",
      ocr_mod.pick_best([("otsu", 3, "", -1.0, 0), ("plain", 6, "", -1.0, 0)]) is None)
check("порядок вариантов фиксирован (приоритет plain)",
      ocr_mod.variant_names() == ["plain", "otsu", "otsu_inv", "adaptive"])
check("при равенстве очков побеждает приоритетный вариант",
      ocr_mod.pick_best([("otsu", 6, "Один и тот же текст", 90.0, 5),
                         ("plain", 6, "Один и тот же текст", 90.0, 5)])[0] == "plain")

print("\n--- 18. v2.5: перевод — автоопределение, транзит, пакеты ---")
import translator as tr_mod  # noqa: E402
check("автоопределение: русский", tr_mod.detect_lang("Привет, как дела?") == "ru")
check("автоопределение: английский", tr_mod.detect_lang("Hello, how are you?") == "en")
check("автоопределение: украинский", tr_mod.detect_lang("Привіт, як у тебе справи?") == "uk")
check("автоопределение: немецкий", tr_mod.detect_lang("Hallo, wie geht es dir heute?") == "de")
check("автоопределение: пустой текст → None", tr_mod.detect_lang("12345") is None)
_fake_models = [
    os.path.join(TMP, "translate-en_ru-1_9.argosmodel"),
    os.path.join(TMP, "translate-ru_en-1_9.argosmodel"),
    os.path.join(TMP, "translate-en_de-1_9.argosmodel"),
]
_orig_find = config.find_argos_models
config.find_argos_models = lambda: list(_fake_models)
try:
    check("пары читаются по именам файлов",
          tr_mod.available_pairs() == [("en", "ru"), ("ru", "en"), ("en", "de")],
          str(tr_mod.available_pairs()))
    check("прямая пара есть → direct", tr_mod.translation_path("en", "ru") == ("direct",))
    check("нет прямой, но есть цепочка через en → via_en",
          tr_mod.translation_path("ru", "de") == ("via_en",))
    check("нет ни прямой, ни цепочки → missing со списком пакетов",
          tr_mod.translation_path("fr", "ru")[0] == "missing")
    packs = tr_mod.installed_packs()
    check("installed_packs: размеры и признак bundled",
          len(packs) == 3 and all("bundled" in p and "size" in p for p in packs))
    r = tr_mod.remove_pair("en", "ru")
    check("встроенный пакет удалить нельзя", r.get("ok") is False)
    idx = [
        {"from_code": "en", "to_code": "fr", "links": ["https://x/translate-en_fr-1_0.argosmodel"]},
        {"from_code": "en", "to_code": "fr", "links": ["https://x/dup.argosmodel"]},
        {"from_code": "xx", "to_code": "yy", "links": ["https://x/translate-xx_yy-1_0.argosmodel"]},
        {"from_code": "en", "to_code": "es"},
    ]
    rem = tr_mod.remote_packages(index=idx)
    check("remote_packages: дубли и пары вне каталога отброшены",
          [ (p["src"], p["tgt"]) for p in rem ] == [("en", "fr")], str(rem))
finally:
    config.find_argos_models = _orig_find

print("\n--- 19. v2.5: меню трея (состав пунктов) ---")
import tray as tray_mod  # noqa: E402
m_hidden = tray_mod.menu_items(False)
m_shown = tray_mod.menu_items(True)
check("меню при спрятанном окне: «Показать окно»", m_hidden[0] == (tray_mod.IDM_TOGGLE, "Показать окно"))
check("меню при открытом окне: «Скрыть окно»", m_shown[0] == (tray_mod.IDM_TOGGLE, "Скрыть окно"))
check("в меню есть разделитель и «Выход»",
      m_hidden[1] == ("sep", None) and m_hidden[2] == (tray_mod.IDM_EXIT, "Выход"))
check("цвета меню заданы явно (тёмный фон, светлый текст)",
      tray_mod.MENU_BG != tray_mod.MENU_TEXT and tray_mod.MENU_BG[0] < 128 < tray_mod.MENU_TEXT[0])

print("\n--- 20. v2.6: трей — 64-битные типы сообщений и фолбэк меню ---")
import ctypes as _ct  # noqa: E402
import inspect as _inspect  # noqa: E402
check("WPARAM/LPARAM/LRESULT 64-битные на x64 (причина «белого меню» v2.5)",
      _ct.sizeof(tray_mod.WPARAM_T) == _ct.sizeof(_ct.c_void_p)
      and _ct.sizeof(tray_mod.LPARAM_T) == _ct.sizeof(_ct.c_void_p)
      and _ct.sizeof(tray_mod.LRESULT_T) == _ct.sizeof(_ct.c_void_p))
_src = _inspect.getsource(tray_mod)
check("есть фолбэк на системное меню (MF_STRING), если самоотрисовка сбоит",
      "MF_STRING" in _src and "_ownerdraw_failed" in _src)
check("отрисовка пункта защищена try/except (сбой не роняет цикл сообщений)",
      "except Exception" in _inspect.getsource(tray_mod.TrayIcon._wnd_proc))
check("v2.6: трей умеет проверять здоровье и пересоздавать значок",
      hasattr(tray_mod.TrayIcon, "ensure_icon") and hasattr(tray_mod.TrayIcon, "_add_icon"))
check("v2.6: всплывающая подсказка у значка (balloon) для стрелки ^",
      hasattr(tray_mod.TrayIcon, "notify")
      and tray_mod.NIF_INFO == 0x10 and tray_mod.NIM_MODIFY == 1)
check("v2.6: single-instance — второй запуск открывает окно первого",
      tray_mod.WM_RASPOZ_SHOW == tray_mod.WM_APP + 5
      and hasattr(tray_mod, "claim_single_instance")
      and "WM_RASPOZ_SHOW" in _inspect.getsource(tray_mod.TrayIcon._wnd_proc))
check("v2.6: watchdog возвращает пропавший значок (≤5 с)",
      hasattr(tray_mod.TrayIcon, "_watchdog")
      and hasattr(tray_mod.TrayIcon, "_message_loop"))

print("\n--- 21. v2.6: скрытие консольных окон (proc) ---")
import proc as proc_mod  # noqa: E402
kw = proc_mod.inject({}, force=True)
check("inject добавляет CREATE_NO_WINDOW",
      kw.get("creationflags") == proc_mod.CREATE_NO_WINDOW)
kw2 = proc_mod.inject({"creationflags": 0x00000008}, force=True)  # DETACHED
check("inject сохраняет чужие флаги (OR, а не замена)",
      kw2["creationflags"] == (0x00000008 | proc_mod.CREATE_NO_WINDOW))


class _FakeSP:
    def __init__(self):
        self.calls = []

    def run(self, *a, **k):
        self.calls.append(("run", a, k))
        return "ok"

    def Popen(self, *a, **k):  # noqa: N802
        self.calls.append(("Popen", a, k))
        return "popen"

    PIPE = -1


fake = _FakeSP()
wrapped = proc_mod._SilentSubprocess(fake)
# эмулируем Windows: на Linux флаги честно НЕ добавляются
import types as _types  # noqa: E402
_real_os, _real_sp = proc_mod.os, proc_mod.subprocess
proc_mod.os = _types.SimpleNamespace(name="nt")
proc_mod.subprocess = _types.SimpleNamespace(
    STARTUPINFO=lambda: _types.SimpleNamespace(dwFlags=0))
try:
    wrapped.run(["x"], creationflags=0)
    wrapped.Popen(["y"])
finally:
    proc_mod.os, proc_mod.subprocess = _real_os, _real_sp
check("обёртка прокидывает флаги в чужой subprocess (run/Popen)",
      fake.calls[0][2].get("creationflags") == proc_mod.CREATE_NO_WINDOW
      and fake.calls[1][2].get("creationflags") == proc_mod.CREATE_NO_WINDOW
      and fake.calls[0][2].get("startupinfo") is not None)
check("обёртка отдаёт чужие атрибуты (PIPE) как есть", wrapped.PIPE == -1)


class _Lib:
    subprocess = _FakeSP()


proc_mod.patch_module(_Lib)
check("patch_module подменяет subprocess у библиотеки",
      isinstance(_Lib.subprocess, proc_mod._SilentSubprocess))
proc_mod.patch_module(_Lib)
check("повторный patch_module безопасен (не заворачивает дважды)",
      isinstance(_Lib.subprocess._real, _FakeSP))

print("\n--- 22. v2.6.1: иконка трея ОТВЕЧАЕТ, программа всегда запускается ---")
import threading as _th  # noqa: E402
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_main_src = io.open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read() \
    if hasattr(io, "open") else open(os.path.join(_ROOT, "main.py"), encoding="utf-8").read()

# 22.1 watchdog: следит за ЖИВЫМ насосом, а не только за значком
_now = time.time()
check("watchdog: здоровый трей → ok",
      tray_mod.watchdog_action(True, True, _now)[0] == "ok")
check("watchdog: насос молчит 30 с → restart (главная причина «мёртвой» иконки)",
      tray_mod.watchdog_action(True, True, _now - 30)[0] == "restart"
      and "насос" in tray_mod.watchdog_action(True, True, _now - 30)[1])
check("watchdog: поток трея погиб → restart",
      tray_mod.watchdog_action(True, False, _now)[0] == "restart")
check("watchdog: значок пропал → restart",
      tray_mod.watchdog_action(False, True, _now)[0] == "restart")
check("watchdog: открытое меню трея НЕ считается зависанием",
      tray_mod.watchdog_action(True, True, 0, in_menu=True)[0] == "ok")
check("watchdog: остановленный трей не перезапускается",
      tray_mod.watchdog_action(True, True, _now, running=False)[0] == "stop")
check("pump_is_stale: свежий пульс → жив", tray_mod.pump_is_stale(_now, now=_now) is False)
check("pump_is_stale: пульса никогда не было → мёртв", tray_mod.pump_is_stale(0, now=_now) is True)

# 22.2 повторный запуск программы: «не запускается» исключено
check("single-instance: мы первые → first",
      tray_mod.single_instance_decision(False, False, False, False) == "first")
check("single-instance: первый экземпляр ответил → opened",
      tray_mod.single_instance_decision(True, True, True, True) == "opened")
check("single-instance: первый НЕ ответил → takeover (запускаемся сами)",
      tray_mod.single_instance_decision(True, True, True, False) == "takeover")
check("single-instance: окна-владельца нет → takeover",
      tray_mod.single_instance_decision(True, False, False, False) == "takeover")
_src_claim = _inspect.getsource(tray_mod.claim_single_instance)
check("подтверждение первого экземпляра: событие + ожидание ответа",
      "SHOW_ACK_EVENT" in _src_claim and "WaitForSingleObject" in _src_claim
      and "ResetEvent" in _src_claim)

# 22.3 значок создаёт ТОЛЬКО поток с насосом (иначе «муляж»)
check("_add_icon отказывается работать вне потока насоса",
      "current_thread() is not self._thread" in _inspect.getsource(tray_mod.TrayIcon._add_icon))
_src_watch = _inspect.getsource(tray_mod.TrayIcon._watchdog)
check("watchdog НЕ создаёт окно/значок сам (только перезапуск потока)",
      "_add_icon" not in _src_watch and "_ensure_window" not in _src_watch
      and "_restart" in _src_watch and "watchdog_action" in _src_watch)
check("_restart создаёт НОВЫЙ поток трея (новое окно + новый значок)",
      "Thread(target=self._run" in _inspect.getsource(tray_mod.TrayIcon._restart))

# 22.4 пульс насоса и потерянный значок
_src_tray = _inspect.getsource(tray_mod)
check("пульс насоса: WM_TIMER раз в секунду",
      tray_mod.WM_TIMER == 0x0113 and "SetTimer" in _src_tray
      and tray_mod.HEARTBEAT_MS == 1000)
check("значок проверяется NIM_MODIFY (система теряет значки после перезапуска explorer)",
      "NIM_MODIFY" in _inspect.getsource(tray_mod.TrayIcon._probe_icon))
check("перезапуск explorer: обрабатываем TaskbarCreated",
      "TaskbarCreated" in _src_tray and "_taskbar_msg" in _src_tray)

# 22.5 клики не блокируют насос; левый клик всегда ПОКАЗЫВАЕТ окно
_src_proc = _inspect.getsource(tray_mod.TrayIcon._wnd_proc)
_branch = _src_proc.split("if msg == WM_TRAYICON:")[1].split("if msg == WM_RASPOZ_SHOW")[0]
check("левый/двойной клик → показ окна (спрятать окно кликом больше нельзя)",
      "WM_LBUTTONUP" in _branch and "WM_LBUTTONDBLCLK" in _branch
      and "_call_show" in _branch and "_call_toggle" not in _branch)
check("правый клик → меню (из потока насоса)", "_show_menu" in _branch)
check("колбэки кликов уходят в отдельный поток — насос не блокируется",
      "_spawn" in _branch and "threading.Thread" in _inspect.getsource(tray_mod.TrayIcon._spawn))
check("WM_RASPOZ_SHOW: сначала подтверждаем жизнь, потом показываем окно",
      "_signal_ack" in _src_proc and "_call_show" in _src_proc)
check("main: показ окна из трея через Win32 (api.win_force_show), без window.show()",
      "api.win_force_show()" in _main_src and "window.show()" not in _main_src)
check("меню трея помечается как модальное (watchdog не убьёт трей во время меню)",
      "_in_menu" in _inspect.getsource(tray_mod.TrayIcon._show_menu))

# 22.6 healthy() — условие, при котором вообще разрешено прятать окно
t22 = tray_mod.TrayIcon(on_show=lambda: None, on_exit=lambda: None)
t22._running = True
t22.icon_shown = True
t22._thread = _th.current_thread()      # «живой» поток
t22._last_beat = time.time()
check("healthy(): значок + живой поток + свежий пульс → True", t22.healthy() is True)
t22._last_beat = time.time() - 30
check("healthy(): насос молчит → False (трей будет перезапущен)", t22.healthy() is False)
t22._in_menu = True
check("healthy(): открытое меню не считается зависанием", t22.healthy() is True)
t22._in_menu = False
t22._last_beat = time.time()
t22.icon_shown = False
check("healthy(): нет значка → False", t22.healthy() is False)

# 22.7 api: показ/скрытие окна без блокирующего Invoke
a22, w22 = fresh_api()
a22._win32_show_hide = lambda show: True
a22._visible = False
r = a22.win_force_show()
check("win_force_show: Win32-путь НЕ трогает pywebview (нет блокировки GUI-потока)",
      r.get("via") == "win32" and "show" not in w22.ops and a22._visible is True,
      str(w22.ops))
a22b, w22b = fresh_api()
a22b._win32_show_hide = lambda show: False
r = a22b.win_force_show()
check("win_force_show: запасной путь pywebview, окно видно",
      r.get("via") == "pywebview" and "show" in w22b.ops and a22b._visible is True)
a22c, w22c = fresh_api()
a22c._win32_show_hide = lambda show: True
r = a22c.win_force_hide()
check("win_force_hide: Win32-скрытие без pywebview",
      r.get("via") == "win32" and a22c._visible is False and "hide" not in w22c.ops)

# 22.8 страховка: пользователь не остаётся без окна
check("страховка: окно видно → ничего не делаем",
      api_mod.safety_net_action(True, False, 5) == ("idle", 0))
check("страховка: трей здоров → ничего не делаем",
      api_mod.safety_net_action(False, True, 5) == ("idle", 0))
check("страховка: первый сбой трея → ждём самолечения",
      api_mod.safety_net_action(False, False, 0) == ("wait", 1))
check("страховка: трей не починился → возвращаем окно на экран",
      api_mod.safety_net_action(False, False, 2) == ("show", 0))

# 22.9 ✕ при close_action=tray: прячем окно И включаем страховку
a23, w23 = fresh_api()
w23.events.loaded.set(); time.sleep(0.6)
class _HealthyTray:
    icon_shown = True
    stopped = False
    def ensure_icon(self, timeout=0): return True
    def healthy(self): return True
    def stop(self): self.stopped = True
    def notify(self, title, text, timeout_ms=10000): return True
a23._tray = _HealthyTray()
a23.save_settings({**a23._settings, "close_action": "tray"})
r = a23.win_close()
check("✕ при tray: окно спрятано, программа продолжает работать",
      r.get("minimized_to_tray") is True and "destroy" not in w23.ops, str(w23.ops))
check("✕ при tray: страховка включена (окно вернётся, если иконка умрёт)",
      a23._net_started is True and a23._visible is False)
a23._closing = True

print("\n--- 23. версия: одно место правды, нигде не «зашита» ---")
_cfg = io.open(os.path.join(_ROOT, "config.py"), encoding="utf-8").read()
_nsi = io.open(os.path.join(_ROOT, "installer.nsi"), encoding="utf-8").read()
_rdm = io.open(os.path.join(_ROOT, "README.md"), encoding="utf-8").read()
_appjs = io.open(os.path.join(_ROOT, "ui", "app.js"), encoding="utf-8").read()
_ver = config.APP_VERSION
check("config.py: APP_VERSION = " + _ver, f'APP_VERSION = "{_ver}"' in _cfg)
check("installer.nsi: OutFile и все поля версии = " + _ver,
      f"Raspoznavatel-{_ver}-Setup.exe" in _nsi
      and f'VIProductVersion "{_ver}.0"' in _nsi
      and f'"FileVersion"     "{_ver}"' in _nsi
      and f'"ProductVersion"  "{_ver}"' in _nsi
      and f"Распознаватель {_ver}" in _nsi, _nsi[:80])
check("README: номер версии НЕ зашит (иначе на сайте видна старая версия)",
      not re.search(r"\b2\.\d+\.\d+\b", _rdm),
      str(re.findall(r"\b2\.\d+\.\d+\b", _rdm)[:3]))
check("интерфейс: в журнале нет «Интерфейс vX.Y загружен» (версия приходит из config)",
      "Интерфейс v" not in _appjs and "Интерфейс загружен" in _appjs)
check("в коде не осталось меток старых версий (v2.x) в комментариях",
      not re.search(r"v2\.\d", _appjs) and not re.search(r"(?m)^\s*#\s*v2\.\d", _cfg))

print("\n--- 24. v2.6.2: перевод с языка, модели для которого нет ---")
import translator as tr_mod  # noqa: E402
_pairs = [("en", "ru"), ("ru", "en")]
check("fallback_language: «be» без модели → переводим как «ru»",
      tr_mod.fallback_language("be", "en", _pairs) == "ru")
check("fallback_language: «uk» → «ru»", tr_mod.fallback_language("uk", "en", _pairs) == "ru")
check("fallback_language: «de» без модели → «en» (та же латиница)",
      tr_mod.fallback_language("de", "ru", _pairs) == "en")
check("fallback_language: «ru» (модель есть) → замены нет",
      tr_mod.fallback_language("ru", "en", _pairs) is None)
check("fallback_language: кандидат совпал с языком перевода → замены нет",
      tr_mod.fallback_language("kk", "ru", _pairs) is None)
check("fallback_language: иная письменность (zh) → замены нет",
      tr_mod.fallback_language("zh", "en", _pairs) is None)
check("fallback_language: пустой список моделей → замены нет",
      tr_mod.fallback_language("be", "en", []) is None)

# сквозная проверка: translate(auto) берёт запасной язык и переводит
_orig = (tr_mod.detect_lang, tr_mod.available_pairs, tr_mod.init_model,
         tr_mod._translate_direct)
tr_mod.detect_lang = lambda t: "be"
tr_mod.available_pairs = lambda: list(_pairs)
tr_mod.init_model = lambda *a, **k: True
tr_mod._translate_direct = lambda text, s, t, cb=None: "mother"
try:
    r = tr_mod.translate("мама", "auto", "en")
    check("translate(auto): «be» без модели переведён как «ru» (без ошибки)",
          r.get("ok") is True and r.get("src") == "ru" and r.get("detected") == "be"
          and r.get("translated") == "mother", str(r)[:160])
    tr_mod.detect_lang = lambda t: "ru"
    r2 = tr_mod.translate("мама", "auto", "en")
    check("translate(auto): «ru» с моделью идёт напрямую",
          r2.get("ok") is True and r2.get("src") == "ru" and r2.get("detected") == "ru")
finally:
    (tr_mod.detect_lang, tr_mod.available_pairs, tr_mod.init_model,
     tr_mod._translate_direct) = _orig

print("\n--- 25. v2.6.2: неопределённых имён нет (Windows-ветки не падают у владельца) ---")
_files = ["api.py", "main.py", "tray.py", "translator.py", "config.py", "ocr.py",
          "log.py", "proc.py", "qr_reader.py", "screen_capture.py",
          "e2e_selfcheck.py", "tools/download_resources.py",
          "tests/test_logic.py", "tests/tray_win_smoke.py"]
try:
    import ast as _ast
    from pyflakes import checker as _pfc
    _bad = []
    for _f in _files:
        _p = os.path.join(_ROOT, _f)
        if not os.path.isfile(_p):
            continue
        _tree = _ast.parse(io.open(_p, encoding="utf-8").read())
        for _m in _pfc.Checker(_tree, _p).messages:
            if _m.__class__.__name__ == "UndefinedName":
                _bad.append("%s:%d %s" % (_f, _m.lineno, _m.message % _m.message_args))
    check("pyflakes: неопределённых имён нет во всех модулях (урок L48)",
          not _bad, "; ".join(_bad[:5]))
except ImportError:
    print("· pyflakes не установлен — проверка имён пропущена (есть в этапе doctor)")
    PASS += 0
_src_fix = _inspect.getsource(api_mod.Api._fix_window_styles)
check("_fix_window_styles: wintypes импортирован в самой функции (Windows-only ветка)",
      "from ctypes import wintypes" in _src_fix)

print("\n--- 26. v2.6.3: определение языка и перевод без «скачайте пакет» ---")
_ru_long = ("Вышла ЛУЧШАЯ нейронка для дипфейков — ByteDance выпустили новую модель. "
            "Ты смог бы это прочитать? Сыну нравится, и мы рады всем.")
check("русский текст с заглавной «Ы» определяется как ru (а не be)",
      tr_mod.detect_lang(_ru_long) == "ru", tr_mod.detect_lang(_ru_long))
check("короткий русский текст с «Ы» — тоже ru", tr_mod.detect_lang("ТЫ СЫН МОЙ?") == "ru")
check("настоящий украинский определяется как uk",
      tr_mod.detect_lang("привіт, як справи? це український текст, і він довгий") == "uk")
check("короткий украинский текст (одна отличительная буква) — uk",
      tr_mod.detect_lang("Їжак") == "uk")
check("английский определяется как en",
      tr_mod.detect_lang("Hello world, this is an English sentence") == "en")
check("в подсказках кириллицы нет русской буквы «Ы»",
      "Ы" not in dict(tr_mod._CYR_HINTS).get("be", set())
      and "ы" not in dict(tr_mod._CYR_HINTS).get("be", set()))

# перевод: язык-«сосед» без модели больше не требует скачать пакет
_orig2 = (tr_mod.detect_lang, tr_mod.available_pairs, tr_mod.init_model,
          tr_mod._translate_direct)
tr_mod.detect_lang = lambda t: "be"
tr_mod.available_pairs = lambda: [("en", "ru"), ("ru", "en")]
tr_mod.init_model = lambda *a, **k: True
tr_mod._translate_direct = lambda text, s_, t_, cb=None: "mother"
try:
    r = tr_mod.translate("мама", "auto", "ru")
    check("авто: «be» → цель «ru» — текст оставлен как есть, БЕЗ ошибки про пакет",
          r.get("ok") is True and not r.get("error") and "пакет" not in str(r.get("note", "")),
          str(r)[:160])
    check("в ответе пояснено, почему текст не менялся",
          "определён" in str(r.get("note", "")) or "совпадают" in str(r.get("note", "")),
          str(r.get("note")))
    r = tr_mod.translate("мама", "auto", "en")
    check("авто: «be» → цель «en» — переводим как «ru» (модель есть)",
          r.get("ok") is True and r.get("src") == "ru" and r.get("translated") == "mother",
          str(r)[:160])
finally:
    (tr_mod.detect_lang, tr_mod.available_pairs, tr_mod.init_model,
     tr_mod._translate_direct) = _orig2

print(f"\nИТОГ: {PASS} пройдено, {FAIL} провалено")
sys.exit(0 if FAIL == 0 else 1)

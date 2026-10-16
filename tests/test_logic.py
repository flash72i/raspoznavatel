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

print(f"\nИТОГ: {PASS} пройдено, {FAIL} провалено")
sys.exit(0 if FAIL == 0 else 1)

import re as _re
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

print(f"\nИТОГ: {PASS} пройдено, {FAIL} провалено")
sys.exit(0 if FAIL == 0 else 1)

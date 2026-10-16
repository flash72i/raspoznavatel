# -*- coding: utf-8 -*-
"""
api.py — Python-API приложения «Распознаватель» (v2.2), доступное из JavaScript.

Что изменилось в 2.2 (исправление «немых» кнопок и зависшего старта):
  • Api больше НЕ требует окно в конструкторе: main.py создаёт Api, передаёт
    его в webview.create_window(js_api=api) и затем вызывает attach_window().
    Без js_api мост window.pywebview.api в JavaScript был пуст — именно
    поэтому ВСЕ кнопки интерфейса «не реагировали», а ошибки не попадали в
    журнал (вызовы просто не доходили до Python).
  • Строки журнала ДО загрузки страницы буферизуются и отправляются пакетом
    после события loaded. Раньше evaluate_js() вызывался до webview.start(),
    блокировался на 20 с на каждой строке и уходил в рекурсию — окно не
    появлялось вовсе («бесконечный запуск»).
  • evaluate_js НИКОГДА не вызывается из обработчиков событий GUI-потока
    (в WebView2 это взаимная блокировка) — только из фоновых потоков.
  • Диалоги файлов используют правильные коды pywebview:
    OPEN_DIALOG = 10, SAVE_DIALOG = 30 (раньше «Сохранить как…» открывал
    диалог выбора ПАПКИ, а «Загрузить изображение» падал из-за неверного
    имени параметра type=… вместо dialog_type=…).
  • Поиск HWND главного окна проверяет PID процесса и имеет запасной путь
    через EnumWindows — кнопки свернуть/развернуть/закрыть работают надёжно.
  • Горячие клавиши: нормализация и валидация комбинаций (минимум 2 клавиши,
    одна основная + модификаторы), запрет системных комбинаций (Alt+F4 и т.п.),
    проверка совпадения двух комбинаций между собой.
  • Захват экрана прячет окно, только если оно было видимо (в режиме
    автозапуска «в трее» окно больше не выскакивает после горячей клавиши).

Как и раньше: КАЖДЫЙ вызов метода и КАЖДАЯ ошибка пишутся в журнал (log.py);
в интерфейс передаются только маленькие превью (JPEG ≤ 640 px); тяжёлые
библиотеки грузятся лениво.
"""

import functools
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
from collections import deque
from datetime import datetime

import config
import log
import ocr
import qr_reader
import screen_capture
import translator

# Фильтры диалога выбора файла — КАЖДЫЙ отдельной строкой формата
# 'Описание (*.ext;*.ext)' (pywebview валидирует их регуляркой и не принимает
# две группы, склеенные в одну строку через ';').
IMAGE_FILTER = (
    "Изображения (*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.tiff;*.tif;*.webp)",
    "Все файлы (*.*)",
)

# Коды диалогов pywebview (webview.OPEN_DIALOG / SAVE_DIALOG)
OPEN_DIALOG = 10
SAVE_DIALOG = 30

DEFAULT_SETTINGS = {
    "ocr_langs": "auto",                    # язык OCR определяется автоматически
    "auto_copy": True,                      # автокопирование результата
    "smart_hotkey": True,                   # с экрана: сначала QR, затем текст
    "hotkey_recognize": "ctrl+shift+s",     # комбинация распознавания области
    "hotkey_screenshot": "ctrl+shift+p",    # комбинация скриншота области
    "autostart": False,                     # запуск при включении ПК (в трей)
    "preload": True,                        # прогревать модель перевода в фоне
}

# ---------------------------------------------------------------------------
# Валидация горячих клавиш (формат библиотеки keyboard: "ctrl+shift+s")
# ---------------------------------------------------------------------------
HOTKEY_MODS = ("ctrl", "alt", "shift", "windows")
HOTKEY_SPECIAL = {
    "space", "tab", "enter", "esc", "escape", "delete", "insert",
    "home", "end", "page up", "page down", "up", "down", "left", "right",
    "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/", "`",
    "num 0", "num 1", "num 2", "num 3", "num 4",
    "num 5", "num 6", "num 7", "num 8", "num 9",
    "num +", "num -", "num *", "num /", "num .", "num enter",
}
# Комбинации, которые ломали бы работу системы/других программ — запрещены
HOTKEY_DENY = {
    "alt+f4", "alt+tab", "alt+escape", "ctrl+alt+delete", "ctrl+shift+esc",
    "windows+l", "windows+d", "windows+e", "windows+r", "windows+tab",
    "windows+x", "windows+s", "windows+a", "windows+g",
}


def normalize_hotkey(combo):
    """
    Приводит пользовательскую комбинацию к каноническому виду библиотеки
    keyboard: модификаторы в порядке ctrl, alt, shift, windows + одна основная
    клавиша. Принимаются комбинации от 2 клавиш (например ctrl+f9).

    :return: (нормализованная строка или None, текст ошибки или None)
             пустая строка — допустима (означает «не назначать»).
    """
    if combo is None:
        return "", None
    raw = str(combo).strip().lower()
    if not raw:
        return "", None
    parts = [p.strip() for p in raw.split("+") if p.strip()]
    if len(parts) < 2:
        return None, "нужно минимум 2 клавиши (например ctrl+f9 или alt+q)"
    mods = [p for p in parts if p in HOTKEY_MODS]
    keys = [p for p in parts if p not in HOTKEY_MODS]
    if len(set(mods)) != len(mods):
        return None, "модификаторы повторяются"
    if len(keys) != 1:
        return None, "нужна ровно одна основная клавиша (буква, цифра, F-клавиша…)"
    key = keys[0]
    if key == "escape":
        key = "esc"
    ok_key = (
        re.fullmatch(r"[a-z0-9]", key)
        or re.fullmatch(r"f\d{1,2}", key)
        or key in HOTKEY_SPECIAL
    )
    if not ok_key:
        return None, f"неизвестная клавиша «{key}»"
    ordered = [m for m in HOTKEY_MODS if m in mods]
    norm = "+".join(ordered + [key])
    if norm in HOTKEY_DENY:
        return None, "эта системная комбинация занята Windows — выберите другую"
    return norm, None


def safe_call(fn):
    """
    Декоратор: логирует вызов метода, его результат/исключение.
    Любая ошибка гарантированно попадает в журнал и возвращается в JS
    в виде {'ok': False, 'error': ...} — мост pywebview не рвётся.
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        name = fn.__name__
        try:
            short = ""
            if args:
                a0 = args[0]
                if isinstance(a0, str):
                    short = " " + (a0[:60] + ("…" if len(a0) > 60 else ""))
                elif isinstance(a0, bool):
                    short = f" {a0}"
            log.info(f"API → {name}(){short}")
            result = fn(self, *args, **kwargs)
            if isinstance(result, dict) and result.get("error"):
                log.error(f"API {name}: {result['error']}")
            return result
        except Exception as exc:
            tb = traceback.format_exc(limit=6)
            log.error(f"НЕПРЕДВИДЕННАЯ ОШИБКА в {name}: {exc}\n{tb}")
            return {"ok": False, "error": f"{name}: {exc}", "cancelled": False}
    return wrapper


class Api:
    """Мост между интерфейсом (JS) и логикой (Python)."""

    def __init__(self, window=None):
        self._window = None
        self._loaded = False                 # страница загружена — можно evaluate_js
        self._pending_lines = deque(maxlen=500)   # буфер строк журнала до загрузки
        self._visible = True                 # видно ли окно (для захвата экрана)
        self._hwnd = None                    # кэш HWND главного окна
        self._tray = None                    # иконка в трее (создаётся из main)
        self._busy = False
        self._preload_started = False   # прогрев перевода запускается один раз
        self._settings = self._load_settings()
        if window is not None:
            self.attach_window(window)

    # ------------------------------------------------------------------
    # Подключение окна и доставка журнала в интерфейс
    # ------------------------------------------------------------------

    def attach_window(self, window, started_hidden: bool = False):
        """
        Подключает окно pywebview. Вызывается из main.py СРАЗУ после
        create_window(js_api=api). Подписка на событие loaded — здесь же:
        до его срабатывания evaluate_js вызывать НЕЛЬЗЯ (блокируется на 20 с
        и в WebView2 приводит к взаимной блокировке GUI-потока).
        """
        self._window = window
        self._visible = not started_hidden
        try:
            window.events.loaded += self._on_window_loaded
        except Exception as exc:
            log.warn(f"Не удалось подписаться на событие loaded: {exc}")

    def _on_window_loaded(self):
        """Страница загружена. ВАЖНО: мы в GUI-потоке — evaluate_js только из фона."""
        self._loaded = True
        threading.Thread(target=self._flush_pending, daemon=True).start()

    def _flush_pending(self):
        """Отправляет накопленные до загрузки строки журнала одним пакетом."""
        if not self._pending_lines:
            return
        time.sleep(0.3)   # даём app.js инициализироваться
        lines = []
        while self._pending_lines:
            try:
                lines.append(self._pending_lines.popleft())
            except IndexError:
                break
        if not lines:
            return
        payload = json.dumps(lines)   # ensure_ascii=True: см. комментарий у _js_raw
        self._js_raw(f"window.onLogBulk && window.onLogBulk({payload})")

    def push_log_line(self, line: str):
        """Доставка одной строки журнала в интерфейс (безопасна до загрузки)."""
        if not self._loaded:
            self._pending_lines.append(line)
            return
        self._js_raw(
            f"window.onLogEntry && window.onLogEntry({json.dumps(line)})"
        )

    def _js_raw(self, code: str):
        """
        evaluate_js БЕЗ логирования сбоев. Используется для доставки журнала:
        log.warn отсюда вызвал бы бесконечную рекурсию (журнал -> ошибка ->
        журнал -> ...). Недоставленная строка журнала не критична — она уже
        записана в файл.

        ВАЖНО: все строковые полезные нагрузки для evaluate_js сериализуются
        json.dumps() с ensure_ascii=True (кириллица экранируется в ASCII). Причина:
        GTK-бэкенд pywebview передаёт движку len(script) в СИМВОЛАХ вместо
        байтов и обрезает скрипт посреди многобайтовой буквы (SyntaxError),
        а WebView2 в этом не виноват, но ASCII-пейлоад безопасен везде.
        """
        try:
            if self._window and self._loaded:
                self._window.evaluate_js(code)
        except Exception:
            pass

    def _js(self, code: str):
        """evaluate_js для статусов/ошибок (только после загрузки страницы)."""
        if not self._loaded:
            return
        try:
            if self._window:
                self._window.evaluate_js(code)
        except Exception as exc:
            log.warn(f"evaluate_js не прошёл: {exc}")

    def _status(self, text: str):
        self._js(f"window.onStatus && window.onStatus({json.dumps(text)})")

    def _error(self, text: str):
        log.error(text)
        self._js(f"window.onError && window.onError({json.dumps(str(text))})")
        self._status("Ошибка: " + str(text))

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------

    def _load_settings(self) -> dict:
        try:
            if os.path.isfile(config.SETTINGS_FILE):
                with open(config.SETTINGS_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                merged = dict(DEFAULT_SETTINGS)
                merged.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
                return merged
        except Exception as exc:
            log.warn(f"Не удалось прочитать настройки ({exc}) — беру значения по умолчанию")
        return dict(DEFAULT_SETTINGS)

    def _save_settings(self):
        try:
            with open(config.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            log.error(f"Не удалось сохранить настройки: {exc}")

    # ------------------------------------------------------------------
    # Win32-управление окном (основной путь — надёжнее pywebview)
    # ------------------------------------------------------------------

    def _get_hwnd(self):
        """
        Находит HWND главного окна (только нашего процесса!).
        1) точный поиск по заголовку FindWindowW;
        2) запасной путь: EnumWindows по префиксу «Распознаватель».
        Кэшируется только успешный результат.
        """
        if self._hwnd:
            return self._hwnd
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32

            def pid_of(hwnd):
                p = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                return p.value

            my_pid = os.getpid()
            hwnd = user32.FindWindowW(None, config.WINDOW_TITLE)
            if hwnd and pid_of(hwnd) == my_pid:
                self._hwnd = hwnd
                log.info(f"HWND окна найден: {hwnd}")
                return hwnd

            found = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def cb(h, _lp):
                try:
                    n = user32.GetWindowTextLengthW(h)
                    if n and pid_of(h) == my_pid:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        user32.GetWindowTextW(h, buf, n + 1)
                        if buf.value.startswith(config.APP_NAME) and buf.value != "RaspoznavatelTray":
                            found.append(h)
                            return False   # найдено — прекращаем перечисление
                except Exception:
                    pass
                return True

            user32.EnumWindows(WNDENUMPROC(cb), 0)
            if found:
                self._hwnd = found[0]
                log.info(f"HWND окна найден (перечисление): {self._hwnd}")
                return self._hwnd
            log.warn("HWND окна не найден (будет использован pywebview-путь)")
            return None
        except Exception as exc:
            log.error(f"Поиск HWND не удался: {exc}")
            return None

    @safe_call
    def win_minimize(self):
        """Свернуть окно (Win32 ShowWindow, запасной путь — pywebview)."""
        if os.name == "nt":
            try:
                import ctypes
                hwnd = self._get_hwnd()
                if hwnd and ctypes.windll.user32.ShowWindow(hwnd, 6):  # SW_MINIMIZE
                    return {"ok": True, "via": "win32"}
            except Exception as exc:
                log.warn(f"Win32-сворачивание не удалось ({exc}) — пробую pywebview")
        self._window.minimize()
        return {"ok": True, "via": "pywebview"}

    @safe_call
    def win_maximize_toggle(self):
        """Развернуть / восстановить окно."""
        if os.name == "nt":
            try:
                import ctypes
                user32 = ctypes.windll.user32
                hwnd = self._get_hwnd()
                if hwnd:
                    maximized = bool(user32.IsZoomed(hwnd))
                    user32.ShowWindow(hwnd, 9 if maximized else 3)  # SW_RESTORE / SW_MAXIMIZE
                    self._js(f"window.onMaximized && window.onMaximized({str(not maximized).lower()})")
                    return {"ok": True, "maximized": not maximized, "via": "win32"}
            except Exception as exc:
                log.warn(f"Win32-разворачивание не удалось ({exc}) — пробую pywebview")
        if getattr(self, "_maximized", False):
            self._window.restore()
            self._maximized = False
        else:
            self._window.maximize()
            self._maximized = True
        self._js(f"window.onMaximized && window.onMaximized({str(self._maximized).lower()})")
        return {"ok": True, "maximized": self._maximized, "via": "pywebview"}

    @safe_call
    def win_close(self):
        """
        Закрыть приложение (крестик на панели заголовка).

        destroy() вызывается с крошечной задержкой в отдельном потоке:
        мост pywebview должен успеть вернуть ответ в JS ДО разрушения окна,
        иначе его поток навсегда повиснет на семафоре evaluate_js и процесс
        не завершится (особенность GTK-бэкенда).
        """
        log.info("Закрытие приложения по кнопке ✕")
        try:
            if self._tray:
                self._tray.stop()
        except Exception:
            pass
        # Снимаем доставку журнала в UI: evaluate_js во время закрытия окна
        # конфликтует с внутренними структурами pywebview (в GTK это рвёт
        # close_window на середине и окно не закрывается).
        try:
            log.set_ui_callback(None)
        except Exception:
            pass

        def _destroy():
            time.sleep(0.5)   # мост успевает вернуть в JS ответ win_close
            try:
                self._window.destroy()
            except Exception as exc:
                log.error(f"destroy() не прошёл: {exc}")

        threading.Thread(target=_destroy, daemon=True).start()
        return {"ok": True}

    @safe_call
    def win_resize(self, width: int, height: int):
        """Изменить размер окна (зоны ресайза интерфейса)."""
        self._window.resize(int(width), int(height))
        return {"ok": True}

    @safe_call
    def win_restore_from_tray(self):
        """Показать и восстановить окно (клик по иконке трея)."""
        if os.name == "nt":
            try:
                import ctypes
                hwnd = self._get_hwnd()
                if hwnd:
                    user32 = ctypes.windll.user32
                    if user32.IsIconic(hwnd):
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    self._visible = True
                    return {"ok": True, "via": "win32"}
            except Exception as exc:
                log.warn(f"Win32-восстановление не удалось: {exc}")
        try:
            self._window.restore()
        except Exception:
            pass
        self._visible = True
        return {"ok": True, "via": "pywebview"}

    @safe_call
    def win_hide_to_tray(self):
        """Спрятать окно в трей (иконка остаётся в области уведомлений)."""
        try:
            self._window.hide()
            self._visible = False
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @safe_call
    def win_show(self):
        """Показать окно (из трея)."""
        try:
            self._window.show()
            self._visible = True
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Информация / настройки / языки
    # ------------------------------------------------------------------

    @safe_call
    def get_info(self) -> dict:
        return {
            "name": config.APP_NAME,
            "version": config.APP_VERSION,
            "frozen": config.is_frozen(),
            "data_dir": config.user_data_dir(),
            "screenshots_dir": config.get_screenshots_dir(),
        }

    @safe_call
    def get_settings(self) -> dict:
        # сверяем флаг автозапуска с реальным наличием ярлыка (Windows)
        if os.name == "nt":
            lnk = config.autostart_shortcut_path()
            actual = bool(lnk and os.path.isfile(lnk))
            if self._settings.get("autostart") != actual:
                self._settings["autostart"] = actual
                self._save_settings()
        return dict(self._settings)

    @safe_call
    def save_settings(self, new_settings) -> dict:
        """
        Сохраняет настройки. Возвращает {'ok': True, **настройки} либо
        {'ok': False, 'error': причина, 'settings': текущие} — интерфейс
        показывает ошибку и откатывает поле ввода.
        """
        if not isinstance(new_settings, dict):
            return {"ok": False, "error": "Некорректный формат настроек",
                    "settings": dict(self._settings)}
        pending = {}
        changed_hotkeys = False
        autostart_changed = False
        for key in DEFAULT_SETTINGS:
            if key not in new_settings:
                continue
            val = new_settings[key]
            if key in ("hotkey_recognize", "hotkey_screenshot"):
                norm, err = normalize_hotkey(val)
                if err:
                    log.error(f"Настройки: комбинация «{val}» отклонена — {err}")
                    return {"ok": False,
                            "error": f"Комбинация «{val}»: {err}",
                            "settings": dict(self._settings)}
                val = norm
                if val != self._settings.get(key):
                    changed_hotkeys = True
            elif key in ("auto_copy", "smart_hotkey", "preload", "autostart"):
                val = bool(val)
                if key == "autostart" and val != self._settings.get("autostart"):
                    autostart_changed = True
            pending[key] = val

        h1 = pending.get("hotkey_recognize", self._settings.get("hotkey_recognize"))
        h2 = pending.get("hotkey_screenshot", self._settings.get("hotkey_screenshot"))
        if h1 and h2 and h1 == h2:
            return {"ok": False,
                    "error": "Комбинации распознавания и скриншота не должны совпадать",
                    "settings": dict(self._settings)}

        self._settings.update(pending)
        self._save_settings()
        log.ok("Настройки сохранены")
        if autostart_changed:
            self._apply_autostart(self._settings["autostart"])
        if changed_hotkeys:
            self.register_hotkeys()
        return {"ok": True, **dict(self._settings)}

    @safe_call
    def get_available_langs(self) -> list:
        return config.get_available_langs()

    # ------------------------------------------------------------------
    # Автозапуск (ярлык в папке «Автозагрузка» — БЕЗ реестра и админ-прав)
    # ------------------------------------------------------------------

    def _apply_autostart(self, enabled: bool):
        lnk = config.autostart_shortcut_path()
        if not lnk:
            log.error("Автозапуск: не определена папка Startup (APPDATA)")
            return
        try:
            if not enabled:
                if os.path.isfile(lnk):
                    os.remove(lnk)
                    log.ok("Автозапуск отключён, ярлык удалён")
                    self._status("Автозапуск отключён")
                return
            exe = config.executable_path()
            if config.is_frozen():
                target, args = exe, "--minimized"
            else:
                target, args = sys_executable(), f'"{exe}" --minimized'
            ps = (
                "$s=(New-Object -COM WScript.Shell).CreateShortcut('%s');"
                "$s.TargetPath='%s';$s.Arguments='%s';"
                "$s.WorkingDirectory='%s';$s.IconLocation='%s,0';"
                "$s.Description='Распознаватель (автозапуск свёрнутым)';$s.Save()"
            ) % (
                lnk.replace("'", "''"), target.replace("'", "''"),
                args.replace("'", "''"),
                os.path.dirname(exe).replace("'", "''"),
                (config.executable_path()).replace("'", "''"),
            )
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=60, **kwargs,
            )
            if r.returncode == 0 and os.path.isfile(lnk):
                log.ok(f"Автозапуск включён: {lnk}")
                self._status("Автозапуск включён (программа будет стартовать свёрнутой в трей)")
            else:
                log.error(f"Автозапуск не создан: {(r.stderr or '')[:300]}")
                self._error("Не удалось создать автозапуск (см. журнал)")
        except Exception as exc:
            log.error(f"Сбой настройки автозапуска: {exc}")
            self._error(f"Сбой настройки автозапуска: {exc}")

    @safe_call
    def get_autostart(self) -> dict:
        lnk = config.autostart_shortcut_path()
        return {"enabled": bool(lnk and os.path.isfile(lnk)), "path": lnk or ""}

    # ------------------------------------------------------------------
    # Горячие клавиши (перерегистрация при изменении настроек)
    # ------------------------------------------------------------------

    def register_hotkeys(self) -> bool:
        """(Пере)регистрирует глобальные горячие клавиши из настроек."""
        try:
            import keyboard
        except Exception as exc:
            log.error(f"Библиотека keyboard недоступна: {exc}")
            return False
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        ok = True
        hk_rec, err1 = normalize_hotkey(self._settings.get("hotkey_recognize"))
        hk_shot, err2 = normalize_hotkey(self._settings.get("hotkey_screenshot"))
        for val, err, what in ((hk_rec, err1, "распознавания"), (hk_shot, err2, "скриншота")):
            if err:
                ok = False
                log.error(f"Комбинация {what} «{val}» неверна: {err}")
                self._error(f"Неверная комбинация {what}: {err}")
        if hk_rec and hk_shot and hk_rec == hk_shot:
            ok = False
            log.error("Комбинации распознавания и скриншота совпадают — обе не зарегистрированы")
            self._error("Комбинации распознавания и скриншота совпадают")
            return False
        if hk_rec and not err1:
            try:
                keyboard.add_hotkey(hk_rec, lambda: self.hotkey_recognize(), suppress=False)
                log.ok(f"Горячая клавиша распознавания: {hk_rec}")
            except Exception as exc:
                ok = False
                log.error(f"Не удалось назначить «{hk_rec}»: {exc}")
                self._error(f"Не удалось назначить «{hk_rec}»: {exc}")
        if hk_shot and not err2:
            try:
                keyboard.add_hotkey(hk_shot, lambda: self.hotkey_screenshot(), suppress=False)
                log.ok(f"Горячая клавиша скриншота: {hk_shot}")
            except Exception as exc:
                ok = False
                log.error(f"Не удалось назначить «{hk_shot}»: {exc}")
                self._error(f"Не удалось назначить «{hk_shot}»: {exc}")
        return ok

    def hotkey_recognize(self):
        """Срабатывание горячей клавиши распознавания (в отдельном потоке)."""
        def worker():
            smart = bool(self._settings.get("smart_hotkey", True))
            log.info(f"Горячая клавиша: {'умное распознавание (QR → текст)' if smart else 'распознавание текста'}")
            result = self.capture_smart() if smart else self.capture_and_ocr()
            self._js_raw(f"window.onHotkeyResult && window.onHotkeyResult({json.dumps(result)})")
        threading.Thread(target=worker, daemon=True).start()

    def hotkey_screenshot(self):
        """Срабатывание горячей клавиши скриншота (в отдельном потоке)."""
        def worker():
            log.info("Горячая клавиша: скриншот области")
            result = self.capture_screenshot(ask_folder=False)
            self._js_raw(f"window.onScreenshotResult && window.onScreenshotResult({json.dumps(result)})")
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Журнал
    # ------------------------------------------------------------------

    @safe_call
    def get_log(self) -> str:
        return log.get_log()

    @safe_call
    def clear_log(self) -> dict:
        log.clear_log()
        return {"ok": True}

    @safe_call
    def js_log(self, message: str):
        """Запись события из интерфейса (JS)."""
        log.info(f"[UI] {message}")
        return {"ok": True}

    @safe_call
    def js_error(self, message: str):
        """Запись ошибки из интерфейса (JS)."""
        log.error(f"[UI] {message}")
        return {"ok": True}

    # ------------------------------------------------------------------
    # Буфер обмена
    # ------------------------------------------------------------------

    @safe_call
    def copy_to_clipboard(self, text: str) -> dict:
        try:
            import pyperclip
            pyperclip.copy(text or "")
            log.ok(f"Скопировано в буфер: {len(text or '')} симв.")
            return {"ok": True}
        except Exception as exc:
            log.warn(f"pyperclip не сработал ({exc}) — пробую tkinter")
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(text or "")
            root.update()
            root.destroy()
            log.ok(f"Скопировано в буфер (tkinter): {len(text or '')} симв.")
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": f"Не удалось скопировать текст: {exc}"}

    # ------------------------------------------------------------------
    # Изображения: выбор / drag&drop (превью — всегда маленькое!)
    # ------------------------------------------------------------------

    @safe_call
    def pick_image(self):
        try:
            path = self._window.create_file_dialog(
                OPEN_DIALOG,
                directory=os.path.expanduser("~"),
                allow_multiple=False,
                file_types=IMAGE_FILTER,
            )
        except Exception as exc:
            return {"ok": False, "error": f"Диалог выбора файла не открылся: {exc}"}
        if not path:
            log.info("Выбор файла отменён")
            return {"ok": False, "cancelled": True, "error": None}
        if isinstance(path, (list, tuple)):
            path = path[0]
        path = str(path)
        if not os.path.isfile(path):
            return {"ok": False, "error": f"Файл не найден: {path}"}
        log.ok(f"Загружен файл: {path} ({os.path.getsize(path) // 1024} КБ)")
        return {
            "ok": True,
            "path": path,
            "name": os.path.basename(path),
            "base64": screen_capture.preview_base64(path),
        }

    @safe_call
    def save_dropped_file(self, name: str, data_b64: str) -> dict:
        import base64
        try:
            safe_name = os.path.basename(name or "dropped.png")
            path = os.path.join(config.TMP_DIR, f"drop_{int(time.time() * 1000)}_{safe_name}")
            with open(path, "wb") as f:
                f.write(base64.b64decode(data_b64))
            screen_capture.cleanup_temp_captures()
            log.ok(f"Файл перетащен в окно: {safe_name}")
            return {
                "ok": True,
                "path": path,
                "name": safe_name,
                "base64": screen_capture.preview_base64(path),
            }
        except Exception as exc:
            return {"ok": False, "error": f"Не удалось сохранить файл: {exc}"}

    # ------------------------------------------------------------------
    # QR-коды
    # ------------------------------------------------------------------

    @safe_call
    def decode_image(self, path: str) -> dict:
        result = qr_reader.decode_image(path)
        if result["count"] == 0:
            log.warn("Коды не найдены на изображении")
            self._status("Коды не найдены на изображении.")
        else:
            texts = "; ".join(r["text"][:60] for r in result["results"])
            log.ok(f"Найдено кодов: {result['count']} — {texts}")
            self._status(f"Найдено кодов: {result['count']}.")
        return {"ok": True, **result}

    # ------------------------------------------------------------------
    # OCR
    # ------------------------------------------------------------------

    @safe_call
    def ocr_image(self, path: str, langs=None) -> dict:
        use_langs = langs if langs is not None else self._settings.get("ocr_langs")
        if use_langs == "auto":
            self._status("Определяю язык изображения…")
        result = ocr.ocr_image(path, use_langs)
        n = len(result.get("text", "").strip())
        if n == 0:
            log.warn("Текст на изображении не найден")
            self._status("Текст на изображении не найден.")
        else:
            conf = result.get("confidence")
            det = f", язык: {result.get('detected')}" if result.get("detected") else ""
            self._status(f"Распознано символов: {n}, уверенность {conf}%{det}.")
        return {"ok": True, **result}

    # ------------------------------------------------------------------
    # Захват экрана
    # ------------------------------------------------------------------

    def _hide_window_for_capture(self) -> bool:
        """Прячет окно перед снимком. Возвращает True, если окно было спрятано нами."""
        if not self._visible:
            return False
        try:
            self._window.hide()
            self._visible = False
            time.sleep(0.25)
            return True
        except Exception as exc:
            log.warn(f"Не удалось спрятать окно перед снимком: {exc}")
            return False

    def _show_window_after_capture(self, hidden_by_us: bool):
        if not hidden_by_us:
            return
        try:
            self._window.show()
            self._visible = True
        except Exception as exc:
            log.warn(f"Не удалось показать окно после снимка: {exc}")

    def _do_capture(self):
        self._status("Выделите область на экране… (Esc — отмена)")
        shot = screen_capture.capture_region_interactive()
        if not shot:
            log.info("Выделение области отменено пользователем")
            self._status("Выделение области отменено.")
            return None
        screen_capture.cleanup_temp_captures()
        return shot

    def _shot_info(self, shot: dict) -> dict:
        return {
            "base64": shot["base64"],   # уже маленькое превью (JPEG ≤640px)
            "width": shot["width"],
            "height": shot["height"],
            "path": shot.get("path"),
        }

    @safe_call
    def capture_and_ocr(self) -> dict:
        if self._busy:
            return {"ok": False, "error": "Операция уже выполняется.", "cancelled": False}
        self._busy = True
        hidden = False
        try:
            hidden = self._hide_window_for_capture()
            shot = self._do_capture()
            self._show_window_after_capture(hidden)
            if not shot:
                return {"ok": False, "cancelled": True, "error": None}
            result = ocr.ocr_image(shot["path"], self._settings.get("ocr_langs"))
            return {"ok": True, "mode": "ocr", "shot": self._shot_info(shot), **result}
        finally:
            self._busy = False

    @safe_call
    def capture_and_decode_qr(self) -> dict:
        if self._busy:
            return {"ok": False, "error": "Операция уже выполняется.", "cancelled": False}
        self._busy = True
        hidden = False
        try:
            hidden = self._hide_window_for_capture()
            shot = self._do_capture()
            self._show_window_after_capture(hidden)
            if not shot:
                return {"ok": False, "cancelled": True, "error": None}
            result = qr_reader.decode_image(shot["path"])
            return {"ok": True, "mode": "qr", "shot": self._shot_info(shot), **result}
        finally:
            self._busy = False

    @safe_call
    def capture_smart(self) -> dict:
        if self._busy:
            return {"ok": False, "error": "Операция уже выполняется.", "cancelled": False}
        self._busy = True
        hidden = False
        try:
            hidden = self._hide_window_for_capture()
            shot = self._do_capture()
            self._show_window_after_capture(hidden)
            if not shot:
                return {"ok": False, "cancelled": True, "error": None}
            codes = qr_reader.decode_image(shot["path"])
            shot_info = self._shot_info(shot)
            if codes.get("count", 0) > 0:
                log.ok(f"Умный режим: найден QR ({codes['count']} шт.)")
                return {"ok": True, "mode": "qr", "shot": shot_info, **codes}
            text = ocr.ocr_image(shot["path"], self._settings.get("ocr_langs"))
            return {"ok": True, "mode": "ocr", "shot": shot_info, **text}
        finally:
            self._busy = False

    # ------------------------------------------------------------------
    # Скриншоты (как в FastStone Capture)
    # ------------------------------------------------------------------

    @safe_call
    def capture_screenshot(self, ask_folder: bool = False) -> dict:
        if self._busy:
            return {"ok": False, "error": "Операция уже выполняется.", "cancelled": False}
        self._busy = True
        hidden = False
        try:
            hidden = self._hide_window_for_capture()
            shot = self._do_capture()
            self._show_window_after_capture(hidden)
            if not shot:
                return {"ok": False, "cancelled": True, "error": None}

            if ask_folder:
                dst = None
                try:
                    suggested = "Скриншот_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".png"
                    dst = self._window.create_file_dialog(
                        SAVE_DIALOG,
                        directory=config.get_screenshots_dir(),
                        save_filename=suggested,
                        file_types=("PNG (*.png)",),
                    )
                    if isinstance(dst, (list, tuple)):
                        dst = dst[0] if dst else None
                except Exception as exc:
                    log.warn(f"Диалог «Сохранить как» не открылся: {exc}")
                if not dst:
                    log.info("Сохранение скриншота отменено")
                    return {"ok": False, "cancelled": True, "error": None}
                dst = str(dst)
                if not dst.lower().endswith(".png"):
                    dst += ".png"
            else:
                dst = os.path.join(
                    config.get_screenshots_dir(),
                    "Скриншот_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".png"
                )
            shutil.copyfile(shot["path"], dst)
            log.ok(f"Скриншот сохранён: {dst}")
            self._status(f"Скриншот сохранён: {dst}")
            return {
                "ok": True, "path": dst,
                "width": shot["width"], "height": shot["height"],
                "base64": shot["base64"],
            }
        finally:
            self._busy = False

    @safe_call
    def open_screenshots_dir(self) -> dict:
        d = config.get_screenshots_dir()
        if os.name == "nt":
            os.startfile(d)  # noqa: S606
        else:
            log.info(f"(не Windows) папка скриншотов: {d}")
        return {"ok": True, "path": d}

    # ------------------------------------------------------------------
    # Перевод
    # ------------------------------------------------------------------

    @safe_call
    def translate_text(self, text: str) -> dict:
        if not (text or "").strip():
            return {"translated": "", "ok": False, "error": "Пустой текст для перевода."}
        self._status("Идёт перевод (офлайн, первый запуск может занять ~30 с)…")
        result = translator.translate_en_to_ru(text)
        if result.get("ok"):
            log.ok(f"Перевод готов ({len(result['translated'])} симв.)")
            self._status("Перевод завершён.")
        return result

    @safe_call
    def preload_translator(self) -> dict:
        """Фоновый прогрев модели перевода (не блокирует интерфейс)."""
        if not self._settings.get("preload", True):
            log.info("Прогрев перевода отключён в настройках")
            return {"ok": True, "skipped": True}
        if self._preload_started:
            # вызовы из main и из интерфейса дублировались — прогрев один
            return {"ok": True, "skipped": True}
        self._preload_started = True

        def worker():
            log.info("Фоновый прогрев модели перевода…")
            if translator.init_model():
                log.ok("Модель перевода готова к работе")
            else:
                log.warn("Модель перевода не загружена (перевод будет недоступен до устранения)")
        threading.Timer(30.0, worker).start()   # стартуем через 30 с — не мешаем запуску
        return {"ok": True}

    # ------------------------------------------------------------------
    # Диагностика
    # ------------------------------------------------------------------

    @safe_call
    def check_components(self) -> dict:
        self._status("Проверка компонентов…")
        result = {
            "ocr": ocr.check_engine(),
            "qr": qr_reader.check_engines(),
            "screen": screen_capture.pil_available() or screen_capture.mss_available(),
            "clipboard": True,
            "tray": self._tray is not None,
        }
        try:
            import pyperclip
            pyperclip.copy("")
        except Exception:
            result["clipboard"] = False
        result["translator"] = translator.check_model()
        log.ok("Проверка компонентов завершена")
        self._status("Проверка компонентов завершена.")
        return result

    @safe_call
    def open_data_dir(self) -> dict:
        d = config.user_data_dir()
        if os.name == "nt":
            os.startfile(d)  # noqa: S606
        else:
            log.info(f"(не Windows) папка данных: {d}")
        return {"ok": True, "path": d}


def sys_executable() -> str:
    """Путь к python.exe (для автозапуска в режиме разработки)."""
    import sys
    return sys.executable

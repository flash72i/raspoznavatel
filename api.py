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
    "sound": True,                          # звуки-уведомления («бульк» на готово)
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
        self._zoom_win = None           # отдельное окно-лупа для картинки
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

    def _progress(self, pct, text: str = ""):
        """Шкала прогресса в интерфейсе (0-100 + подпись стадии)."""
        try:
            pct = max(0, min(100, int(pct)))
        except (TypeError, ValueError):
            pct = 0
        self._js(f"window.onProgress && window.onProgress({pct}, {json.dumps(text)})")

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
            elif key in ("auto_copy", "smart_hotkey", "preload", "autostart", "sound"):
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
    # Автоматическое распознавание файла (QR → текст) — v2.3
    # ------------------------------------------------------------------

    @safe_call
    def recognize_file(self, path: str) -> dict:
        """
        Автораспознавание добавленного изображения БЕЗ нажатия кнопок:
        сначала ищет QR/штрих-коды (быстро), если их нет — распознаёт текст
        (автоязык, смешанные языки поддерживаются). Весь путь — с прогрессом.
        """
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": f"Файл не найден: {path}"}
        if self._busy:
            return {"ok": False, "error": "Операция уже выполняется.", "cancelled": False}
        self._busy = True
        try:
            self._progress(3, "Смотрю, что на изображении…")
            try:
                codes = qr_reader.decode_image(path)
            except Exception as exc:
                log.warn(f"QR-проверка не удалась: {exc}")
                codes = {"count": 0, "results": []}
            if codes.get("count", 0) > 0:
                self._progress(100, "")
                texts = "; ".join(r["text"][:60] for r in codes["results"])
                log.ok(f"Авто: найдено кодов {codes['count']} — {texts}")
                self._status(f"Найдено кодов: {codes['count']}.")
                return {"ok": True, "mode": "qr",
                        "count": codes["count"], "results": codes["results"]}
            self._progress(12, "Кодов нет — распознаю текст…")

            def cb(pct, txt):
                self._progress(12 + int(int(pct) * 0.86), txt)

            result = ocr.ocr_image(path, self._settings.get("ocr_langs"), progress_cb=cb)
            self._progress(100, "")
            n = len(result.get("text", "").strip())
            if n == 0:
                log.warn("Авто: текст на изображении не найден")
                self._status("Текст на изображении не найден.")
            else:
                det = f", языки: {result.get('detected')}" if result.get("detected") else ""
                self._status(f"Распознано символов: {n}, уверенность {result.get('confidence')}%{det}.")
            return {"ok": True, "mode": "ocr", **result}
        finally:
            self._busy = False
            self._progress(100, "")

    # ------------------------------------------------------------------
    # OCR
    # ------------------------------------------------------------------

    @safe_call
    def ocr_image(self, path: str, langs=None) -> dict:
        use_langs = langs if langs is not None else self._settings.get("ocr_langs")
        if use_langs == "auto":
            self._status("Определяю языки изображения…")

        def cb(pct, txt):
            self._progress(int(pct), txt)

        result = ocr.ocr_image(path, use_langs, progress_cb=cb)
        n = len(result.get("text", "").strip())
        if n == 0:
            log.warn("Текст на изображении не найден")
            self._status("Текст на изображении не найден.")
        else:
            conf = result.get("confidence")
            det = f", языки: {result.get('detected')}" if result.get("detected") else ""
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
            self._progress(10, "Распознаю текст…")

            def cb(pct, txt):
                self._progress(10 + int(int(pct) * 0.85), txt)

            result = ocr.ocr_image(shot["path"], self._settings.get("ocr_langs"), progress_cb=cb)
            self._progress(100, "")
            return {"ok": True, "mode": "ocr", "shot": self._shot_info(shot), **result}
        finally:
            self._busy = False
            self._progress(100, "")

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
            self._progress(30, "Ищу QR и штрих-коды…")
            result = qr_reader.decode_image(shot["path"])
            self._progress(100, "")
            return {"ok": True, "mode": "qr", "shot": self._shot_info(shot), **result}
        finally:
            self._busy = False
            self._progress(100, "")

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
            self._progress(10, "Ищу QR и штрих-коды…")
            codes = qr_reader.decode_image(shot["path"])
            shot_info = self._shot_info(shot)
            if codes.get("count", 0) > 0:
                self._progress(100, "")
                log.ok(f"Умный режим: найден QR ({codes['count']} шт.)")
                return {"ok": True, "mode": "qr", "shot": shot_info, **codes}
            self._progress(20, "Кодов нет — распознаю текст…")

            def cb(pct, txt):
                self._progress(20 + int(int(pct) * 0.75), txt)

            text = ocr.ocr_image(shot["path"], self._settings.get("ocr_langs"), progress_cb=cb)
            self._progress(100, "")
            return {"ok": True, "mode": "ocr", "shot": shot_info, **text}
        finally:
            self._busy = False
            self._progress(100, "")

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
            self._progress(80, "Сохраняю скриншот…")
            shutil.copyfile(shot["path"], dst)
            self._progress(100, "")
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
    # Перевод (v2.3: выбор направления «с какого на какой»)
    # ------------------------------------------------------------------

    @safe_call
    def get_translation_pairs(self) -> dict:
        """Доступные направления перевода (по моделям в resources/argos)."""
        pairs = translator.available_pairs()
        names = config.LANG_NAMES
        out = [{
            "src": s, "tgt": t,
            "label": f"{names.get(s, s.upper())} → {names.get(t, t.upper())}",
        } for s, t in pairs]
        return {"ok": True, "pairs": out}

    @safe_call
    def translate_text(self, text: str, src: str = "en", tgt: str = "ru") -> dict:
        if not (text or "").strip():
            return {"translated": "", "ok": False, "error": "Пустой текст для перевода."}
        src = (src or "en").strip().lower()
        tgt = (tgt or "ru").strip().lower()
        self._status(f"Перевод {src.upper()} → {tgt.upper()} (офлайн, первая загрузка модели ~30 с)…")

        def cb(pct, txt):
            self._progress(int(pct), txt or f"Перевод {src.upper()} → {tgt.upper()}…")

        result = translator.translate(text, src, tgt, progress_cb=cb)
        self._progress(100, "")
        if result.get("ok"):
            log.ok(f"Перевод {src}→{tgt} готов ({len(result['translated'])} симв.)")
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


    # ------------------------------------------------------------------
    # Окно-лупа: клик по превью открывает картинку в ОТДЕЛЬНОМ окне
    # (появляется над картинкой, плавно увеличивается, колесо = зум,
    #  перетаскивание = двигать окно, Esc/✕ = закрыть)
    # ------------------------------------------------------------------

    @safe_call
    def open_image_zoom(self, path: str, x: int = 0, y: int = 0) -> dict:
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": f"Файл не найден: {path}"}
        try:
            import webview
        except Exception as exc:
            return {"ok": False, "error": f"Оконный компонент недоступен: {exc}"}

        import base64
        # data-URL картинки: маленькие файлы — как есть, большие уменьшаем
        try:
            size = os.path.getsize(path)
            pil = screen_capture._get_pil()
            nat_w = nat_h = 0
            if pil is not None:
                with pil[0].open(path) as im0:
                    nat_w, nat_h = im0.size
            if size <= 6 * 1024 * 1024:
                with open(path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
                mime = "jpeg" if ext in ("jpg", "jpeg") else ext
                data_url = f"data:image/{mime};base64,{b64}"
            elif pil is not None:
                import io
                with pil[0].open(path) as im0:
                    im0 = im0.convert("RGB")
                    im0.thumbnail((2400, 2400))
                    buf = io.BytesIO()
                    im0.save(buf, "JPEG", quality=88)
                data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            else:
                return {"ok": False, "error": "Файл слишком большой, а Pillow недоступен."}
        except Exception as exc:
            return {"ok": False, "error": f"Не удалось прочитать изображение: {exc}"}

        # размер окна — по пропорциям картинки, не больше 980×700
        w, h = 860, 620
        if nat_w and nat_h:
            k = min(940.0 / nat_w, 640.0 / nat_h, 1.5)
            w = max(360, min(980, int(nat_w * k) + 40))
            h = max(280, min(700, int(nat_h * k) + 96))

        # позиция: НАД точкой клика (а не по центру экрана)
        px = max(0, int(x) - 60) if x else None
        py = max(0, int(y) - h - 30) if y else None

        name = os.path.basename(path)
        html = (ZOOM_VIEWER_HTML
                .replace("__SRC__", data_url)
                .replace("__NAME__", name.replace("<", "&lt;").replace(">", "&gt;")))

        # предыдущее окно-лупу закрываем
        self.close_zoom_window(quiet=True)

        zapi = _ZoomApi()
        kwargs = dict(
            title=f"Лупа — {name}",
            html=html,
            width=w, height=h,
            frameless=True,
            easy_drag=True,      # перетаскивание окна мышью за любое место
            on_top=True,         # поверх главного окна
            resizable=True,
            background_color="#0a0f1e",
            js_api=zapi,
        )
        if px is not None:
            kwargs["x"] = px
            kwargs["y"] = py
        try:
            win = webview.create_window(**kwargs)
            zapi._win = win
            self._zoom_win = win
            log.ok(f"Окно-лупа открыто: {name} ({w}×{h}, позиция {px},{py})")
            return {"ok": True}
        except Exception as exc:
            log.error(f"Не удалось открыть окно-лупу: {exc}")
            return {"ok": False, "error": str(exc)}

    @safe_call
    def close_zoom_window(self, quiet: bool = False) -> dict:
        """Закрывает окно-лупу (если открыто)."""
        win = self._zoom_win
        self._zoom_win = None
        if win is None:
            return {"ok": True}

        def _destroy():
            time.sleep(0.15)
            try:
                win.destroy()
            except Exception:
                pass
        threading.Thread(target=_destroy, daemon=True).start()
        return {"ok": True}


ZOOM_VIEWER_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; overflow: hidden; background: #0a0f1e;
    font-family: 'Segoe UI', system-ui, sans-serif; color: #cdd6f4;
    -webkit-user-select: none; user-select: none; }
  #bar { position: fixed; top: 0; left: 0; right: 0; height: 34px; z-index: 10;
    display: flex; align-items: center; gap: 8px; padding: 0 10px;
    background: linear-gradient(180deg, rgba(10,15,30,.96), rgba(10,15,30,.75));
    border-bottom: 1px solid rgba(120,140,255,.14); font-size: 11.5px; }
  #name { font-weight: 600; color: #e8ecff; max-width: 45%; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  #hint { color: #7f8bb5; font-size: 10.5px; flex: 1; text-align: right;
    overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  #zoomPct { color: #22d3ee; font-weight: 700; min-width: 46px; text-align: right; }
  #closeBtn { width: 26px; height: 26px; border: 0; border-radius: 7px; cursor: pointer;
    background: rgba(255,93,108,.14); color: #ff8b96; font-size: 13px; line-height: 1;
    transition: background .15s, transform .15s; }
  #closeBtn:hover { background: rgba(255,93,108,.35); transform: scale(1.08); }
  #stage { position: absolute; inset: 34px 0 0 0; display: flex;
    align-items: center; justify-content: center; overflow: hidden; }
  #img { max-width: 96%; max-height: 94%; object-fit: contain; cursor: grab;
    transform: scale(1); transform-origin: center center;
    transition: transform .14s cubic-bezier(.22,.9,.35,1.2), opacity .18s;
    opacity: 0; border-radius: 6px; box-shadow: 0 14px 60px rgba(0,0,0,.6); }
  #img.shown { opacity: 1; }
</style>
</head>
<body>
  <div id="bar">
    <span id="name">__NAME__</span>
    <span id="hint">колесо = зум · двойной клик = 100% · тащить = двигать окно · Esc = закрыть</span>
    <span id="zoomPct">100%</span>
    <button id="closeBtn" title="Закрыть (Esc)">✕</button>
  </div>
  <div id="stage"><img id="img" src="__SRC__" alt=""></div>
<script>
  var scale = 1;
  var img = document.getElementById('img');
  var pct = document.getElementById('zoomPct');
  function apply(){ img.style.transform = 'scale(' + scale + ')';
    pct.textContent = Math.round(scale * 100) + '%'; }
  window.addEventListener('wheel', function(e){
    e.preventDefault();
    var k = e.deltaY < 0 ? 1.16 : 1 / 1.16;
    scale = Math.min(14, Math.max(0.15, scale * k));
    apply();
  }, {passive: false});
  img.addEventListener('dblclick', function(){ scale = 1; apply(); });
  img.addEventListener('load', function(){
    img.classList.add('shown');
    img.style.transform = 'scale(1)';
  });
  // плавное «наплывающее» появление: стартуем с 0.72
  img.style.transform = 'scale(0.72)';
  setTimeout(function(){ scale = 1; apply(); img.classList.add('shown'); }, 60);
  function closeWin(){
    try { window.pywebview.api.close_zoom(); } catch(e) { window.close(); }
  }
  document.getElementById('closeBtn').addEventListener('click', closeWin);
  window.addEventListener('keydown', function(e){
    if (e.key === 'Escape') closeWin();
    if (e.key === '+' || e.key === '=') { scale = Math.min(14, scale * 1.2); apply(); }
    if (e.key === '-') { scale = Math.max(0.15, scale / 1.2); apply(); }
    if (e.key === '0') { scale = 1; apply(); }
  });
</script>
</body>
</html>
"""


class _ZoomApi:
    """Мини-API окна-лупы (js_api второго окна)."""

    def __init__(self):
        self._win = None

    def close_zoom(self):
        win = self._win
        if win is None:
            return {"ok": False}

        def _destroy():
            time.sleep(0.12)
            try:
                win.destroy()
            except Exception:
                pass
        threading.Thread(target=_destroy, daemon=True).start()
        return {"ok": True}


def sys_executable() -> str:
    """Путь к python.exe (для автозапуска в режиме разработки)."""
    import sys
    return sys.executable

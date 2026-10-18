# -*- coding: utf-8 -*-
"""
api.py — Python-API приложения «Распознаватель» (v2.4), доступное из JavaScript.

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
import threading
import time
import traceback
from collections import deque
from datetime import datetime

import config
import log
import ocr
import proc
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

def safety_net_action(visible: bool, tray_healthy: bool, bad_streak: int,
                      threshold: int = 3):
    """
    v2.6.1, чистая функция «страховки трея»: что делать, если окно спрятано.
    :return: (действие, новый счётчик), действие ∈ {"idle", "wait", "show"}
    Пользователь НЕ должен остаться без окна и без работающей иконки: если трей
    несколько проверок подряд не здоров — окно возвращается на экран.
    """
    if visible or tray_healthy:
        return ("idle", 0)
    bad_streak += 1
    if bad_streak >= threshold:
        return ("show", 0)
    return ("wait", bad_streak)


# Коды диалогов pywebview (webview.OPEN_DIALOG / FOLDER_DIALOG / SAVE_DIALOG)
OPEN_DIALOG = 10
FOLDER_DIALOG = 20
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
    "screenshots_dir": "",                  # своя папка скриншотов ("" = Screenshots рядом с программой)
    "close_action": "exit",                 # ✕: "exit" — закрыть программу, "tray" — свернуть в трей
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
        self._closing = False                # программа закрывается (v2.6.1)
        self._net_started = False            # страховка трея запущена (v2.6.1)
        self._busy = False
        self._preload_started = False   # прогрев перевода запускается один раз
        self._history_lock = threading.Lock()   # история распознаваний (v2.5)
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

    def _effective_screenshots_dir(self) -> str:
        """Папка для скриншотов: своя из настроек (если задана и существует),
        иначе Screenshots рядом с программой."""
        custom = str(self._settings.get("screenshots_dir") or "").strip()
        if custom and os.path.isdir(custom):
            return custom
        return config.get_screenshots_dir()

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
            # урок L6: на x64 дескрипторы обязаны быть 64-битными, иначе HWND
            # обрезается и вызовы молча бьют «мимо» окна
            user32.FindWindowW.restype = ctypes.c_void_p
            user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextW.restype = ctypes.c_int
            user32.GetWindowTextW.argtypes = [
                wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.EnumWindows.restype = wintypes.BOOL
            user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]

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
        # Настройка «действие крестика»: свернуть в трей вместо выхода
        action = str(self._settings.get("close_action") or "exit").lower()
        if action == "tray":
            if self._tray is not None:
                # v2.6.0/v2.6.1: проверяем ЗДОРОВЬЕ трея (значок + ЖИВОЙ насос
                # сообщений). Если трей не чинится — закрываем программу как
                # обычно: пользователь не должен остаться с процессом без окна.
                try:
                    healthy = self._tray.ensure_icon()
                except Exception as exc:
                    log.error(f"Проверка здоровья трея не удалась: {exc}")
                    healthy = False
                if healthy:
                    try:
                        self.win_force_hide()
                        self.start_tray_safety_net()
                        if not getattr(self, "_tray_hint_shown", False):
                            self._tray_hint_shown = True
                            try:
                                self._tray.notify(
                                    "Распознаватель",
                                    "Программа свёрнута в трей. Если значка не "
                                    "видно: нажмите стрелку ^ у часов и "
                                    "закрепите значок там.")
                            except Exception:
                                pass
                        log.info("✕: программа свёрнута в трей (настройка close_action=tray)")
                        return {"ok": True, "closed": False, "minimized_to_tray": True}
                    except Exception as exc:
                        log.error(f"Не удалось свернуть в трей ({exc}) — закрываю полностью")
                else:
                    log.warn("close_action=tray, но значок трея не создался — "
                             "закрываю программу полностью")
            else:
                log.warn("close_action=tray, но трей недоступен — закрываю программу полностью")
        log.info("Закрытие приложения по кнопке ✕")
        self._closing = True          # v2.6.1: страховка трея больше не нужна
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
        return self.win_force_show()

    @safe_call
    def win_hide_to_tray(self):
        """Спрятать окно в трей (иконка остаётся в области уведомлений)."""
        return self.win_force_hide()

    @safe_call
    def win_show(self):
        """Показать окно (из трея)."""
        return self.win_force_show()

    # ------------------------------------------------------------------
    # v2.6.1: надёжный показ/скрытие окна
    # ------------------------------------------------------------------

    def _win32_show_hide(self, show: bool) -> bool:
        """
        Показать/спрятать главное окно НАПРЯМУЮ через Win32.

        Зачем: `window.show()/hide()` у pywebview — это синхронный `Invoke`
        в GUI-поток WinForms. Вызванный из потока трея (или из потока захвата
        экрана) он блокируется, если GUI-поток занят, и трей «замерзает»:
        иконка видна, но клики не работают. `ShowWindow`/`SetForegroundWindow`
        работают из любого потока и не ждут ответа.
        :return: True, если окно реально обработано Win32.
        """
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = self._get_hwnd()
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            user32.IsIconic.restype = wintypes.BOOL
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.BringWindowToTop.restype = wintypes.BOOL
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            SW_HIDE, SW_SHOWNORMAL, SW_RESTORE = 0, 1, 9
            if show:
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)
                user32.ShowWindow(hwnd, SW_SHOWNORMAL)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            else:
                user32.ShowWindow(hwnd, SW_HIDE)
            return True
        except Exception as exc:
            log.warn(f"Win32-{'показ' if show else 'скрытие'} окна не удался: {exc}")
            return False

    def win_force_show(self) -> dict:
        """
        Гарантированно вернуть окно на экран (клик по иконке трея, повторный
        запуск exe, возврат после снимка, страховка «мёртвого» трея).
        Сначала Win32, и только если HWND неизвестен — pywebview.
        """
        if self._win32_show_hide(True):
            self._visible = True
            return {"ok": True, "via": "win32"}
        try:
            self._window.show()
        except Exception as exc:
            log.warn(f"pywebview show() не удался: {exc}")
        try:
            self._window.restore()
        except Exception:
            pass
        self._visible = True
        return {"ok": True, "via": "pywebview"}

    def win_force_hide(self) -> dict:
        """Спрятать окно в трей (без блокирующего Invoke в GUI-поток)."""
        if self._win32_show_hide(False):
            self._visible = False
            return {"ok": True, "via": "win32"}
        try:
            self._window.hide()
            self._visible = False
            return {"ok": True, "via": "pywebview"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # v2.6.1: страховка — пользователь не остаётся без окна и без иконки
    # ------------------------------------------------------------------

    def start_tray_safety_net(self):
        """Запускает фоновый контроль: окно спрятано, а трей не отвечает → вернуть окно."""
        if self._net_started:
            return
        self._net_started = True
        threading.Thread(target=self._tray_safety_net, daemon=True).start()

    def _tray_safety_net(self):
        bad = 0
        while not self._closing:
            time.sleep(3)
            if self._closing:
                return
            try:
                if self._tray is None:
                    bad = 0
                    continue
                fn = getattr(self._tray, "healthy", None)
                try:
                    healthy = bool(fn()) if callable(fn) else bool(
                        getattr(self._tray, "icon_shown", False))
                except Exception:
                    healthy = False
                action, bad = safety_net_action(self._visible, healthy, bad)
                if action == "show":
                    log.warn("Окно спрятано, а трей не отвечает — возвращаю окно на экран")
                    self.win_force_show()
            except Exception as exc:
                log.warn(f"Страховка трея: {exc}")

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
            "screenshots_dir": self._effective_screenshots_dir(),
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
                # ВАЖНО: отслеживание изменения автозапуска — именно в этой
                # ветке (в v2.4.0 строка случайно уехала в ветку close_action,
                # из-за чего ярлык в «Автозагрузке» не создавался/не удалялся).
                if key == "autostart" and val != bool(self._settings.get("autostart")):
                    autostart_changed = True
            elif key == "screenshots_dir":
                val = str(val or "").strip()
                if val and not os.path.isdir(val):
                    return {"ok": False,
                            "error": f"Папка скриншотов не существует: {val}",
                            "settings": dict(self._settings)}
            elif key == "close_action":
                val = str(val or "exit").strip().lower()
                if val not in ("exit", "tray"):
                    return {"ok": False,
                            "error": f"Неверное действие крестика: {val} (можно exit или tray)",
                            "settings": dict(self._settings)}
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
            # v2.6.0: единая точка скрытия консоли (proc.run) —
            # окно powershell не мелькает при включении автозапуска
            r = proc.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=60,
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
                self._history_add("qr", "\n".join(r["text"] for r in codes["results"]),
                                  {"source": "file", "count": codes["count"]})
                return {"ok": True, "mode": "qr",
                        "count": codes["count"], "results": codes["results"]}
            self._progress(12, "Кодов нет — распознаю текст…")

            def cb(pct, txt):
                self._progress(12 + int(int(pct) * 0.86), txt)

            result = ocr.ocr_image(path, self._settings.get("ocr_langs"), progress_cb=cb)
            self._progress(100, "")
            n = len(result.get("text", "").strip())
            if n:
                self._history_add("ocr", result.get("text", ""), {
                    "source": "file", "langs": result.get("lang"),
                    "confidence": result.get("confidence")})
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
            self.win_force_hide()
            time.sleep(0.25)
            return True
        except Exception as exc:
            log.warn(f"Не удалось спрятать окно перед снимком: {exc}")
            return False

    def _show_window_after_capture(self, hidden_by_us: bool):
        if not hidden_by_us:
            return
        try:
            self.win_force_show()
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
            if (result.get("text") or "").strip():
                self._history_add("ocr", result.get("text", ""), {
                    "source": "screen", "langs": result.get("lang"),
                    "confidence": result.get("confidence")})
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
            if result.get("count", 0) > 0:
                self._history_add("qr", "\n".join(r["text"] for r in result["results"]),
                                  {"source": "screen", "count": result["count"]})
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
                self._history_add("qr", "\n".join(r["text"] for r in codes["results"]),
                                  {"source": "screen", "count": codes["count"]})
                return {"ok": True, "mode": "qr", "shot": shot_info, **codes}
            self._progress(20, "Кодов нет — распознаю текст…")

            def cb(pct, txt):
                self._progress(20 + int(int(pct) * 0.75), txt)

            text = ocr.ocr_image(shot["path"], self._settings.get("ocr_langs"), progress_cb=cb)
            self._progress(100, "")
            if (text.get("text") or "").strip():
                self._history_add("ocr", text.get("text", ""), {
                    "source": "screen", "langs": text.get("lang"),
                    "confidence": text.get("confidence")})
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
                        directory=self._effective_screenshots_dir(),
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
                    self._effective_screenshots_dir(),
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
    def choose_screenshots_dir(self) -> dict:
        """Диалог выбора папки для скриншотов; результат сразу сохраняется."""
        try:
            picked = self._window.create_file_dialog(
                FOLDER_DIALOG,
                directory=self._effective_screenshots_dir(),
            )
        except Exception as exc:
            return {"ok": False, "error": f"Диалог выбора папки не открылся: {exc}"}
        if not picked:
            log.info("Выбор папки скриншотов отменён")
            return {"ok": False, "cancelled": True, "error": None}
        if isinstance(picked, (list, tuple)):
            picked = picked[0]
        picked = str(picked)
        if not os.path.isdir(picked):
            return {"ok": False, "error": f"Это не папка: {picked}"}
        self._settings["screenshots_dir"] = picked
        self._save_settings()
        log.ok(f"Папка скриншотов изменена: {picked}")
        self._status(f"Скриншоты будут сохраняться в: {picked}")
        return {"ok": True, "path": picked, "default": config.get_screenshots_dir()}

    @safe_call
    def reset_screenshots_dir(self) -> dict:
        """Вернуть папку скриншотов по умолчанию (рядом с программой)."""
        self._settings["screenshots_dir"] = ""
        self._save_settings()
        d = config.get_screenshots_dir()
        log.ok(f"Папка скриншотов сброшена: {d}")
        self._status(f"Скриншоты снова сохраняются в: {d}")
        return {"ok": True, "path": d, "default": d}

    @safe_call
    def open_screenshots_dir(self) -> dict:
        d = self._effective_screenshots_dir()
        if os.name == "nt":
            os.startfile(d)  # noqa: S606
        else:
            log.info(f"(не Windows) папка скриншотов: {d}")
        return {"ok": True, "path": d}

    # ------------------------------------------------------------------
    # v2.5: ИСТОРИЯ распознаваний (кнопка «История» на панели инструментов)
    # ------------------------------------------------------------------

    def _history_load(self) -> list:
        try:
            if os.path.isfile(config.HISTORY_FILE):
                with open(config.HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return [x for x in data if isinstance(x, dict)]
        except Exception as exc:
            log.warn(f"Не удалось прочитать историю ({exc}) — начинаю пустую")
        return []

    def _history_save(self, items: list):
        try:
            with open(config.HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
        except OSError as exc:
            log.error(f"Не удалось сохранить историю: {exc}")

    def _history_add(self, kind: str, text: str, meta: dict = None):
        """
        Добавляет запись в историю распознаваний (новейшие сверху,
        не больше config.HISTORY_MAX_ITEMS). Пустой текст не пишется.
        """
        text = (text or "").strip()
        if not text:
            return
        item = {
            "id": f"{int(time.time() * 1000)}-{os.getpid() % 1000:03d}-"
                  f"{len(text) % 97:02d}",
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,                 # ocr | qr
            "text": text[:20000],
        }
        item.update(meta or {})
        with self._history_lock:
            items = self._history_load()
            items.insert(0, item)
            self._history_save(items[:config.HISTORY_MAX_ITEMS])
        log.info(f"История: добавлена запись ({kind}, {len(text)} симв.)")

    @safe_call
    def get_history(self, limit: int = 200, query: str = "") -> dict:
        q = (query or "").strip().lower()
        items = self._history_load()
        if q:
            items = [x for x in items if q in (x.get("text") or "").lower()]
        return {"ok": True, "items": items[:int(limit or 200)],
                "total": len(self._history_load())}

    @safe_call
    def history_delete(self, item_id: str) -> dict:
        with self._history_lock:
            items = self._history_load()
            before = len(items)
            items = [x for x in items if str(x.get("id")) != str(item_id)]
            if len(items) == before:
                return {"ok": False, "error": "Запись не найдена."}
            self._history_save(items)
        return {"ok": True}

    @safe_call
    def history_clear(self) -> dict:
        with self._history_lock:
            self._history_save([])
        log.ok("История распознаваний очищена")
        return {"ok": True}

    # ------------------------------------------------------------------
    # v2.5: языковые пакеты перевода («как Яндекс Переводчик»)
    # ------------------------------------------------------------------

    @safe_call
    def get_translation_langs(self) -> dict:
        """
        Каталог языков перевода + установленные пакеты + доступные для
        скачивания направления (индекс Argos, кэш 7 дней).
        """
        packs = translator.installed_packs()
        installed = [(p["src"], p["tgt"]) for p in packs]
        remote = []
        try:
            for p in translator.remote_packages():
                remote.append({"src": p["src"], "tgt": p["tgt"], "size": p["size"]})
        except Exception as exc:
            log.warn(f"Индекс языковых пакетов недоступен: {exc}")
        return {
            "ok": True,
            "catalog": translator.LANGUAGE_CATALOG,
            "packs": packs,
            "installed": installed,
            "remote": remote,
        }

    @safe_call
    def download_translation_pack(self, src: str, tgt: str) -> dict:
        """Скачивает языковой пакет (нужен интернет один раз)."""
        src = (src or "").lower().strip()
        tgt = (tgt or "").lower().strip()
        self._status(f"Скачиваю языковой пакет {src.upper()}→{tgt.upper()}…")

        def cb(pct, txt):
            self._progress(int(pct), txt)

        res = translator.download_pair(src, tgt, progress_cb=cb)
        self._progress(100, "")
        if res.get("ok"):
            log.ok(f"Языковой пакет {src}→{tgt} установлен "
                   f"({res['size'] // (1024 * 1024)} МБ)")
            self._status(f"Пакет {src.upper()}→{tgt.upper()} установлен.")
        else:
            log.error(f"Пакет {src}→{tgt} не скачан: {res.get('error')}")
        return res

    @safe_call
    def remove_translation_pack(self, src: str, tgt: str) -> dict:
        res = translator.remove_pair((src or "").lower(), (tgt or "").lower())
        if res.get("ok"):
            log.ok(f"Языковой пакет {src}→{tgt} удалён")
        return res

    # ------------------------------------------------------------------
    # v2.5: стили окна — возврат кнопки «свернуть» (клик по панели задач)
    # ------------------------------------------------------------------

    def _fix_window_styles(self):
        """
        Безрамочное окно pywebview создаётся БЕЗ стиля WS_MINIMIZEBOX,
        поэтому клик по кнопке приложения на панели задач Windows только
        РАЗВОРАЧИВАЛ окно, но не сворачивал активное. Возвращаем стили
        WS_MINIMIZEBOX|WS_MAXIMIZEBOX — панель задач работает штатно:
        клик сворачивает активное окно и разворачивает свёрнутое.
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            hwnd = self._get_hwnd()
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [
                wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            GWL_STYLE = -16
            WS_MAXIMIZEBOX = 0x00010000
            WS_MINIMIZEBOX = 0x00020000
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            if style & WS_MINIMIZEBOX:
                return
            user32.SetWindowLongPtrW(hwnd, GWL_STYLE,
                                     style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
            log.ok("Окно: включён стиль сворачивания — клик по панели задач "
                   "теперь сворачивает окно")
        except Exception as exc:
            log.warn(f"Не удалось включить стиль сворачивания: {exc}")

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
        label = "авто" if src == "auto" else src.upper()
        self._status(f"Перевод {label} → {tgt.upper()} (офлайн)…")

        def cb(pct, txt):
            self._progress(int(pct), txt or f"Перевод {label} → {tgt.upper()}…")

        result = translator.translate(text, src, tgt, progress_cb=cb)
        self._progress(100, "")
        if result.get("ok"):
            via = f" (через {result['via']})" if result.get("via") else ""
            det = f" (язык: {result['detected']})" if result.get("detected") else ""
            log.ok(f"Перевод {result.get('src')}→{tgt} готов "
                   f"({len(result['translated'])} симв.){via}{det}")
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
            pairs = translator.available_pairs()
            first = ("en", "ru") if ("en", "ru") in pairs else (pairs[0] if pairs else None)
            if not first:
                log.warn("Модели перевода не установлены — прогрев не нужен")
                return
            log.info(f"Фоновый прогрев модели перевода {first[0]}→{first[1]}…")
            if translator.init_model(first[0], first[1]):
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
            min_size=(340, 240),
            frameless=True,
            easy_drag=False,     # перетаскивание окна — только за верхнюю панель
            draggable=False,     # ВАЖНО: гасит нативный HTML5-drag картинок,
                                 # иначе WebView2 перехватывает мышь и окно не сдвинуть
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
  /* верхняя панель = зона перетаскивания ОКНА (pywebview-drag-region) */
  #bar { position: fixed; top: 0; left: 0; right: 0; height: 36px; z-index: 10;
    display: flex; align-items: center; gap: 6px; padding: 0 8px 0 12px;
    background: linear-gradient(180deg, rgba(13,19,38,.97), rgba(10,15,30,.85));
    border-bottom: 1px solid rgba(120,140,255,.16); font-size: 11.5px;
    cursor: move; }
  #name { font-weight: 600; color: #e8ecff; max-width: 34%; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  #hint { color: #7f8bb5; font-size: 10px; flex: 1; text-align: center;
    overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .vbtn { border: 0; border-radius: 7px; cursor: pointer; height: 24px; min-width: 26px;
    padding: 0 6px; background: rgba(120,140,255,.12); color: #cdd6f4;
    font-size: 12px; font-weight: 700; line-height: 1; transition: background .15s, transform .12s; }
  .vbtn:hover { background: rgba(120,140,255,.3); transform: translateY(-1px); }
  #zoomPct { color: #22d3ee; font-weight: 700; min-width: 48px; text-align: center;
    font-size: 11px; cursor: pointer; }
  #closeBtn { background: rgba(255,93,108,.14); color: #ff8b96; }
  #closeBtn:hover { background: rgba(255,93,108,.4); }
  #stage { position: absolute; inset: 36px 0 0 0; display: flex;
    align-items: center; justify-content: center; overflow: hidden; cursor: grab; }
  #stage:active { cursor: grabbing; }
  #img { max-width: 94%; max-height: 92%; object-fit: contain; cursor: grab;
    transform: translate(0px, 0px) scale(1); transform-origin: center center;
    opacity: 0; border-radius: 6px; box-shadow: 0 14px 60px rgba(0,0,0,.6);
    -webkit-user-drag: none; user-drag: none; touch-action: none; }
  #img.smooth { transition: transform .14s cubic-bezier(.22,.9,.35,1.15), opacity .18s; }
  #img.shown { opacity: 1; }
  #img.dragging { cursor: grabbing; }
</style>
</head>
<body>
  <div id="bar" class="pywebview-drag-region">
    <span id="name">__NAME__</span>
    <span id="hint">колесо = зум · тащить картинку = двигать · за панель = двигать окно · Esc = закрыть</span>
    <button class="vbtn" id="zoomOut" title="Уменьшить (-)">−</button>
    <span id="zoomPct" title="Сбросить (0 / двойной клик)">100%</span>
    <button class="vbtn" id="zoomIn" title="Увеличить (+)">+</button>
    <button class="vbtn" id="closeBtn" title="Закрыть (Esc)">✕</button>
  </div>
  <div id="stage"><img id="img" class="smooth" src="__SRC__" alt="" draggable="false"></div>
<script>
  var scale = 1, tx = 0, ty = 0;
  var img = document.getElementById('img');
  var stage = document.getElementById('stage');
  var pct = document.getElementById('zoomPct');
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function apply() {
    img.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
    pct.textContent = Math.round(scale * 100) + '%';
  }
  function zoomAt(cx, cy, k) {   // cx,cy — относительно ЦЕНТРА контейнера
    var ns = clamp(scale * k, 0.1, 20);
    var f = ns / scale;
    tx = cx - f * (cx - tx);
    ty = cy - f * (cy - ty);
    scale = ns;
    img.classList.add('smooth');
    apply();
  }
  function zoomCenter(k) { var r = stage.getBoundingClientRect();
    zoomAt(0, 0, k); }
  function reset() { scale = 1; tx = 0; ty = 0; img.classList.add('smooth'); apply(); }

  /* --- зум колесом — к курсору --- */
  window.addEventListener('wheel', function(e) {
    e.preventDefault();
    var r = stage.getBoundingClientRect();
    var cx = e.clientX - r.left - r.width / 2;
    var cy = e.clientY - r.top - r.height / 2;
    zoomAt(cx, cy, e.deltaY < 0 ? 1.16 : 1 / 1.16);
  }, {passive: false});

  /* --- панорамирование картинки мышью (тащить влево/вправо/вверх/вниз).
         Слушаем ВЕСЬ stage (а не только img) — картинку можно тянуть,
         ухватившись за любое место контентной области. --- */
  var panning = false, px0 = 0, py0 = 0;
  stage.addEventListener('mousedown', function(e) {
    if (e.button !== 0) return;
    e.preventDefault();               // гасим нативный drag картинки/выделение
    panning = true; px0 = e.clientX; py0 = e.clientY;
    img.classList.remove('smooth');   // движение — без задержки, 1:1 за мышью
    img.classList.add('dragging');
  });
  window.addEventListener('mousemove', function(e) {
    if (!panning) return;
    tx += e.clientX - px0; ty += e.clientY - py0;
    px0 = e.clientX; py0 = e.clientY;
    apply();
  });
  window.addEventListener('mouseup', function() {
    if (!panning) return;
    panning = false; img.classList.remove('dragging');
  });

  /* --- страховка перетаскивания ОКНА за панель: дублируем механизм
         pywebview (абсолютные координаты — двойной вызов безопасен) --- */
  (function() {
    var bar = document.getElementById('bar');
    var d = null;
    bar.addEventListener('mousedown', function(e) {
      if (e.target.closest('button')) return;
      if (!window.pywebview || !window.pywebview._jsApiCallback) return;
      d = { cx: e.clientX, cy: e.clientY };
      e.preventDefault();
    });
    window.addEventListener('mousemove', function(e) {
      if (!d) return;
      try { window.pywebview._jsApiCallback('pywebviewMoveWindow',
        [e.screenX - d.cx, e.screenY - d.cy], 'move'); } catch (err) {}
    });
    window.addEventListener('mouseup', function() { d = null; });
  })();

  /* --- кнопки, клавиатура --- */
  document.getElementById('zoomIn').addEventListener('click', function() { zoomCenter(1.25); });
  document.getElementById('zoomOut').addEventListener('click', function() { zoomCenter(1 / 1.25); });
  pct.addEventListener('click', reset);
  img.addEventListener('dblclick', reset);
  function closeWin() {
    try { window.pywebview.api.close_zoom(); } catch (e) { try { window.close(); } catch (e2) {} }
  }
  document.getElementById('closeBtn').addEventListener('click', closeWin);
  window.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') { closeWin(); return; }
    var step = e.shiftKey ? 160 : 60;
    if (e.key === 'ArrowLeft')  { img.classList.remove('smooth'); tx += step; apply(); e.preventDefault(); }
    if (e.key === 'ArrowRight') { img.classList.remove('smooth'); tx -= step; apply(); e.preventDefault(); }
    if (e.key === 'ArrowUp')    { img.classList.remove('smooth'); ty += step; apply(); e.preventDefault(); }
    if (e.key === 'ArrowDown')  { img.classList.remove('smooth'); ty -= step; apply(); e.preventDefault(); }
    if (e.key === '+' || e.key === '=') zoomCenter(1.25);
    if (e.key === '-' || e.key === '_') zoomCenter(1 / 1.25);
    if (e.key === '0') reset();
  });

  /* --- плавное появление --- */
  img.style.transform = 'translate(0px,0px) scale(0.72)';
  function show() { img.classList.add('shown'); scale = 1; apply(); }
  if (img.complete) { setTimeout(show, 50); } else { img.addEventListener('load', function() { setTimeout(show, 30); }); }
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

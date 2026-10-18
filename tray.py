# -*- coding: utf-8 -*-
"""
tray.py — иконка в системном трее Windows (чистый ctypes, без зависимостей).

Нужна для режима автозапуска: программа стартует вместе с Windows
в свёрнутом виде и живёт в трее.

Поведение кнопок мыши:
  • левый клик  — показать окно, а если оно уже открыто — скрыть в трей;
  • правый клик — меню «Показать/Скрыть окно», «Выход».

v2.6.0 — ОКОНЧАТЕЛЬНОЕ ИСПРАВЛЕНИЕ «белого невидимого меню».
  В v2.5.0 меню уже рисовалось владельцем (MF_OWNERDRAW), но на 64-битной
  Windows прототип оконной процедуры объявлял WPARAM/LPARAM как 32-битные
  типы, а LRESULT как c_long: указатель в LPARAM обрезался до 32 бит,
  обработчики WM_MEASUREITEM/WM_DRAWITEM получали мусор и не выполнялись.
  Результат — белое системное меню с ПУСТЫМИ пунктами (надписей не видно).
  Теперь:
  * прототипы сообщений 64-битно-корректны (WPARAM=UINT_PTR, LPARAM=LONG_PTR,
    LRESULT=LONG_PTR) на любой платформе;
  * у всех вызовов GDI/User32 явно заданы argtypes/restype — дескрипторы
    (кисть, перо, шрифт) не «обрезаются» на x64;
  * отрисовка пунктов обёрнута в try/except: если самоотрисовка когда-либо
    не смогла выполниться, программа АВТОМАТИЧЕСКИ переключается на обычное
    системное меню (MF_STRING) — надписи будут читаемы в любой теме Windows;
  * цвета пунктов задаёт сама программа (тёмный фон в стиле интерфейса,
    контрастный светлый текст) — видимость не зависит от темы системы;
  * «здоровье» трея проверяется явно (icon_shown / ensure_icon): если поток
    трея или значок погибли, программа пробует создать значок снова, а режим
    «свернуть в трей» не оставляет пользователя с процессом без окна;
  * при первом сворачивании в трей показывается всплывающая подсказка:
    новый значок Windows по умолчанию прячет под стрелку «^» у часов.

v2.6.1 — «иконка видна, но не нажимается» и «программа не запускается».
  Диагноз v2.6.0: значок и его скрытое окно-владелец создавались в потоке,
  который НЕ качает сообщения (watchdog), а колбэки кликов вызывались ПРЯМО из
  насоса: pywebview `window.show()` — это синхронный `Invoke` в GUI-поток, и
  если тот занят, поток трея замирал навсегда. Значок оставался в трее, но ни
  левый, ни правый клик не работали, а повторный запуск exe молча завершался
  (single-instance находил «окно» и постил сообщение в мёртвую очередь).
  Теперь:
  * окно трея и значок создаёт ТОЛЬКО поток с насосом сообщений (у других
    потоков `_add_icon()` отказывается работать);
  * пульс насоса: `SetTimer` раз в секунду + отметка на каждом сообщении;
    watchdog меряет ЖИВОСТЬ насоса, а не только флаг `icon_shown`, и при
    зависании ПЕРЕЗАПУСКАЕТ весь поток трея (новое окно + новый значок);
  * колбэки приложения (`показать окно`, `выход`) уходят в отдельный поток —
    насос не блокируется никогда; показ окна делается чистым Win32
    (`ShowWindow`/`SetForegroundWindow`), без `Invoke` в GUI-поток;
  * раз в 5 с значок проверяется `NIM_MODIFY` (система теряет значки после
    перезапуска explorer) + обработка сообщения `TaskbarCreated`;
  * левый клик по значку ВСЕГДА показывает окно (раньше он мог спрятать его);
  * single-instance с подтверждением: второй экземпляр ждёт ответа первого
    ~2 с и, если тот не ответил (завис), ЗАПУСКАЕТСЯ САМ — «не запускается»
    больше невозможно.
"""

import ctypes
import os
import threading
import time
from ctypes import wintypes

import config
import log

WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_RASPOZ_SHOW = WM_APP + 5     # «покажи окно» первому экземпляру (single-instance)
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_MEASUREITEM = 0x002C
WM_DRAWITEM = 0x002B
WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10          # всплывающая подсказка (balloon) у значка
NIIF_INFO = 0x02         # обычная информационная подсказка
ERROR_ALREADY_EXISTS = 183
SINGLE_INSTANCE_MUTEX = "Raspoznavatel_SingleInstance_Mutex"
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40
IDM_TOGGLE = 1
IDM_EXIT = 2
TPM_BOTTOMALIGN = 0x20
TPM_LEFTALIGN = 0x0
WM_TIMER = 0x0113
WM_LBUTTONDBLCLK = 0x0203

# --- v2.6.1: надёжность трея ---
TRAY_CLASS = "RaspoznavatelTrayClass"   # класс скрытого окна-владельца значка
TRAY_TIMER_ID = 1
HEARTBEAT_MS = 1000            # пульс насоса: WM_TIMER раз в секунду
PUMP_STALE_AFTER = 6.0         # с без пульса → цикл сообщений считается мёртвым
ICON_PROBE_EVERY = 5.0         # с: раз в 5 проверяем значок через NIM_MODIFY
WATCHDOG_EVERY = 2.0           # с: период watchdog'а
SHOW_ACK_EVENT = "Raspoznavatel_Show_Ack"  # «первый экземпляр жив и показал окно»
SHOW_ACK_TIMEOUT = 2.0         # с: сколько второй экземпляр ждёт подтверждения
WAIT_OBJECT_0 = 0
EVENT_MODIFY_STATE = 0x0002
EVENT_ALL_ACCESS = 0x1F0003
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_RESTORE = 9

# --- флаги меню ---
MF_STRING = 0x0000        # обычный пункт, рисуется системой (фолбэк)
MF_SEPARATOR = 0x0800
MF_OWNERDRAW = 0x1000

# --- отрисовка пунктов (цвета в стиле интерфейса приложения) ---
MENU_BG = (0x20, 0x24, 0x2E)        # тёмный фон (#20242E)
MENU_BG_HILITE = (0x00, 0x78, 0xD7)  # подсветка пункта под курсором (синий)
MENU_TEXT = (0xEC, 0xEF, 0xF6)       # светлый текст
MENU_TEXT_HILITE = (0xFF, 0xFF, 0xFF)
MENU_ITEM_H = 32                     # высота пункта, px
MENU_SEP_H = 9                       # высота разделителя, px
MENU_W = 210                         # ширина меню, px
MENU_FONT = "Segoe UI"

# --- 64-битно-корректные типы сообщений (x86 и x64) ---
# UINT_PTR / LONG_PTR: на x64 это 64 бита, на x86 — 32; c_size_t/c_ssize_t
# дают ровно это на каждой платформе (в отличие от wintypes.WPARAM/LPARAM,
# которые на x64 ошибочно 32-битные — этим и болело v2.5.0).
WPARAM_T = ctypes.c_size_t    # UINT_PTR
LPARAM_T = ctypes.c_ssize_t   # LONG_PTR
LRESULT_T = ctypes.c_ssize_t  # LONG_PTR

ODS_SELECTED = 0x0001
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
TRANSPARENT = 1
PS_SOLID = 0


def _rgb(r, g, b):
    """COLORREF = 0x00BBGGRR."""
    return (b << 16) | (g << 8) | r


def menu_items(visible: bool):
    """
    Пункты меню трея: [(id, текст) | ('sep', None), ...].
    Чистая функция — покрыта юнит-тестами.
    """
    return [
        (IDM_TOGGLE, "Скрыть окно" if visible else "Показать окно"),
        ("sep", None),
        (IDM_EXIT, "Выход"),
    ]


def pump_is_stale(last_beat, now=None, limit=PUMP_STALE_AFTER, in_menu=False):
    """
    Чистая функция: насос сообщений не подаёт признаков жизни?
    `last_beat` — время последней доставки сообщения (0/None = ещё не было).
    Модальное меню трея легально держит насос занятым — это не «смерть».
    """
    if in_menu:
        return False
    if not last_beat:
        return True
    return ((now if now is not None else time.time()) - last_beat) > limit


def watchdog_action(icon_shown, thread_alive, last_beat, in_menu=False,
                    running=True, now=None):
    """
    Чистая функция: решение watchdog'а.
    :return: (действие, причина), действие ∈ {"ok", "restart", "stop"}
    """
    if not running:
        return ("stop", "трей остановлен")
    if in_menu:
        return ("ok", "открыто меню трея")
    if not thread_alive:
        return ("restart", "поток трея погиб")
    if pump_is_stale(last_beat, now=now, in_menu=in_menu):
        return ("restart", "насос сообщений не отвечает")
    if not icon_shown:
        return ("restart", "значок пропал")
    return ("ok", "")


def single_instance_decision(mutex_exists, hwnd_found, post_ok, ack_ok):
    """
    Чистая функция: что делать процессу, который запустился вторым.
    :return: "first"    — мы единственные, продолжаем обычный запуск;
             "opened"   — первый экземпляр ПОДТВЕРДИЛ, что показал окно → выходим;
             "takeover" — первый экземпляр не отвечает (завис / значок мёртв) →
                          запускаемся сами, иначе программа «не запускается».
    """
    if not mutex_exists:
        return "first"
    if hwnd_found and post_ok and ack_ok:
        return "opened"
    return "takeover"


def claim_single_instance(ack_timeout=SHOW_ACK_TIMEOUT):
    """
    ОДИН экземпляр программы + гарантия, что повторный запуск ВСЕГДА
    заканчивается открытым окном.

    Возвращает True, если ЭТОТ процесс должен продолжить запуск (мы первые
    либо первый экземпляр не ответил — перехват). Возвращает False, только
    если уже работающая программа подтвердила, что показала окно.
    """
    if os.name != "nt":
        return True
    k32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    k32.ResetEvent.restype = wintypes.BOOL
    k32.ResetEvent.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    # мьютекс намеренно не закрывается: живёт до конца процесса
    k32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    mutex_exists = (k32.GetLastError() == ERROR_ALREADY_EXISTS)

    hwnd = None
    post_ok = ack_ok = False
    if mutex_exists:
        hwnd = user32.FindWindowW(TRAY_CLASS, None)
        ack = None
        try:
            # auto-reset событие: ждём СВЕЖЕГО подтверждения первого экземпляра
            ack = k32.CreateEventW(None, False, False, SHOW_ACK_EVENT)
            if ack:
                k32.ResetEvent(ack)
        except Exception as exc:
            log.warn(f"Событие подтверждения недоступно: {exc}")
        if hwnd:
            post_ok = bool(user32.PostMessageW(hwnd, WM_RASPOZ_SHOW, 0, 0))
            if post_ok and ack:
                ack_ok = (k32.WaitForSingleObject(
                    ack, int(ack_timeout * 1000)) == WAIT_OBJECT_0)

    decision = single_instance_decision(mutex_exists, bool(hwnd), post_ok, ack_ok)
    if decision == "opened":
        log.info("Программа уже запущена: окно открыл первый экземпляр")
        return False
    if decision == "takeover":
        log.warn("Уже работающая программа не ответила (зависла или её значок "
                 "мёртв) — запускаемся сами: окно обязано открыться")
    return True




class MEASUREITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT),
        ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT),
        ("itemWidth", wintypes.UINT),
        ("itemHeight", wintypes.UINT),
        ("itemData", ctypes.c_void_p),
    ]


class DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT),
        ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT),
        ("itemAction", wintypes.UINT),
        ("itemState", wintypes.UINT),
        ("hwndItem", ctypes.c_void_p),
        ("hDC", ctypes.c_void_p),
        ("rcItem", wintypes.RECT),
        ("itemData", ctypes.c_void_p),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        # --- поля всплывающей подсказки (NIF_INFO) ---
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeout", wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


# Win32-типы существуют только в Windows; на других ОС модуль всё равно
# импортируется (для юнит-тестов чистых функций), но трей не стартует.
if os.name == "nt":  # pragma: no cover
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT_T, wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    def _setup_prototypes():
        """
        Явные argtypes/restype: дескрипторы и указатели не обрезаются на x64.

        v2.6.1: КАЖДЫЙ прототип настраивается отдельно и в try/except.
        Раньше одна неверная привязка (FillRect искали в gdi32, а он живёт в
        user32) роняла всю функцию целиком → поток трея завершался сразу после
        старта → значок «дорисовывал» watchdog в СВОЁМ потоке, без насоса
        сообщений. Итог у пользователя: иконка в трее видна, но не реагирует ни
        на левый, ни на правый клик, а повторный запуск программы молча
        завершался. Одна опечатка не имеет права убивать весь трей.
        """
        u = ctypes.windll.user32
        g = ctypes.windll.gdi32
        sh = ctypes.windll.shell32
        k = ctypes.windll.kernel32

        def proto(lib, name, restype, argtypes=None):
            try:
                fn = getattr(lib, name)
                fn.restype = restype
                if argtypes is not None:
                    fn.argtypes = argtypes
            except Exception as exc:
                log.warn(f"Трей: прототип {name} не настроен: {exc}")

        # --- user32: окно, сообщения, меню ---
        proto(u, "DefWindowProcW", LRESULT_T,
              [wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T])
        proto(u, "RegisterClassW", wintypes.BOOL, [ctypes.POINTER(WNDCLASSW)])
        proto(u, "CreateWindowExW", wintypes.HWND,
              [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
               wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p])
        proto(u, "GetMessageW", ctypes.c_int,     # 0 = WM_QUIT, -1 = ошибка
              [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
               wintypes.UINT, wintypes.UINT])
        proto(u, "TranslateMessage", wintypes.BOOL, [ctypes.POINTER(wintypes.MSG)])
        proto(u, "DispatchMessageW", LRESULT_T, [ctypes.POINTER(wintypes.MSG)])
        proto(u, "PostMessageW", wintypes.BOOL,
              [wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T])
        proto(u, "SendMessageW", LRESULT_T,
              [wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T])
        proto(u, "SetTimer", wintypes.UINT,
              [wintypes.HWND, wintypes.UINT, wintypes.UINT, ctypes.c_void_p])
        proto(u, "KillTimer", wintypes.BOOL, [wintypes.HWND, wintypes.UINT])
        proto(u, "RegisterWindowMessageW", wintypes.UINT, [wintypes.LPCWSTR])
        proto(u, "FindWindowW", ctypes.c_void_p,
              [wintypes.LPCWSTR, wintypes.LPCWSTR])
        proto(u, "GetWindowThreadProcessId", wintypes.DWORD,
              [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)])
        proto(u, "CreatePopupMenu", wintypes.HMENU)
        proto(u, "AppendMenuW", wintypes.BOOL,
              [wintypes.HMENU, wintypes.UINT, WPARAM_T, wintypes.LPCWSTR])
        proto(u, "TrackPopupMenu", wintypes.BOOL,
              [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
               ctypes.c_int, wintypes.HWND, ctypes.c_void_p])
        proto(u, "DestroyMenu", wintypes.BOOL, [wintypes.HMENU])
        proto(u, "GetCursorPos", wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)])
        proto(u, "SetForegroundWindow", wintypes.BOOL, [wintypes.HWND])
        proto(u, "BringWindowToTop", wintypes.BOOL, [wintypes.HWND])
        # --- user32: отрисовка пунктов меню (FillRect — ИМЕННО user32!) ---
        proto(u, "FillRect", ctypes.c_int,
              [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH])
        proto(u, "DrawTextW", ctypes.c_int,
              [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
               ctypes.POINTER(wintypes.RECT), wintypes.UINT])
        proto(u, "LoadImageW", wintypes.HICON,
              [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
               ctypes.c_int, ctypes.c_int, wintypes.UINT])
        proto(u, "LoadIconW", wintypes.HICON, [wintypes.HINSTANCE, wintypes.LPCWSTR])

        # --- gdi32: кисть, перо, шрифт ---
        proto(g, "CreateSolidBrush", wintypes.HBRUSH, [wintypes.COLORREF])
        proto(g, "CreatePen", wintypes.HPEN,
              [ctypes.c_int, ctypes.c_int, wintypes.COLORREF])
        proto(g, "SelectObject", wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ])
        proto(g, "DeleteObject", wintypes.BOOL, [wintypes.HGDIOBJ])
        proto(g, "SetBkMode", ctypes.c_int, [wintypes.HDC, ctypes.c_int])
        proto(g, "SetTextColor", wintypes.COLORREF, [wintypes.HDC, wintypes.COLORREF])
        proto(g, "MoveToEx", wintypes.BOOL,
              [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p])
        proto(g, "LineTo", wintypes.BOOL, [wintypes.HDC, ctypes.c_int, ctypes.c_int])
        try:
            # CreateFontW: 14 параметров — настраиваем без argtypes (числа и
            # строка передаются корректно), главное — restype HFONT (64 бита)
            g.CreateFontW.restype = wintypes.HFONT
        except Exception as exc:
            log.warn(f"Трей: прототип CreateFontW не настроен: {exc}")

        # --- shell32 / kernel32 ---
        proto(sh, "Shell_NotifyIconW", wintypes.BOOL,
              [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)])
        proto(k, "GetModuleHandleW", ctypes.c_void_p, [wintypes.LPCWSTR])
        proto(k, "CreateMutexW", ctypes.c_void_p,
              [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR])
        proto(k, "CreateEventW", wintypes.HANDLE,
              [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR])
        proto(k, "OpenEventW", wintypes.HANDLE,
              [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR])
        proto(k, "SetEvent", wintypes.BOOL, [wintypes.HANDLE])
        proto(k, "ResetEvent", wintypes.BOOL, [wintypes.HANDLE])
        proto(k, "WaitForSingleObject", wintypes.DWORD,
              [wintypes.HANDLE, wintypes.DWORD])

else:  # pragma: no cover
    WNDPROC = None
    WNDCLASSW = None


class TrayIcon:
    """Иконка в трее. Работает в отдельном потоке со своим циклом сообщений."""

    def __init__(self, on_show, on_exit, tooltip="Распознаватель",
                 on_toggle=None, is_visible=None):
        self.on_show = on_show
        self.on_exit = on_exit
        self.on_toggle = on_toggle or on_show
        self.is_visible = is_visible or (lambda: True)
        self.tooltip = tooltip
        self._thread = None
        self._watch = None
        self._hwnd = None
        self._nid = None
        self._running = False
        self._hfont = None
        self._lock = threading.Lock()
        self._restart_lock = threading.Lock()
        # значок РЕАЛЬНО создан в трее (Shell_NotifyIcon вернул успех)
        self.icon_shown = False
        # --- v2.6.1: живость насоса сообщений ---
        self._last_beat = 0.0        # время последнего доставленного сообщения
        self._last_probe = 0.0       # последняя проверка значка (NIM_MODIFY)
        self._in_menu = False        # открыто модальное меню (насос легально занят)
        self._timer_id = 0           # WM_TIMER — пульс насоса
        self._taskbar_msg = 0        # RegisterWindowMessage("TaskbarCreated")
        self._ack = None             # событие подтверждения второму экземпляру
        self._restarts = 0           # сколько раз перезапускали поток трея
        self._generation = 0         # поколение потока: старое не мешает новому
        self._stopped = False
        # самоотрисовка включена; если WM_DRAWITEM хоть раз не сможет
        # отработать — навсегда переключимся на системное меню (MF_STRING)
        self._ownerdraw_failed = False

    # --- Win32-обвязка ---
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        user32 = ctypes.windll.user32
        # ЛЮБОЕ доставленное сообщение доказывает, что насос жив (пульс).
        self._last_beat = time.time()
        if msg == WM_TRAYICON:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                # v2.6.1: левый клик ВСЕГДА показывает окно. Раньше он «переключал»
                # и мог спрятать открытое окно — пользователь терял программу.
                self._spawn(self._call_show)
            elif lparam == WM_RBUTTONUP:
                # меню рисуется ТОЛЬКО из потока насоса (TrackPopupMenu)
                self._show_menu(hwnd)
            return 0
        if msg == WM_RASPOZ_SHOW:
            # второй экземпляр попросил показать окно: СНАЧАЛА подтверждаем,
            # что мы живы (иначе он запустится сам — и это правильное поведение)
            self._signal_ack()
            self._spawn(self._call_show)
            return 0
        if msg == WM_TIMER:
            self._probe_icon()
            return 0
        if self._taskbar_msg and msg == self._taskbar_msg:
            # explorer.exe перезапустился — система потеряла все значки
            log.warn("Трей: получен TaskbarCreated — возвращаю значок")
            self.icon_shown = False
            self._add_icon()
            return 0
        if msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == IDM_TOGGLE:
                self._spawn(self._call_toggle)
            elif cmd == IDM_EXIT:
                self._remove_icon()
                self._spawn(self._call_exit)
            return 0
        if msg == WM_MEASUREITEM:
            try:
                mis = ctypes.cast(
                    ctypes.c_void_p(lparam),
                    ctypes.POINTER(MEASUREITEMSTRUCT)).contents
                mis.itemWidth = MENU_W
                mis.itemHeight = (
                    MENU_SEP_H if mis.itemID == 0 else MENU_ITEM_H)
            except Exception as exc:  # pragma: no cover
                log.error(f"Трей: сбой WM_MEASUREITEM: {exc}")
                return 0
            return 1
        if msg == WM_DRAWITEM:
            try:
                dis = ctypes.cast(
                    ctypes.c_void_p(lparam),
                    ctypes.POINTER(DRAWITEMSTRUCT)).contents
                self._draw_item(dis)
            except Exception as exc:
                # самоотрисовка не смогла выполниться — дальше рисуем
                # обычным системным меню: надписи обязаны быть читаемы
                self._ownerdraw_failed = True
                log.error(f"Трей: самоотрисовка отключена ({exc})")
            return 1
        if msg == WM_CLOSE:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _spawn(self, fn):
        """
        Колбэки приложения выполняются В ОТДЕЛЬНОМ потоке.
        Насос сообщений трея не имеет права блокироваться: pywebview
        `window.show()` — это синхронный `Invoke` в GUI-поток, и если тот занят,
        поток трея замирал навсегда (иконка видна, но клики «съедаются»).
        """
        try:
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            return t
        except Exception as exc:  # pragma: no cover
            log.error(f"Трей: не удалось запустить обработчик: {exc}")
            return None

    def _call_toggle(self):
        try:
            self.on_toggle()
        except Exception as exc:
            log.error(f"Трей: не удалось переключить окно: {exc}")

    def _call_show(self):
        try:
            self.on_show()
        except Exception as exc:
            log.error(f"Трей: не удалось показать окно: {exc}")

    def _call_exit(self):
        try:
            self.on_exit()
        except Exception as exc:
            log.error(f"Трей: не удалось завершить программу: {exc}")

    def _signal_ack(self):
        """Подтверждение второму экземпляру: «я жив и показываю окно»."""
        try:
            k32 = ctypes.windll.kernel32
            if not self._ack:
                k32.CreateEventW.restype = wintypes.HANDLE
                k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                             wintypes.BOOL, wintypes.LPCWSTR]
                self._ack = k32.CreateEventW(None, False, False, SHOW_ACK_EVENT)
            if self._ack:
                k32.SetEvent(self._ack)
        except Exception:
            pass

    # --- owner-draw отрисовка пунктов меню ---
    def _draw_item(self, dis):
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        dc = dis.hDC
        rc = dis.rcItem
        hilight = bool(dis.itemState & ODS_SELECTED)
        items = menu_items(bool(self.is_visible()))
        label = ""
        # разделители добавлены с wID=0; пункты — с IDM_TOGGLE / IDM_EXIT
        is_sep = (dis.itemID == 0)
        if not is_sep:
            for iid, text in items:
                if iid == dis.itemID:
                    label = text or ""
                    break
        rect = wintypes.RECT(rc.left, rc.top, rc.right, rc.bottom)
        if is_sep:
            brush = gdi32.CreateSolidBrush(_rgb(*MENU_BG))
            old = gdi32.SelectObject(dc, brush)
            user32.FillRect(dc, ctypes.byref(rect), brush)   # FillRect — user32!
            gdi32.SelectObject(dc, old)
            gdi32.DeleteObject(brush)
            # линия разделителя по центру
            pen = gdi32.CreatePen(PS_SOLID, 1, _rgb(0x4A, 0x50, 0x5E))
            oldpen = gdi32.SelectObject(dc, pen)
            y = (rc.top + rc.bottom) // 2
            gdi32.MoveToEx(dc, rc.left + 8, y, None)
            gdi32.LineTo(dc, rc.right - 8, y)
            gdi32.SelectObject(dc, oldpen)
            gdi32.DeleteObject(pen)
            return
        # фон пункта
        bg = MENU_BG_HILITE if hilight else MENU_BG
        fg = MENU_TEXT_HILITE if hilight else MENU_TEXT
        brush = gdi32.CreateSolidBrush(_rgb(*bg))
        old = gdi32.SelectObject(dc, brush)
        user32.FillRect(dc, ctypes.byref(rect), brush)       # FillRect — user32!
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(brush)
        # текст
        if self._hfont is None:
            self._hfont = gdi32.CreateFontW(
                -16, 0, 0, 0, 400, 0, 0, 0, 204, 0, 0, 5, 0, MENU_FONT)
        oldfont = gdi32.SelectObject(dc, self._hfont)
        gdi32.SetBkMode(dc, TRANSPARENT)
        gdi32.SetTextColor(dc, _rgb(*fg))
        text_rc = wintypes.RECT(rc.left + 14, rc.top, rc.right - 8, rc.bottom)
        user32.DrawTextW(dc, label, -1, ctypes.byref(text_rc),
                         DT_VCENTER | DT_SINGLELINE)
        gdi32.SelectObject(dc, oldfont)

    def _show_menu(self, hwnd):
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        ownerdraw = not self._ownerdraw_failed
        for iid, text in menu_items(bool(self.is_visible())):
            if iid == "sep":
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            elif ownerdraw:
                user32.AppendMenuW(menu, MF_OWNERDRAW, iid, text)
            else:
                # фолбэк: пункт рисует система — читаемо в любой теме
                user32.AppendMenuW(menu, MF_STRING, iid, text)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # TrackPopupMenu держит МОДАЛЬНЫЙ цикл: помечаем это, чтобы watchdog
        # не принял занятый насос за мёртвый и не перезапустил трей на глазах
        # у пользователя.
        self._in_menu = True
        try:
            user32.SetForegroundWindow(hwnd)
            user32.TrackPopupMenu(menu, TPM_BOTTOMALIGN | TPM_LEFTALIGN,
                                  pt.x, pt.y, 0, hwnd, None)
            # без WM_NULL меню может не закрыться при клике вне его области
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            self._in_menu = False
            self._last_beat = time.time()
        user32.DestroyMenu(menu)

    def _load_icon(self):
        """Загружает иконку из ресурсов приложения."""
        user32 = ctypes.windll.user32
        candidates = [
            os.path.join(config.UI_DIR, "assets", "icon.ico"),
            os.path.join(config.app_root(), "ui", "assets", "icon.ico"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                hicon = user32.LoadImageW(
                    None, path, IMAGE_ICON, 0, 0,
                    LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if hicon:
                    return hicon
        return user32.LoadIconW(None, 32512)  # IDI_APPLICATION как запасной

    def _remove_icon(self):
        with self._lock:
            try:
                if self._nid and self._hwnd:
                    self._nid.uFlags = 0
                    ctypes.windll.shell32.Shell_NotifyIconW(
                        NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            self.icon_shown = False

    def _kill_timer(self):
        try:
            if self._timer_id and self._hwnd:
                ctypes.windll.user32.KillTimer(self._hwnd, self._timer_id)
        except Exception:
            pass
        self._timer_id = 0

    # --- v2.6.0: пошаговое создание трея с проверками и повторами ---
    def _ensure_window(self):
        """Скрытое окно-владелец трея; создаётся один раз, с повтором."""
        if self._hwnd:
            return True
        user32 = ctypes.windll.user32
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wnd_proc)  # ссылка от сборщика мусора
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = TRAY_CLASS
        user32.RegisterClassW(ctypes.byref(wc))  # False = класс уже есть
        try:
            # сообщение «explorer перезапустился» — чтобы вернуть значок
            self._taskbar_msg = user32.RegisterWindowMessageW("TaskbarCreated")
        except Exception:
            self._taskbar_msg = 0
        for attempt in (1, 2):
            self._hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, "RaspoznavatelTray",
                0, 0, 0, 0, 0, None, None, hinst, None)
            if self._hwnd:
                return True
            log.warn(f"Трей: не удалось создать скрытое окно (попытка {attempt})")
            time.sleep(0.2)
        log.error("Трей: скрытое окно не создалось — значка не будет")
        return False

    def _add_icon(self):
        """
        Регистрация значка в трее; возврат — значок РЕАЛЬНО создан.

        v2.6.1: вызывается ТОЛЬКО из потока с насосом сообщений. Значок,
        привязанный к окну чужого потока, выглядит живым, но клики по нему
        уходят в очередь, которую никто не читает (именно так v2.6.0 терял
        управление программой).
        """
        if self._thread is not None and threading.current_thread() is not self._thread:
            log.error("Трей: отказ создавать значок вне потока насоса — "
                      "значок был бы «муляжом»; поток трея будет перезапущен")
            return False
        with self._lock:
            if self.icon_shown:
                return True
            if not self._ensure_window():
                return False
            self._nid = NOTIFYICONDATAW()
            self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self._nid.hWnd = self._hwnd
            self._nid.uID = 1
            self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            self._nid.uCallbackMessage = WM_TRAYICON
            self._nid.hIcon = self._load_icon()
            self._nid.szTip = self.tooltip[:127]
            for attempt in (1, 2):
                if ctypes.windll.shell32.Shell_NotifyIconW(
                        NIM_ADD, ctypes.byref(self._nid)):
                    self.icon_shown = True
                    self._last_beat = time.time()
                    self._start_timer()
                    log.ok("Иконка в системном трее создана")
                    return True
                log.warn(f"Трей: Shell_NotifyIcon отказал (попытка {attempt})")
                time.sleep(0.2)
            log.error("Трей: значок в системном трее не создался")
            return False

    def _start_timer(self):
        """WM_TIMER раз в секунду: пульс насоса + периодическая проверка значка."""
        try:
            if self._hwnd and not self._timer_id:
                self._timer_id = ctypes.windll.user32.SetTimer(
                    self._hwnd, TRAY_TIMER_ID, HEARTBEAT_MS, None)
        except Exception as exc:
            log.warn(f"Трей: таймер пульса не поставлен: {exc}")
            self._timer_id = 0

    def _probe_icon(self):
        """
        Раз в ICON_PROBE_EVERY секунд: NIM_MODIFY возвращает FALSE, если значка
        в трее больше нет (его теряют при перезапуске explorer, при сбое
        оболочки). Тогда добавляем значок заново.
        """
        now = time.time()
        if now - self._last_probe < ICON_PROBE_EVERY:
            return
        self._last_probe = now
        try:
            with self._lock:
                if not (self._nid and self._hwnd):
                    return
                self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
                alive = bool(ctypes.windll.shell32.Shell_NotifyIconW(
                    NIM_MODIFY, ctypes.byref(self._nid)))
            if not alive and self.icon_shown:
                log.warn("Трей: система больше не знает наш значок — возвращаю")
                self.icon_shown = False
                self._add_icon()
        except Exception as exc:
            log.warn(f"Трей: проверка значка не удалась: {exc}")

    def healthy(self) -> bool:
        """
        Трей ДЕЙСТВИТЕЛЬНО работает: значок есть, поток жив, насос подаёт пульс.
        Только при таком условии окно разрешено прятать в трей.
        """
        return bool(
            self._running and self.icon_shown
            and self._thread is not None and self._thread.is_alive()
            and not pump_is_stale(self._last_beat, in_menu=self._in_menu))

    def ensure_icon(self, timeout=3.0) -> bool:
        """
        Вернуть трей в здоровое состояние (значок + ЖИВОЙ насос сообщений).
        Чинит сам: перезапускает поток трея и ждёт результата до `timeout` с.
        """
        if os.name != "nt":
            return False
        if self.healthy():
            return True
        if self._thread is None or not self._thread.is_alive():
            reason = "поток трея погиб"
        elif pump_is_stale(self._last_beat, in_menu=self._in_menu):
            reason = "насос сообщений не отвечает"
        else:
            reason = "значок пропал"
        self._restart(reason)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.healthy():
                return True
            time.sleep(0.2)
        return self.healthy()

    def _restart(self, reason):
        """
        Полный перезапуск трея: снять старый значок, остановить старый поток,
        создать НОВЫЙ поток (он создаст своё окно и свой значок и будет качать
        его сообщения). Вызывается из watchdog'а или из ensure_icon().
        """
        with self._restart_lock:
            self._restarts += 1
            n = self._restarts
            old_thread, old_hwnd = self._thread, self._hwnd
            log.warn(f"Трей: перезапуск потока (причина: {reason}, попытка {n})")
            self._running = False
            self._remove_icon()      # старый значок снимаем до убийства окна
            self._kill_timer()
            try:
                if old_hwnd:
                    ctypes.windll.user32.PostMessageW(old_hwnd, WM_QUIT, 0, 0)
            except Exception:
                pass
            if (old_thread is not None and old_thread.is_alive()
                    and old_thread is not threading.current_thread()):
                old_thread.join(timeout=1.0)
            self._hwnd = None
            self._nid = None
            self._timer_id = 0
            self._in_menu = False
            self._last_beat = time.time()
            self.icon_shown = False
            self._running = True
            self._generation += 1     # старое поколение больше не «главное»
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def notify(self, title, text, timeout_ms=10000):
        """Всплывающая подсказка у значка трея (balloon, NIF_INFO)."""
        if not (self.icon_shown and self._nid):
            return False
        nid = self._nid
        nid.uFlags = NIF_INFO
        nid.szInfo = text[:255]
        nid.szInfoTitle = title[:63]
        nid.dwInfoFlags = NIIF_INFO
        nid.uTimeout = timeout_ms
        return bool(ctypes.windll.shell32.Shell_NotifyIconW(
            NIM_MODIFY, ctypes.byref(nid)))

    # --- публичное API ---
    def start(self):
        """Запускает трей в отдельном потоке (не блокирует приложение)."""
        if os.name != "nt":
            log.warn("Трей доступен только в Windows")
            return False
        if self._running and self._thread is not None and self._thread.is_alive():
            return True
        self._running = True
        self._stopped = False
        self._last_beat = time.time()
        self._generation += 1
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # watchdog: раз в 5 с проверяет, что значок на месте, и возвращает его
        self._watch = threading.Thread(target=self._watchdog, daemon=True)
        self._watch.start()
        return True

    def _watchdog(self):
        """
        v2.6.1: следим НЕ только за значком, но и за ЖИВЫМ насосом сообщений.
        Значок, созданный в потоке без насоса, — «муляж»: он виден, но не
        реагирует ни на клики, ни на просьбу второго экземпляра показать окно.
        Решение принимает чистая функция watchdog_action(), а чиним перезапуском
        ВСЕГО потока трея (окно из watchdog'а не создаём НИКОГДА).
        """
        while self._running:
            time.sleep(WATCHDOG_EVERY)
            if not self._running:
                return
            action, reason = watchdog_action(
                icon_shown=self.icon_shown,
                thread_alive=bool(self._thread is not None
                                  and self._thread.is_alive()),
                last_beat=self._last_beat,
                in_menu=self._in_menu,
                running=self._running)
            if action == "restart":
                self._restart(reason)

    def stop(self):
        self._running = False
        self._stopped = True
        self._remove_icon()
        self._kill_timer()
        try:
            if self._hwnd:
                ctypes.windll.user32.PostMessageW(self._hwnd, WM_QUIT, 0, 0)
        except Exception:
            pass
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)

    def _message_loop(self, generation):
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        self._last_beat = time.time()
        while self._running and self._generation == generation:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            self._last_beat = time.time()   # насос жив
            if ret == 0:      # WM_QUIT
                break
            if ret == -1:     # ошибка GetMessage — выходим в retry-обёртку
                log.error("Трей: GetMessage вернул ошибку")
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        # убирать значок имеет право ТОЛЬКО актуальное поколение: иначе поток,
        # который «залип» и проснулся после перезапуска, снесёт НОВЫЙ значок
        if self._generation == generation:
            self._remove_icon()
            self._kill_timer()

    def _run(self):
        try:
            _setup_prototypes()
        except Exception as exc:
            log.error(f"Трей: не удалось настроить Win32-прототипы: {exc}")
            return
        generation = self._generation
        attempt = 0
        while self._running and self._generation == generation and attempt < 5:
            attempt += 1
            if attempt > 1:
                self._hwnd = None   # окно прошлого потока уничтожено системой
            if not self._add_icon():
                time.sleep(1)
                continue
            try:
                self._message_loop(generation)
            except Exception as exc:
                log.error(f"Трей: сбой цикла сообщений: {exc}")
            if not self._running or self._generation != generation:
                return
            log.warn(f"Трей: цикл сообщений завершился — пересоздаю значок "
                     f"(попытка {attempt})")
        if self._running:
            log.error("Трей: значок не удержался — поток завершён; watchdog "
                      "перезапустит его (окно и горячие клавиши работают)")

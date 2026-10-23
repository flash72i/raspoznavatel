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
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40
IDM_TOGGLE = 1
IDM_EXIT = 2
TPM_BOTTOMALIGN = 0x20
TPM_LEFTALIGN = 0x0

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
        """Явные argtypes/restype: дескрипторы и указатели не обрезаются на x64."""
        u = ctypes.windll.user32
        g = ctypes.windll.gdi32
        sh = ctypes.windll.shell32

        u.DefWindowProcW.restype = LRESULT_T
        u.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T]
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.AppendMenuW.restype = wintypes.BOOL
        u.AppendMenuW.argtypes = [
            wintypes.HMENU, wintypes.UINT, WPARAM_T, wintypes.LPCWSTR]
        u.TrackPopupMenu.restype = wintypes.BOOL
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.PostMessageW.restype = wintypes.BOOL
        u.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, WPARAM_T, LPARAM_T]
        u.DestroyMenu.restype = wintypes.BOOL
        u.DestroyMenu.argtypes = [wintypes.HMENU]
        u.GetCursorPos.restype = wintypes.BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.DrawTextW.restype = ctypes.c_int
        u.DrawTextW.argtypes = [
            wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
            ctypes.POINTER(wintypes.RECT), wintypes.UINT]
        u.LoadImageW.restype = wintypes.HICON
        u.LoadIconW.restype = wintypes.HICON
        u.RegisterClassW.restype = wintypes.BOOL
        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.CreateWindowExW.restype = wintypes.HWND
        u.GetMessageW.restype = wintypes.BOOL
        u.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND,
            wintypes.UINT, wintypes.UINT]

        g.CreateSolidBrush.restype = wintypes.HBRUSH
        g.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        g.CreatePen.restype = wintypes.HPEN
        g.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
        g.CreateFontW.restype = wintypes.HFONT
        g.SelectObject.restype = wintypes.HGDIOBJ
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g.DeleteObject.restype = wintypes.BOOL
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.FillRect.restype = ctypes.c_int
        g.FillRect.argtypes = [
            wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
        g.SetBkMode.restype = ctypes.c_int
        g.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        g.SetTextColor.restype = wintypes.COLORREF
        g.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
        g.MoveToEx.restype = wintypes.BOOL
        g.MoveToEx.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        g.LineTo.restype = wintypes.BOOL
        g.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]

        sh.Shell_NotifyIconW.restype = wintypes.BOOL

        k = ctypes.windll.kernel32
        k.GetModuleHandleW.restype = ctypes.c_void_p   # HMODULE: 64 бита!
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
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
        self._hwnd = None
        self._nid = None
        self._running = False
        self._hfont = None
        # значок РЕАЛЬНО создан в трее (Shell_NotifyIcon вернул успех)
        self.icon_shown = False
        # самоотрисовка включена; если WM_DRAWITEM хоть раз не сможет
        # отработать — навсегда переключимся на системное меню (MF_STRING)
        self._ownerdraw_failed = False

    # --- Win32-обвязка ---
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        user32 = ctypes.windll.user32
        if msg == WM_TRAYICON:
            if lparam == WM_LBUTTONUP:
                self._call_toggle()
            elif lparam == WM_RBUTTONUP:
                self._show_menu(hwnd)
            return 0
        if msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == IDM_TOGGLE:
                self._call_toggle()
            elif cmd == IDM_EXIT:
                self._remove_icon()
                try:
                    self.on_exit()
                except Exception:
                    pass
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
            gdi32.FillRect(dc, ctypes.byref(rect), brush)
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
        gdi32.FillRect(dc, ctypes.byref(rect), brush)
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
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(menu, TPM_BOTTOMALIGN | TPM_LEFTALIGN,
                              pt.x, pt.y, 0, hwnd, None)
        # без WM_NULL меню может не закрыться при клике вне его области
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
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
        try:
            if self._nid and self._hwnd:
                self._nid.uFlags = 0
                ctypes.windll.shell32.Shell_NotifyIconW(
                    NIM_DELETE, ctypes.byref(self._nid))
        except Exception:
            pass
        self.icon_shown = False

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
        wc.lpszClassName = "RaspoznavatelTrayClass"
        user32.RegisterClassW(ctypes.byref(wc))  # False = класс уже есть
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
        """Регистрация значка в трее; возврат — значок РЕАЛЬНО создан."""
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
                log.ok("Иконка в системном трее создана")
                return True
            log.warn(f"Трей: Shell_NotifyIcon отказал (попытка {attempt})")
            time.sleep(0.2)
        log.error("Трей: значок в системном трее не создался")
        return False

    def ensure_icon(self, timeout=1.5):
        """
        Здоровье трея: True, если значок реально есть в трее.
        Если значок пропал или поток трея погиб — пробует создать заново
        (иначе «свернуть в трей» оставит пользователя с процессом без окна).
        """
        if self.icon_shown:
            return True
        if os.name != "nt":
            return False
        if not self._running or (
                self._thread is not None and not self._thread.is_alive()):
            self._running = False
            self.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.icon_shown:
                return True
            self._add_icon()
            if self.icon_shown:
                return True
            time.sleep(0.2)
        return self.icon_shown

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
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        self._remove_icon()
        try:
            if self._hwnd:
                ctypes.windll.user32.PostMessageW(self._hwnd, WM_QUIT, 0, 0)
        except Exception:
            pass

    def _run(self):
        try:
            user32 = ctypes.windll.user32
            _setup_prototypes()
            if not self._add_icon():
                return

            msg = wintypes.MSG()
            while self._running:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            self._remove_icon()
        except Exception as exc:
            log.error(f"Трей: сбой потока: {exc}")

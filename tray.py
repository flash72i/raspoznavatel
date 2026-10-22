# -*- coding: utf-8 -*-
"""
tray.py — иконка в системном трее Windows (чистый ctypes, без зависимостей).

Нужна для режима автозапуска: программа стартует вместе с Windows
в свёрнутом виде и живёт в трее.

v2.5.0 — ИСПРАВЛЕНИЕ «белого невидимого меню»:
  Системное всплывающее меню (CreatePopupMenu + TrackPopupMenu) на части
  систем Windows 10/11 отрисовывалось БЕЛЫМ фоном с невидимыми светлыми
  надписями («Показать окно», «Выход» прочитать было невозможно) — тема
  оформления меню наследовалась от общесистемных настроек, которые у
  скрытого окна трея применялись некорректно.
  Теперь меню рисуется ВЛАДЕЛЬЦЕМ (MF_OWNERDRAW + WM_MEASUREITEM/
  WM_DRAWITEM): цвета задаёт сама программа — тёмный фон в стиле
  интерфейса приложения и контрастный светлый текст с подсветкой пункта
  под курсором. Видимость больше не зависит от темы Windows.

Поведение кнопок мыши:
  • левый клик  — показать окно, а если оно уже открыто — скрыть в трей;
  • правый клик — меню «Показать/Скрыть окно», «Выход».
"""

import ctypes
import os
import threading
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
NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40
IDM_TOGGLE = 1
IDM_EXIT = 2
TPM_BOTTOMALIGN = 0x20
TPM_LEFTALIGN = 0x0

# --- флаги меню ---
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
        ("hwndItem", wintypes.HWND),
        ("hDC", wintypes.HDC),
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
    ]


# Win32-типы существуют только в Windows; на других ОС модуль всё равно
# импортируется (для юнит-тестов чистых функций), но трей не стартует.
if os.name == "nt":
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM
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
            mis = ctypes.cast(lparam, ctypes.POINTER(MEASUREITEMSTRUCT)).contents
            mis.itemWidth = MENU_W
            mis.itemHeight = MENU_SEP_H if mis.itemID == 0 else MENU_ITEM_H
            # itemID == 0 не используется (идентификаторы пунктов >= IDM_TOGGLE);
            # разделитель имеет id 0 только если добавлен с wID=0 — у нас так и есть
            return 1
        if msg == WM_DRAWITEM:
            dis = ctypes.cast(lparam, ctypes.POINTER(DRAWITEMSTRUCT)).contents
            self._draw_item(dis)
            return 1
        if msg == 0x0010:  # WM_CLOSE
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
        hilight = bool(dis.itemState & 0x0001)      # ODS_SELECTED
        items = menu_items(bool(self.is_visible()))
        label = ""
        # разделители добавлены с wID=0; пункты — с IDM_TOGGLE / IDM_EXIT
        is_sep = (dis.itemID == 0)
        if not is_sep:
            for iid, text in items:
                if iid == dis.itemID:
                    label = text or ""
                    break
        if is_sep:
            brush = gdi32.CreateSolidBrush(_rgb(*MENU_BG))
            old = gdi32.SelectObject(dc, brush)
            rect = wintypes.RECT(rc.left, rc.top, rc.right, rc.bottom)
            gdi32.FillRect(dc, ctypes.byref(rect), brush)
            gdi32.SelectObject(dc, old)
            gdi32.DeleteObject(brush)
            # линия разделителя по центру
            pen = gdi32.CreatePen(0, 1, _rgb(0x4A, 0x50, 0x5E))
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
        rect = wintypes.RECT(rc.left, rc.top, rc.right, rc.bottom)
        gdi32.FillRect(dc, ctypes.byref(rect), brush)
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(brush)
        # текст
        if self._hfont is None:
            self._hfont = gdi32.CreateFontW(
                -16, 0, 0, 0, 400, 0, 0, 0, 204, 0, 0, 5, 0, MENU_FONT)
        oldfont = gdi32.SelectObject(dc, self._hfont)
        gdi32.SetBkMode(dc, 1)   # TRANSPARENT
        gdi32.SetTextColor(dc, _rgb(*fg))
        text_rc = wintypes.RECT(rc.left + 14, rc.top, rc.right - 8, rc.bottom)
        # DT_LEFT (0x0000) | DT_VCENTER (0x0004) | DT_SINGLELINE (0x0020)
        user32.DrawTextW(dc, label, -1, ctypes.byref(text_rc), 0x0024)
        gdi32.SelectObject(dc, oldfont)

    def _show_menu(self, hwnd):
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        for iid, text in menu_items(bool(self.is_visible())):
            if iid == "sep":
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, MF_OWNERDRAW, iid, text)
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
                ctypes.windll.user32.PostMessageW(self._hwnd, 0x0012, 0, 0)  # WM_QUIT
        except Exception:
            pass

    def _run(self):
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hinst = kernel32.GetModuleHandleW(None)

            self._proc = WNDPROC(self._wnd_proc)   # держим ссылку от сборщика мусора
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = "RaspoznavatelTrayClass"
            user32.RegisterClassW(ctypes.byref(wc))

            self._hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, "RaspoznavatelTray",
                0, 0, 0, 0, 0, None, None, hinst, None)
            if not self._hwnd:
                log.error("Трей: не удалось создать скрытое окно")
                return

            self._nid = NOTIFYICONDATAW()
            self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self._nid.hWnd = self._hwnd
            self._nid.uID = 1
            self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            self._nid.uCallbackMessage = WM_TRAYICON
            self._nid.hIcon = self._load_icon()
            self._nid.szTip = self.tooltip[:127]
            if not ctypes.windll.shell32.Shell_NotifyIconW(
                    NIM_ADD, ctypes.byref(self._nid)):
                log.error("Трей: Shell_NotifyIcon отказал")
                return
            log.ok("Иконка в системном трее создана")

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

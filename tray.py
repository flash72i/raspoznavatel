# -*- coding: utf-8 -*-
"""
tray.py — иконка в системном трее Windows (чистый ctypes, без зависимостей).

Нужна для режима автозапуска: программа стартует вместе с Windows
в свёрнутом виде и живёт в трее. Клик левой кнопкой — показать окно,
правой — меню «Показать окно / Выход».
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
NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40
IDM_SHOW = 1
IDM_EXIT = 2
TPM_BOTTOMALIGN = 0x20
TPM_LEFTALIGN = 0x0


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


class TrayIcon:
    """Иконка в трее. Работает в отдельном потоке со своим циклом сообщений."""

    def __init__(self, on_show, on_exit, tooltip="Распознаватель"):
        self.on_show = on_show
        self.on_exit = on_exit
        self.tooltip = tooltip
        self._thread = None
        self._hwnd = None
        self._nid = None
        self._running = False

    # --- Win32-обвязка ---
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        user32 = ctypes.windll.user32
        if msg == WM_TRAYICON:
            if lparam == WM_LBUTTONUP:
                self._call_show()
            elif lparam == WM_RBUTTONUP:
                self._show_menu(hwnd)
            return 0
        if msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == IDM_SHOW:
                self._call_show()
            elif cmd == IDM_EXIT:
                self._remove_icon()
                try:
                    self.on_exit()
                except Exception:
                    pass
            return 0
        if msg == 0x0010:  # WM_CLOSE
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _call_show(self):
        try:
            self.on_show()
        except Exception as exc:
            log.error(f"Трей: не удалось показать окно: {exc}")

    def _show_menu(self, hwnd):
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, IDM_SHOW, "Показать окно")
        user32.AppendMenuW(menu, 0x800, 0, None)   # разделитель
        user32.AppendMenuW(menu, 0, IDM_EXIT, "Выход")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(menu, TPM_BOTTOMALIGN | TPM_LEFTALIGN,
                              pt.x, pt.y, 0, hwnd, None)
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

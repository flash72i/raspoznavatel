# -*- mode: python ; coding: utf-8 -*-
# ==========================================================================
# build.spec — сборка «Распознаватель» в ПОРТАТИВНУЮ ПАПКУ:
#
#   Raspoznavatel/
#   ├── Raspoznavatel.exe      <- запуск отсюда
#   └── App/                   <- все ресурсы и библиотеки (contents_directory)
#
# Запуск:  pyinstaller build.spec
# Портативность: папку можно носить целиком (флешка), установка не нужна,
# права администратора не нужны, в реестр ничего не пишется.
# ==========================================================================

from PyInstaller.utils.hooks import (
    collect_all, collect_dynamic_libs, collect_data_files, copy_metadata,
)
import os
import sysconfig

datas = []
binaries = []
hiddenimports = []

# --- Собираем пакеты целиком (данные + DLL + скрытые импорты) -------------
for pkg in (
    "clr_loader",        # загрузчик .NET для pywebview
    "pythonnet",         # runtime Python.Runtime.dll
    "ctranslate2",       # движок перевода (содержит DLL)
    "sentencepiece",     # токенизатор перевода
    "pyzbar",            # ZBar (содержит libzbar-64.dll)
    "zxingcpp",          # zxing-cpp
    "mss",               # захват экрана
    "pytesseract",       # обёртка Tesseract
    "keyboard",          # горячие клавиши
    "pyperclip",         # буфер обмена
    "PIL",               # Pillow
    "cv2",               # OpenCV
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# --- pywebview: ОСОБАЯ обработка -------------------------------------------
# Платформенные модули webview.platforms.edgechromium/winforms на уровне
# модуля выполняют `import clr` и требуют рабочий .NET, которого может не
# быть в среде сборки (например, при сборке под Wine). Поэтому весь пакет
# webview включается ДАННЫМИ (исходники .py + DLL из lib/) — в рантайме на
# Windows они импортируются штатно. Shim-модуль clr.py кладётся рядом.
datas += collect_data_files("webview", include_py_files=True)
try:
    datas += copy_metadata("webview")
except Exception:
    pass
_sp = sysconfig.get_paths()["purelib"]
_clr_shim = os.path.join(_sp, "clr.py")
if os.path.isfile(_clr_shim):
    datas.append((_clr_shim, "."))
datas += collect_data_files("pythonnet")   # runtime/Python.Runtime.dll
datas += collect_data_files("clr_loader")  # ffi/dlls/amd64/ClrLoader.dll
try:
    datas += copy_metadata("pythonnet")
except Exception:
    pass
hiddenimports += ["clr_loader", "pythonnet", "proxy_tools", "bottle",
                  "typing_extensions", "tkinter"]

# --- Ресурсы приложения ------------------------------------------------------
# ui/        — интерфейс: index.html, styles.css, app.js, шрифты, иконка
# resources/ — tesseract (движок), tessdata (языки), argos (модель EN->RU)
datas += [
    ("ui", "ui"),
    ("resources", "resources"),
]

# --- Дополнительные скрытые импорты -------------------------------------------
hiddenimports += [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "tkinter",
    "json",
    "base64",
    "ctypes",
    "e2e_selfcheck",   # автосамопроверка интерфейса (RASPOZ_E2E=1)
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=["C:\\hooks_win"] if os.path.isdir("C:\\hooks_win") else [],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "scipy", "pandas", "IPython", "jupyter",
        "notebook", "sphinx", "pytest", "PyQt5", "PyQt6", "PySide2",
        "PySide6", "torch", "tensorflow", "stanza", "argostranslate",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,           # onedir: содержимое — в папке App
    name="Raspoznavatel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                   # БЕЗ консольного окна
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="ui/assets/icon.ico",       # иконка приложения
    uac_admin=False,                 # НЕ требует прав администратора
    version=None,
    contents_directory="App",        # все ресурсы — в подпапке App (портативная структура)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Raspoznavatel",            # папка dist/Raspoznavatel/
)

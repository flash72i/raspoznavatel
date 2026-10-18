# -*- coding: utf-8 -*-
"""
tools/pyflakes_check.py — статическая проверка «неопределённых имён» (урок L48).

Зачем: Windows-ветки кода (`if os.name == "nt"`, ctypes-вызовы) на Linux не
выполняются — ни юнит-тесты, ни selftest в них не заходят. Поэтому NameError
в такой ветке уезжает прямо к владельцу и всплывает только на его машине
(так v2.6.1 потеряла стиль сворачивания окна: в `_fix_window_styles` не было
`from ctypes import wintypes`, а `except Exception` спрятал ошибку в WARN).

Запуск:  python3 tools/pyflakes_check.py [корень_проекта]
Код возврата: 0 — чисто (или pyflakes недоступен: проверка пропущена),
              1 — найдены неопределённые имена / синтаксическая ошибка.
"""

import ast
import io
import os
import sys

FILES = [
    "api.py", "main.py", "tray.py", "translator.py", "config.py", "ocr.py",
    "log.py", "proc.py", "qr_reader.py", "screen_capture.py",
    "e2e_selfcheck.py",
    "tools/download_resources.py", "tools/pyflakes_check.py",
    "tests/test_logic.py", "tests/tray_win_smoke.py",
]


def main(root):
    try:
        from pyflakes import checker
    except ImportError:
        print("[pyflakes] не установлен — проверка пропущена "
              "(поставь: python3 -m pip install pyflakes)")
        return 0

    bad = []
    checked = 0
    for rel in FILES:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        checked += 1
        src = io.open(path, encoding="utf-8").read()
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            bad.append("%s:%s: SyntaxError: %s" % (rel, exc.lineno, exc.msg))
            continue
        for msg in checker.Checker(tree, path).messages:
            if msg.__class__.__name__ == "UndefinedName":
                bad.append("%s:%d: %s" % (rel, msg.lineno,
                                          msg.message % msg.message_args))
    if bad:
        print("[pyflakes] НАЙДЕНЫ НЕОПРЕДЕЛЁННЫЕ ИМЕНА (на Windows это NameError):")
        for line in bad:
            print("   " + line)
        return 1
    print("[pyflakes] чисто: проверено файлов %d, неопределённых имён нет" % checked)
    return 0


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main(root))
